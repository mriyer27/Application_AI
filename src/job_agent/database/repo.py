"""Cross-cutting repositories: state, audit, runs, errors, applications, approvals, locks.

These functions are the *only* way the rest of the system changes application
status or approvals. They enforce the state machine and approval rules in
deterministic code. Module-specific tables (jobs, scores, documents, emails,
follow-ups, reports) are handled by their owning module's repository.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from datetime import timedelta
from typing import Any

from job_agent.database.connection import Database, from_json, to_json
from job_agent.errors import ApprovalError, InvalidTransition, JobAgentError
from job_agent.models import (
    ALLOWED_TRANSITIONS,
    APPLICATION_STATES,
    EVIDENCE_REQUIRED,
    JOB_STATES,
    USER_ONLY_WITHOUT_EVIDENCE,
    ApprovalStatus,
    ApprovalType,
    ErrorKind,
    USER_CORRECTIONS,
    PipelineState,
    RunStatus,
)
from job_agent.security.redaction import redact, redact_text
from job_agent.utils.time import parse_iso, to_iso, utcnow, utcnow_iso

ACTORS = {"system", "user", "browser", "email", "scheduler", "dashboard", "cli"}


def new_uid(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(5)}"


def payload_hash(payload: Any) -> str:
    return hashlib.sha256(to_json(payload).encode()).hexdigest()


# --------------------------------------------------------------------------- #
# agent_state (key/value)
# --------------------------------------------------------------------------- #
def get_state(db: Database, key: str, default: str | None = None) -> str | None:
    row = db.query_one("SELECT value FROM agent_state WHERE key = ?", (key,))
    return default if row is None else row["value"]


def set_state(db: Database, key: str, value: str | None) -> None:
    db.execute(
        "INSERT INTO agent_state (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, utcnow_iso()),
    )


def is_paused(db: Database) -> bool:
    return get_state(db, "automation_paused", "0") == "1"


def set_paused(db: Database, paused: bool, *, actor: str, reason: str | None = None) -> None:
    with db.transaction():
        set_state(db, "automation_paused", "1" if paused else "0")
        set_state(db, "automation_paused_reason", reason if paused else None)
        audit(db, actor=actor, action="automation_paused" if paused else "automation_resumed",
              outcome="success", details={"reason": reason})


def set_config_metadata(db: Database, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO config_metadata (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, utcnow_iso()),
    )


def record_profile_meta(db: Database, *, profile_hash: str, schema_version: int, fact_count: int,
                        skill_count: int, placeholders: int) -> None:
    now = utcnow_iso()
    db.execute(
        "INSERT INTO candidate_profile_meta (profile_hash, schema_version, fact_count, skill_count, placeholders,"
        " first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(profile_hash) DO UPDATE SET last_seen_at = excluded.last_seen_at",
        (profile_hash, schema_version, fact_count, skill_count, placeholders, now, now),
    )


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #
def audit(db: Database, *, actor: str, action: str, outcome: str, entity_type: str | None = None,
          entity_id: Any = None, details: dict[str, Any] | None = None, run_id: int | None = None) -> int:
    """Record a consequential action. Details are redacted (secrets + PII)."""
    return db.insert(
        "audit_events",
        {
            "occurred_at": utcnow_iso(),
            "actor": actor,
            "action": action,
            "entity_type": entity_type,
            "entity_id": None if entity_id is None else str(entity_id),
            "outcome": outcome,
            "details": to_json(redact(details or {}, pii=True)),
            "run_id": run_id,
        },
    )


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
def record_error(db: Database, *, component: str, error: BaseException | str, kind: ErrorKind | None = None,
                 task_name: str | None = None, run_id: int | None = None, job_id: int | None = None,
                 application_id: int | None = None, details: dict[str, Any] | None = None,
                 human_action: str | None = None, retryable: bool | None = None) -> int:
    if isinstance(error, JobAgentError):
        kind = kind or error.kind
        human_action = human_action or error.human_action
        retryable = error.retryable if retryable is None else retryable
    kind = kind or ErrorKind.PERMANENT
    if retryable is None:
        retryable = kind == ErrorKind.RECOVERABLE
    message = redact_text(f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error),
                          pii=True)[:2000]
    requires_human = kind in {ErrorKind.AUTHORIZATION_REQUIRED, ErrorKind.HUMAN_ACTION_REQUIRED,
                              ErrorKind.UNKNOWN_OUTCOME} or bool(human_action)
    return db.insert(
        "errors",
        {
            "occurred_at": utcnow_iso(),
            "component": component,
            "task_name": task_name,
            "run_id": run_id,
            "job_id": job_id,
            "application_id": application_id,
            "error_kind": kind.value,
            "message": message,
            "details": to_json(redact(details or {}, pii=True)),
            "requires_human_action": int(requires_human),
            "human_action": human_action,
            "retryable": int(bool(retryable)),
        },
    )


def resolve_error(db: Database, error_id: int, *, actor: str) -> bool:
    with db.transaction():
        cur = db.execute("UPDATE errors SET resolved = 1, resolved_at = ? WHERE id = ? AND resolved = 0",
                         (utcnow_iso(), error_id))
        if cur.rowcount:
            audit(db, actor=actor, action="error_resolved", entity_type="error", entity_id=error_id,
                  outcome="success")
        return cur.rowcount == 1


def unresolved_errors(db: Database, limit: int = 50) -> list[sqlite3.Row]:
    return db.query_all("SELECT * FROM errors WHERE resolved = 0 ORDER BY occurred_at DESC, id DESC LIMIT ?",
                        (limit,))


# --------------------------------------------------------------------------- #
# Agent runs
# --------------------------------------------------------------------------- #
def start_run(db: Database, task_name: str, trigger: str, attempt: int = 1) -> tuple[int, str]:
    uid = new_uid("R")
    run_id = db.insert(
        "agent_runs",
        {
            "run_uid": uid,
            "task_name": task_name,
            "trigger": trigger,
            "status": RunStatus.RUNNING.value,
            "started_at": utcnow_iso(),
            "attempt": attempt,
            "host_pid": os.getpid(),
        },
    )
    return run_id, uid


def finish_run(db: Database, run_id: int, status: RunStatus, *, summary: dict[str, Any] | None = None,
               error_kind: ErrorKind | None = None, error_message: str | None = None) -> None:
    db.update(
        "agent_runs",
        run_id,
        {
            "status": status.value,
            "finished_at": utcnow_iso(),
            "summary": to_json(redact(summary or {}, pii=True)),
            "error_kind": error_kind.value if error_kind else None,
            "error_message": redact_text(error_message, pii=True)[:2000] if error_message else None,
        },
    )


def mark_interrupted_runs(db: Database) -> int:
    """On startup: runs left 'running' by a dead process are marked interrupted."""
    rows = db.query_all("SELECT id, host_pid FROM agent_runs WHERE status = ?", (RunStatus.RUNNING.value,))
    count = 0
    for row in rows:
        pid = row["host_pid"]
        if pid and pid != os.getpid() and pid_alive(pid):
            continue
        if pid == os.getpid():
            continue
        finish_run(db, row["id"], RunStatus.INTERRUPTED, error_kind=ErrorKind.UNKNOWN_OUTCOME,
                   error_message="process exited before the run finished")
        count += 1
    return count


def pid_alive(pid: int) -> bool:
    """True if a process with this pid is currently running (cross-platform)."""
    if os.name == "nt":  # os.kill(pid, 0) would send CTRL_C_EVENT on Windows
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - exists but not ours
        return True
    except OSError:  # pragma: no cover - e.g. Windows semantics
        return False
    return True


def last_successful_run(db: Database, task_name: str | None = None) -> sqlite3.Row | None:
    if task_name:
        return db.query_one(
            "SELECT * FROM agent_runs WHERE status IN ('succeeded','partial') AND task_name = ? "
            "ORDER BY finished_at DESC LIMIT 1",
            (task_name,),
        )
    return db.query_one(
        "SELECT * FROM agent_runs WHERE status IN ('succeeded','partial') ORDER BY finished_at DESC LIMIT 1"
    )


# --------------------------------------------------------------------------- #
# Task locks (cross-process overlap prevention)
# --------------------------------------------------------------------------- #
def acquire_task_lock(db: Database, task_name: str, owner: str, ttl_minutes: int) -> bool:
    now = utcnow()
    with db.transaction():
        db.execute(
            "INSERT OR IGNORE INTO scheduled_tasks (task_name, updated_at) VALUES (?, ?)", (task_name, to_iso(now))
        )
        cur = db.execute(
            "UPDATE scheduled_tasks SET lock_owner = ?, lock_acquired_at = ?, lock_expires_at = ?, updated_at = ? "
            "WHERE task_name = ? AND (lock_owner IS NULL OR lock_expires_at IS NULL OR lock_expires_at < ?)",
            (owner, to_iso(now), to_iso(now + timedelta(minutes=ttl_minutes)), to_iso(now), task_name, to_iso(now)),
        )
        return cur.rowcount == 1


def release_task_lock(db: Database, task_name: str, owner: str) -> None:
    db.execute(
        "UPDATE scheduled_tasks SET lock_owner = NULL, lock_acquired_at = NULL, lock_expires_at = NULL, "
        "updated_at = ? WHERE task_name = ? AND lock_owner = ?",
        (utcnow_iso(), task_name, owner),
    )


def record_task_result(db: Database, task_name: str, status: RunStatus, *, schedule: str | None = None,
                       next_run_at: str | None = None) -> None:
    now = utcnow_iso()
    success = status in {RunStatus.SUCCEEDED, RunStatus.PARTIAL}
    db.execute("INSERT OR IGNORE INTO scheduled_tasks (task_name, updated_at) VALUES (?, ?)", (task_name, now))
    db.execute(
        "UPDATE scheduled_tasks SET last_run_at = ?, last_status = ?, updated_at = ?,"
        " last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END,"
        " consecutive_failures = CASE WHEN ? THEN 0 WHEN ? = 'skipped' THEN consecutive_failures"
        "   ELSE consecutive_failures + 1 END,"
        " schedule = COALESCE(?, schedule), next_run_at = COALESCE(?, next_run_at)"
        " WHERE task_name = ?",
        (now, status.value, now, int(success), now, int(success), status.value, schedule, next_run_at, task_name),
    )


# --------------------------------------------------------------------------- #
# Job pipeline state (pre-application)
# --------------------------------------------------------------------------- #
def set_job_state(db: Database, job_id: int, target: PipelineState, *, actor: str, reason: str | None = None) -> bool:
    """Move a job between pre-application states. Returns False if already there."""
    if target not in JOB_STATES:
        raise InvalidTransition(f"{target} is not a job state")
    row = db.query_one("SELECT pipeline_state FROM jobs WHERE id = ?", (job_id,))
    if row is None:
        raise InvalidTransition(f"job {job_id} does not exist")
    current = PipelineState(row["pipeline_state"])
    if current == target and target != PipelineState.SCORED:
        return False
    if target not in ALLOWED_TRANSITIONS[current] and current != target:
        raise InvalidTransition(f"job {job_id}: {current} -> {target} is not allowed")
    db.execute(
        "UPDATE jobs SET pipeline_state = ?, filter_reason = ?, updated_at = ? WHERE id = ?",
        (target.value, reason if target == PipelineState.FILTERED_OUT else None, utcnow_iso(), job_id),
    )
    return True


# --------------------------------------------------------------------------- #
# Applications + state machine
# --------------------------------------------------------------------------- #
def get_application(db: Database, application_id: int) -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM applications WHERE id = ?", (application_id,))


def get_application_for_job(db: Database, job_id: int) -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM applications WHERE job_id = ?", (job_id,))


def create_application(db: Database, job_id: int, *, actor: str, application_url: str | None = None) -> int:
    """Create (or return the existing) application for a job. One application per job."""
    with db.transaction():
        existing = get_application_for_job(db, job_id)
        if existing is not None:
            return int(existing["id"])
        now = utcnow_iso()
        app_id = db.insert(
            "applications",
            {
                "application_uid": new_uid("A"),
                "job_id": job_id,
                "status": PipelineState.DOCUMENTS_PENDING.value,
                "status_updated_at": now,
                "application_url": application_url,
                "last_activity_at": now,
                "next_action": "Generate tailored documents",
                "created_at": now,
                "updated_at": now,
            },
        )
        _add_event(db, app_id, "created", None, PipelineState.DOCUMENTS_PENDING, actor, {})
        audit(db, actor=actor, action="application_created", entity_type="application", entity_id=app_id,
              outcome="success", details={"job_id": job_id})
        return app_id


def _add_event(db: Database, application_id: int, event_type: str, from_status: PipelineState | None,
               to_status: PipelineState | None, actor: str, details: dict[str, Any],
               evidence: dict[str, Any] | None = None) -> int:
    return db.insert(
        "application_events",
        {
            "application_id": application_id,
            "event_type": event_type,
            "from_status": from_status.value if from_status else None,
            "to_status": to_status.value if to_status else None,
            "actor": actor,
            "details": to_json(redact(details, pii=True)),
            "evidence": to_json(redact(evidence, pii=True)) if evidence else None,
            "created_at": utcnow_iso(),
        },
    )


#: Required keys per machine evidence type (anything else is not credible evidence).
_EVIDENCE_KEYS: dict[str, tuple[str, ...]] = {
    "page": ("url", "matched_text"),
    "email": ("gmail_message_id",),
}


def _valid_evidence(evidence: dict[str, Any] | None) -> bool:
    if not isinstance(evidence, dict):
        return False
    keys = _EVIDENCE_KEYS.get(str(evidence.get("type", "")))
    return bool(keys) and all(evidence.get(k) for k in keys)


def add_application_event(db: Database, application_id: int, event_type: str, *, actor: str,
                          details: dict[str, Any] | None = None, evidence: dict[str, Any] | None = None) -> int:
    with db.transaction():
        event_id = _add_event(db, application_id, event_type, None, None, actor, details or {}, evidence)
        db.execute("UPDATE applications SET last_activity_at = ?, updated_at = ? WHERE id = ?",
                   (utcnow_iso(), utcnow_iso(), application_id))
        return event_id


def transition_application(db: Database, application_id: int, target: PipelineState, *, actor: str,
                           details: dict[str, Any] | None = None, evidence: dict[str, Any] | None = None,
                           approval_uid: str | None = None, approved_payload: dict[str, Any] | None = None,
                           next_action: str | None = None,
                           expected_current: PipelineState | None = None) -> PipelineState:
    """Validated status change with event history. Returns the previous state.

    Rules enforced here (not in prompts):
      * only transitions in ``ALLOWED_TRANSITIONS``
      * ``-> SUBMISSION_ATTEMPTED`` requires an approval that this call consumes,
        and ``approved_payload`` (the exact payload about to be submitted) must
        hash to what the human approved
      * post-submission outcomes require ``evidence`` unless the actor is the user
      * a few manual shortcuts (e.g. "I applied myself") are user-only
    """
    if actor not in ACTORS:
        raise InvalidTransition(f"unknown actor {actor!r}")
    if target not in APPLICATION_STATES:
        raise InvalidTransition(f"{target} is not an application state")
    with db.transaction():
        row = get_application(db, application_id)
        if row is None:
            raise InvalidTransition(f"application {application_id} does not exist")
        current = PipelineState(row["status"])
        if expected_current is not None and current != expected_current:
            raise InvalidTransition(
                f"application {application_id} is {current}, expected {expected_current} (concurrent change?)"
            )
        if target not in ALLOWED_TRANSITIONS[current]:
            raise InvalidTransition(f"application {application_id}: {current} -> {target} is not allowed")
        if (current, target) in USER_ONLY_WITHOUT_EVIDENCE and actor != "user":
            raise InvalidTransition(f"{current} -> {target} may only be recorded by the user")
        if (current, target) in USER_CORRECTIONS and actor != "user":
            raise InvalidTransition(f"{current} -> {target} is a correction only the user may make")
        if target in EVIDENCE_REQUIRED and actor != "user" and not _valid_evidence(evidence):
            raise InvalidTransition(
                f"-> {target} requires evidence: {{'type': 'page', 'url', 'matched_text'}} or "
                "{'type': 'email', 'gmail_message_id'}")
        if (current == PipelineState.NEEDS_MANUAL_ACTION and target == PipelineState.CONFIRMATION_RECEIVED
                and actor != "user" and int(row["submission_attempts"] or 0) == 0):
            raise InvalidTransition("a confirmation can only resolve an application whose submission was "
                                    "attempted; this one was never submitted")
        if target == PipelineState.SUBMISSION_ATTEMPTED:
            if not approval_uid or approved_payload is None:
                raise ApprovalError("submission requires an approved approval request and its payload")
            consume_approval(db, approval_uid, application_id=application_id, actor=actor,
                             current_payload=approved_payload)
        now = utcnow_iso()
        updates: dict[str, Any] = {
            "status": target.value,
            "status_updated_at": now,
            "last_activity_at": now,
            "updated_at": now,
        }
        if next_action is not None:
            updates["next_action"] = next_action
        if target == PipelineState.SUBMISSION_ATTEMPTED:
            updates["submission_attempts"] = int(row["submission_attempts"]) + 1
        if target == PipelineState.SUBMITTED and not row["submitted_at"]:
            updates["submitted_at"] = now
        if target in {PipelineState.SUBMITTED, PipelineState.CONFIRMATION_RECEIVED} and evidence:
            updates["confirmation_evidence"] = to_json(redact(evidence, pii=True))
            updates["confirmation_status"] = (
                "email_confirmed" if evidence.get("type") == "email" else
                "page_evidence" if evidence.get("type") == "page" else
                "user_reported" if actor == "user" else row["confirmation_status"]
            )
        elif target == PipelineState.SUBMITTED and actor == "user":
            updates["confirmation_status"] = "user_reported"
        if target == PipelineState.CONFIRMATION_RECEIVED and not row["submitted_at"]:
            updates["submitted_at"] = now
        db.update("applications", application_id, updates)
        _add_event(db, application_id, "status_change", current, target, actor,
                   {**(details or {}), **({"approval_uid": approval_uid} if approval_uid else {})}, evidence)
        audit(db, actor=actor, action="application_status_changed", entity_type="application",
              entity_id=application_id, outcome="success",
              details={"from": current.value, "to": target.value, "approval_uid": approval_uid})
        return current


# --------------------------------------------------------------------------- #
# Approvals
# --------------------------------------------------------------------------- #
def create_approval(db: Database, request_type: ApprovalType, payload: dict[str, Any], *, summary: str,
                    actor: str, application_id: int | None = None, followup_id: int | None = None,
                    expires_in_hours: int | None = 72) -> str:
    """Create a pending approval. A live request for the same target is superseded."""
    phash = payload_hash(payload)
    now = utcnow()
    with db.transaction():
        if application_id is not None:
            live = db.query_all(
                "SELECT approval_uid, payload_hash, status FROM approval_requests WHERE application_id = ? "
                "AND request_type = ? AND status IN ('pending','approved')",
                (application_id, request_type.value),
            )
        elif followup_id is not None:
            live = db.query_all(
                "SELECT approval_uid, payload_hash, status FROM approval_requests WHERE followup_id = ? "
                "AND request_type = ? AND status IN ('pending','approved')",
                (followup_id, request_type.value),
            )
        else:
            live = []
        for row in live:
            if row["payload_hash"] == phash and row["status"] == ApprovalStatus.PENDING.value:
                return str(row["approval_uid"])  # idempotent re-request
            db.execute("UPDATE approval_requests SET status = ?, decided_at = ?, decision_note = ? "
                       "WHERE approval_uid = ?",
                       (ApprovalStatus.SUPERSEDED.value, to_iso(now), "payload changed", row["approval_uid"]))
        uid = new_uid("AP")
        db.insert(
            "approval_requests",
            {
                "approval_uid": uid,
                "request_type": request_type.value,
                "application_id": application_id,
                "followup_id": followup_id,
                "summary": summary,
                "payload": to_json(payload),  # shown verbatim: exactly what is hashed, approved and submitted
                "payload_hash": phash,
                "status": ApprovalStatus.PENDING.value,
                "requested_at": to_iso(now),
                "expires_at": to_iso(now + timedelta(hours=expires_in_hours)) if expires_in_hours else None,
            },
        )
        audit(db, actor=actor, action="approval_requested", entity_type="approval", entity_id=uid,
              outcome="success", details={"type": request_type.value, "application_id": application_id,
                                          "followup_id": followup_id})
        return uid


def get_approval(db: Database, approval_uid: str) -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM approval_requests WHERE approval_uid = ?", (approval_uid,))


def expire_approvals(db: Database) -> int:
    cur = db.execute(
        "UPDATE approval_requests SET status = 'expired' WHERE status IN ('pending','approved') "
        "AND expires_at IS NOT NULL AND expires_at < ?",
        (utcnow_iso(),),
    )
    return cur.rowcount


def pending_approvals(db: Database) -> list[sqlite3.Row]:
    expire_approvals(db)
    return db.query_all("SELECT * FROM approval_requests WHERE status = 'pending' ORDER BY requested_at")


def decide_approval(db: Database, approval_uid: str, *, approve: bool, actor: str, note: str | None = None,
                    expected_payload_hash: str | None = None) -> None:
    """Record the human decision. Only a human actor may approve."""
    if actor not in {"user", "dashboard", "cli"}:
        raise ApprovalError("approvals can only be decided by the user (cli or dashboard)")
    if not db.in_transaction:
        expire_approvals(db)  # committed on its own so a failed decision cannot roll it back
    with db.transaction():
        expire_approvals(db)
        row = get_approval(db, approval_uid)
        if row is None:
            raise ApprovalError(f"approval {approval_uid} not found")
        if row["status"] != ApprovalStatus.PENDING.value:
            raise ApprovalError(f"approval {approval_uid} is {row['status']}, not pending")
        if expected_payload_hash is not None and expected_payload_hash != row["payload_hash"]:
            raise ApprovalError("the request changed since you reviewed it; review it again")
        status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
        db.execute(
            "UPDATE approval_requests SET status = ?, decided_at = ?, decided_by = ?, decision_note = ? "
            "WHERE approval_uid = ? AND status = 'pending'",
            (status.value, utcnow_iso(), actor, note, approval_uid),
        )
        audit(db, actor=actor, action="approval_decided", entity_type="approval", entity_id=approval_uid,
              outcome=status.value, details={"note": note})


def consume_approval(db: Database, approval_uid: str, *, application_id: int | None = None,
                     followup_id: int | None = None, actor: str, current_payload: dict[str, Any] | None = None) -> None:
    """Atomically use an approval exactly once. Raises if not approved/expired/consumed/mismatched."""
    if not db.in_transaction:
        expire_approvals(db)
    with db.transaction():
        expire_approvals(db)
        row = get_approval(db, approval_uid)
        if row is None:
            raise ApprovalError(f"approval {approval_uid} not found")
        if application_id is not None and row["application_id"] != application_id:
            raise ApprovalError("approval does not belong to this application")
        if followup_id is not None and row["followup_id"] != followup_id:
            raise ApprovalError("approval does not belong to this follow-up")
        if current_payload is not None and payload_hash(current_payload) != row["payload_hash"]:
            raise ApprovalError("the application changed after approval; a new approval is required")
        cur = db.execute(
            "UPDATE approval_requests SET status = 'consumed', consumed_at = ? "
            "WHERE approval_uid = ? AND status = 'approved'",
            (utcnow_iso(), approval_uid),
        )
        if cur.rowcount != 1:
            raise ApprovalError(f"approval {approval_uid} is {row['status']}; it must be approved and unused")
        audit(db, actor=actor, action="approval_consumed", entity_type="approval", entity_id=approval_uid,
              outcome="success", details={"application_id": application_id, "followup_id": followup_id})


def approval_payload(row: sqlite3.Row) -> dict[str, Any]:
    return from_json(row["payload"], {})


def approval_is_live(row: sqlite3.Row) -> bool:
    if row["status"] not in {ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value}:
        return False
    exp = parse_iso(row["expires_at"])
    return exp is None or exp > utcnow()
