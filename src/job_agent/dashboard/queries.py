"""Read-only SQL for the dashboard (against the schema in ``database/sql``).

Everything here is a SELECT with bound parameters. Sort orders and filters are
chosen from whitelists, never interpolated from user input. Rows are returned as
plain dicts with JSON columns decoded, ready for the templates.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

from job_agent.config import AppConfig
from job_agent.database.connection import Database, from_json
from job_agent.models import ApprovalStatus, EmailCategory, FollowupStatus, MatchCategory, PipelineState
from job_agent.utils.time import local_today, parse_iso, to_iso, utcnow

PAGE_SIZE = 25
HEARTBEAT_FRESH_SECONDS = 180

#: Current score of a job (latest ``is_current`` row), joined as ``s``.
_CURRENT_SCORE = (
    "LEFT JOIN job_scores s ON s.id = (SELECT s2.id FROM job_scores s2 WHERE s2.job_id = j.id "
    "AND s2.is_current = 1 ORDER BY s2.scored_at DESC, s2.id DESC LIMIT 1)"
)

JOB_SORTS: dict[str, str] = {
    "score": "s.overall_score IS NULL, s.overall_score DESC, j.discovered_at DESC, j.id DESC",
    "date": "j.discovered_at DESC, j.id DESC",
    "employer": "j.employer_normalized ASC, j.title_normalized ASC, j.id ASC",
    "status": "COALESCE(a.status, j.pipeline_state) ASC, s.overall_score DESC, j.id DESC",
}

OPEN_FOLLOWUP_STATUSES = (FollowupStatus.PENDING.value, FollowupStatus.DRAFTED.value,
                          FollowupStatus.AWAITING_APPROVAL.value, FollowupStatus.APPROVED.value)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def row_dict(row: sqlite3.Row | None, json_fields: Iterable[str] = (),
             defaults: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    defaults = defaults or {}
    for name in json_fields:
        if name in data:
            data[name] = from_json(data[name], defaults.get(name))
    return data


def rows_dicts(rows: Sequence[sqlite3.Row], json_fields: Iterable[str] = (),
               defaults: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    fields = tuple(json_fields)
    return [row_dict(r, fields, defaults) for r in rows]  # type: ignore[misc]


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def local_midnight_utc_iso(tz_name: str, days_ago: int = 0) -> str:
    day = local_today(tz_name) - timedelta(days=days_ago)
    return to_iso(datetime.combine(day, dtime.min, tzinfo=ZoneInfo(tz_name)))  # type: ignore[return-value]


@dataclass
class Page:
    items: list[dict[str, Any]]
    total: int
    page: int
    page_size: int = PAGE_SIZE

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.page_size))

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages


def clamp_page(value: int | str | None) -> int:
    try:
        page = int(value or 1)
    except (TypeError, ValueError):
        return 1
    return min(max(page, 1), 10_000)


# --------------------------------------------------------------------------- #
# Agent status
# --------------------------------------------------------------------------- #
def _state(db: Database, key: str) -> sqlite3.Row | None:
    return db.query_one("SELECT value, updated_at FROM agent_state WHERE key = ?", (key,))


def agent_status(db: Database) -> dict[str, Any]:
    now = utcnow()
    paused_row = _state(db, "automation_paused")
    reason_row = _state(db, "automation_paused_reason")
    heartbeat_row = _state(db, "daemon_heartbeat_at")
    heartbeat = _safe_parse(heartbeat_row["value"] if heartbeat_row else None)
    age = (now - heartbeat).total_seconds() if heartbeat else None
    running = age is not None and age < HEARTBEAT_FRESH_SECONDS
    last = db.query_one(
        "SELECT task_name, finished_at, trigger FROM agent_runs WHERE status IN ('succeeded','partial') "
        "ORDER BY finished_at DESC LIMIT 1"
    )
    nxt = db.query_one(
        "SELECT task_name, next_run_at FROM scheduled_tasks WHERE next_run_at IS NOT NULL AND enabled = 1 "
        "ORDER BY next_run_at LIMIT 1"
    )
    locks = db.query_all(
        "SELECT task_name, lock_acquired_at, lock_expires_at FROM scheduled_tasks "
        "WHERE lock_owner IS NOT NULL AND (lock_expires_at IS NULL OR lock_expires_at > ?) ORDER BY task_name",
        (to_iso(now),),
    )
    return {
        "paused": bool(paused_row and paused_row["value"] == "1"),
        "paused_reason": reason_row["value"] if reason_row else None,
        "heartbeat_at": heartbeat_row["value"] if heartbeat_row else None,
        "heartbeat_age_seconds": int(age) if age is not None else None,
        "daemon_running": running,
        "last_success": row_dict(last),
        "next_run": row_dict(nxt),
        "running_tasks": rows_dicts(locks),
    }


def _safe_parse(value: str | None) -> datetime | None:
    try:
        return parse_iso(value)
    except ValueError:
        return None


def gmail_status(db: Database, cfg: AppConfig) -> dict[str, Any]:
    """Connected only if the last success is newer than any recorded auth error."""
    if not cfg.gmail.enabled:
        return {"connected": False, "label": "Gmail integration disabled",
                "detail": "gmail.enabled is false in config/config.yaml (see docs/gmail-setup.md)."}
    ok = _state(db, "gmail_last_success_at")
    err = _state(db, "gmail_auth_error")
    ok_at = _safe_parse(ok["value"]) if ok and ok["value"] else None
    if ok and ok["value"] and ok_at is None:
        ok_at = _safe_parse(ok["updated_at"])
    err_at = _safe_parse(err["updated_at"]) if err and err["value"] else None
    if ok_at is not None and (err_at is None or ok_at > err_at):
        return {"connected": True, "label": "Gmail connected", "detail": f"last successful sync {to_iso(ok_at)}",
                "last_success_at": to_iso(ok_at)}
    if err_at is not None:
        return {"connected": False, "label": "Gmail not connected",
                "detail": f"authorization problem: {err['value']}", "auth_error": err["value"]}
    return {"connected": False, "label": "Gmail not connected",
            "detail": "no successful Gmail sync recorded yet (run `job-agent gmail auth`, then a sync)."}


def last_health(db: Database) -> dict[str, Any] | None:
    row = _state(db, "last_health")
    return from_json(row["value"]) if row else None


def pending_approval_count(db: Database) -> int:
    return int(db.scalar(
        "SELECT COUNT(*) FROM approval_requests WHERE status = 'pending' AND (expires_at IS NULL OR expires_at > ?)",
        (to_iso(utcnow()),),
    ) or 0)


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #
def overview(db: Database, cfg: AppConfig, *, top_n: int = 8) -> dict[str, Any]:
    now_iso = to_iso(utcnow())
    today_start = local_midnight_utc_iso(cfg.app.timezone)
    new_today = db.scalar(
        "SELECT COUNT(*) FROM jobs WHERE discovered_at >= ? AND duplicate_of_job_id IS NULL", (today_start,)
    )
    top = db.query_all(
        "SELECT j.id, j.job_uid, j.employer, j.title, j.location, j.work_arrangement, j.pipeline_state, "
        "j.user_relevance, s.overall_score, s.category, s.recommended_next_action, a.status AS application_status "
        f"FROM jobs j {_CURRENT_SCORE} LEFT JOIN applications a ON a.job_id = j.id "
        "WHERE s.id IS NOT NULL AND j.duplicate_of_job_id IS NULL AND j.listing_status = 'active' "
        "AND j.pipeline_state != 'filtered_out' AND (j.user_relevance IS NULL OR j.user_relevance != 'irrelevant') "
        "ORDER BY s.overall_score DESC, j.discovered_at DESC LIMIT ?",
        (top_n,),
    )
    due_cutoff = to_iso(utcnow() + timedelta(days=1))
    placeholders = ",".join("?" for _ in OPEN_FOLLOWUP_STATUSES)
    followups_due = db.query_all(
        "SELECT f.*, j.employer, j.title FROM followups f "
        "LEFT JOIN applications a ON a.id = f.application_id "
        "LEFT JOIN jobs j ON j.id = COALESCE(f.job_id, a.job_id) "
        f"WHERE f.status IN ({placeholders}) AND f.due_at <= ? ORDER BY f.due_at LIMIT 10",
        (*OPEN_FOLLOWUP_STATUSES, due_cutoff),
    )
    attention = db.query_all(
        "SELECT id, received_at, from_address, subject, category, summary, injection_suspected "
        "FROM email_messages WHERE requires_attention = 1 AND (received_at IS NULL OR received_at >= ?) "
        "ORDER BY received_at DESC, id DESC LIMIT 10",
        (to_iso(utcnow() - timedelta(days=14)),),
    )
    errors = db.query_all(
        "SELECT id, occurred_at, component, error_kind, message, human_action FROM errors WHERE resolved = 0 "
        "ORDER BY occurred_at DESC, id DESC LIMIT 5"
    )
    counts = {
        "jobs_total": db.scalar("SELECT COUNT(*) FROM jobs WHERE duplicate_of_job_id IS NULL"),
        "shortlisted": db.scalar("SELECT COUNT(*) FROM jobs WHERE pipeline_state = 'shortlisted'"),
        "applications": db.scalar("SELECT COUNT(*) FROM applications"),
        "unresolved_errors": db.scalar("SELECT COUNT(*) FROM errors WHERE resolved = 0"),
    }
    return {
        "status": agent_status(db),
        "new_jobs_today": int(new_today or 0),
        "top_matches": rows_dicts(top),
        "pending_approvals": list_approvals(db, status="pending", limit=10),
        "pending_count": pending_approval_count(db),
        "followups_due": rows_dicts(followups_due),
        "attention_emails": rows_dicts(attention),
        "recent_errors": rows_dicts(errors),
        "counts": counts,
        "now": now_iso,
    }


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
@dataclass
class JobFilters:
    q: str = ""
    category: str = ""
    state: str = ""
    source: str = ""
    sort: str = "score"
    page: int = 1

    def normalized(self) -> "JobFilters":
        categories = {c.value for c in MatchCategory}
        states = {s.value for s in PipelineState}
        return JobFilters(
            q=(self.q or "").strip()[:100],
            category=self.category if self.category in categories else "",
            state=self.state if self.state in states else "",
            source=(self.source or "").strip()[:64],
            sort=self.sort if self.sort in JOB_SORTS else "score",
            page=clamp_page(self.page),
        )

    def query_params(self, **overrides: Any) -> dict[str, Any]:
        params = {"q": self.q, "category": self.category, "state": self.state, "source": self.source,
                  "sort": self.sort, "page": self.page}
        params.update(overrides)
        return {k: v for k, v in params.items() if v not in ("", None) and not (k == "page" and v == 1)}


def list_jobs(db: Database, filters: JobFilters) -> Page:
    f = filters.normalized()
    where = ["j.duplicate_of_job_id IS NULL"]
    params: list[Any] = []
    if f.q:
        where.append("(j.employer LIKE ? ESCAPE '\\' OR j.title LIKE ? ESCAPE '\\')")
        params += [_like(f.q), _like(f.q)]
    if f.category:
        where.append("s.category = ?")
        params.append(f.category)
    if f.state:
        where.append("COALESCE(a.status, j.pipeline_state) = ?")
        params.append(f.state)
    if f.source:
        where.append("j.source_name = ?")
        params.append(f.source)
    base = f"FROM jobs j {_CURRENT_SCORE} LEFT JOIN applications a ON a.job_id = j.id WHERE " + " AND ".join(where)
    total = int(db.scalar(f"SELECT COUNT(*) {base}", params) or 0)
    rows = db.query_all(
        "SELECT j.id, j.job_uid, j.employer, j.title, j.location, j.work_arrangement, j.employment_type, "
        "j.source_name, j.discovered_at, j.posted_at, j.pipeline_state, j.listing_status, j.user_relevance, "
        "j.salary_text, s.overall_score, s.category, a.id AS application_id, a.status AS application_status "
        f"{base} ORDER BY {JOB_SORTS[f.sort]} LIMIT ? OFFSET ?",
        [*params, PAGE_SIZE, (f.page - 1) * PAGE_SIZE],
    )
    return Page(items=rows_dicts(rows), total=total, page=f.page)


def job_sources(db: Database) -> list[str]:
    return [r[0] for r in db.query_all("SELECT DISTINCT source_name FROM jobs ORDER BY source_name")]


_SCORE_JSON = ("factors", "matching_qualifications", "missing_qualifications", "potential_disqualifiers",
               "eligibility_flags", "evidence", "reasons_to_apply", "reasons_not_to_apply")


def job_detail(db: Database, job_id: int) -> dict[str, Any] | None:
    job = row_dict(db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,)),
                   ("required_qualifications", "preferred_qualifications"),
                   {"required_qualifications": [], "preferred_qualifications": []})
    if job is None:
        return None
    job.pop("raw_metadata", None)
    score = row_dict(
        db.query_one("SELECT * FROM job_scores WHERE job_id = ? AND is_current = 1 "
                     "ORDER BY scored_at DESC, id DESC LIMIT 1", (job_id,)),
        _SCORE_JSON, {k: [] for k in _SCORE_JSON},
    )
    history = db.query_all(
        "SELECT scored_at, overall_score, category, scoring_version, used_llm, llm_model FROM job_scores "
        "WHERE job_id = ? ORDER BY scored_at DESC, id DESC LIMIT 10", (job_id,)
    )
    analysis_row = db.query_one(
        "SELECT model, prompt_version, output, created_at FROM llm_analyses WHERE job_id = ? AND purpose = ? "
        "ORDER BY created_at DESC, id DESC LIMIT 1", (job_id, "job_analysis")
    )
    analysis = row_dict(analysis_row, ("output",), {"output": {}})
    if analysis is not None and not isinstance(analysis.get("output"), dict):
        analysis["output"] = {}
    if score is not None:
        for key in ("factors", "eligibility_flags", "evidence"):  # lists of objects per the ScoreResult contract
            value = score.get(key)
            score[key] = [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []
    application = row_dict(db.query_one("SELECT * FROM applications WHERE job_id = ?", (job_id,)))
    documents = rows_dicts(db.query_all(
        "SELECT id, doc_type, version, format, file_name, status, generator, created_at FROM document_versions "
        "WHERE job_id = ? ORDER BY doc_type, version DESC, format", (job_id,)
    ))
    changes = rows_dicts(db.query_all(
        "SELECT change_type, summary, detected_at FROM job_changes WHERE job_id = ? ORDER BY detected_at DESC "
        "LIMIT 20", (job_id,)
    ))
    duplicate_of = None
    if job.get("duplicate_of_job_id"):
        duplicate_of = row_dict(db.query_one("SELECT id, employer, title FROM jobs WHERE id = ?",
                                             (job["duplicate_of_job_id"],)))
    return {"job": job, "score": score, "score_history": rows_dicts(history), "analysis": analysis,
            "application": application, "documents": documents, "changes": changes,
            "duplicate_of": duplicate_of}


# --------------------------------------------------------------------------- #
# Applications
# --------------------------------------------------------------------------- #
def list_applications(db: Database, *, status: str = "", page: int = 1) -> Page:
    page = clamp_page(page)
    params: list[Any] = []
    where = "1 = 1"
    if status in {s.value for s in PipelineState}:
        where = "a.status = ?"
        params.append(status)
    total = int(db.scalar(f"SELECT COUNT(*) FROM applications a WHERE {where}", params) or 0)
    rows = db.query_all(
        "SELECT a.id, a.application_uid, a.status, a.status_updated_at, a.next_action, a.follow_up_due_at, "
        "a.submitted_at, a.confirmation_status, a.last_activity_at, j.id AS job_id, j.employer, j.title, "
        "j.location FROM applications a JOIN jobs j ON j.id = a.job_id "
        f"WHERE {where} ORDER BY a.last_activity_at DESC, a.id DESC LIMIT ? OFFSET ?",
        [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE],
    )
    return Page(items=rows_dicts(rows), total=total, page=page)


def application_detail(db: Database, application_id: int) -> dict[str, Any] | None:
    app = row_dict(db.query_one("SELECT * FROM applications WHERE id = ?", (application_id,)),
                   ("confirmation_evidence",), {"confirmation_evidence": {}})
    if app is None:
        return None
    job = row_dict(db.query_one(
        "SELECT id, job_uid, employer, title, location, job_url, application_url FROM jobs WHERE id = ?",
        (app["job_id"],)))
    events = rows_dicts(db.query_all(
        "SELECT * FROM application_events WHERE application_id = ? ORDER BY created_at, id", (application_id,)
    ), ("details", "evidence"), {"details": {}, "evidence": None})
    answers = rows_dicts(db.query_all(
        "SELECT * FROM application_answers WHERE application_id = ? "
        "ORDER BY is_legal_declaration DESC, requires_review DESC, id", (application_id,)
    ))
    documents = rows_dicts(db.query_all(
        "SELECT id, doc_type, version, format, file_name, status, created_at FROM document_versions "
        "WHERE job_id = ? ORDER BY doc_type, version DESC, format", (app["job_id"],)
    ))
    selected = {app.get("resume_document_id"), app.get("cover_letter_document_id")} - {None}
    for d in documents:
        d["selected"] = d["id"] in selected
    approvals = rows_dicts(db.query_all(
        "SELECT approval_uid, request_type, status, summary, requested_at, decided_at, decided_by, consumed_at "
        "FROM approval_requests WHERE application_id = ? ORDER BY requested_at DESC, id DESC", (application_id,)
    ))
    followups = rows_dicts(db.query_all(
        "SELECT id, followup_type, due_at, status, suggested_action FROM followups WHERE application_id = ? "
        "ORDER BY due_at", (application_id,)
    ))
    emails = rows_dicts(db.query_all(
        "SELECT m.id, m.received_at, m.subject, m.category, ea.method, ea.confirmed_by_user, ea.status_applied "
        "FROM email_associations ea JOIN email_messages m ON m.id = ea.email_id "
        "WHERE ea.application_id = ? ORDER BY m.received_at DESC", (application_id,)
    ))
    return {"application": app, "job": job, "events": events, "answers": answers, "documents": documents,
            "approvals": approvals, "followups": followups, "emails": emails}


# --------------------------------------------------------------------------- #
# Approvals
# --------------------------------------------------------------------------- #
def _approval_rows(rows: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
    now = utcnow()
    out = []
    for r in rows:
        d = dict(r)
        exp = _safe_parse(d.get("expires_at"))
        d["expired_now"] = d["status"] == ApprovalStatus.PENDING.value and exp is not None and exp <= now
        out.append(d)
    return out


def list_approvals(db: Database, *, status: str = "pending", limit: int = 100) -> list[dict[str, Any]]:
    select = (
        "SELECT r.approval_uid, r.request_type, r.application_id, r.followup_id, r.summary, r.payload_hash, "
        "r.status, r.requested_at, r.expires_at, r.decided_at, r.decided_by, j.id AS job_id, j.employer, j.title "
        "FROM approval_requests r LEFT JOIN applications a ON a.id = r.application_id "
        "LEFT JOIN jobs j ON j.id = a.job_id "
    )
    if status == "pending":
        rows = db.query_all(select + "WHERE r.status = 'pending' ORDER BY r.requested_at, r.id LIMIT ?", (limit,))
    else:
        rows = db.query_all(select + "WHERE r.status != 'pending' ORDER BY COALESCE(r.decided_at, r.requested_at) "
                            "DESC, r.id DESC LIMIT ?", (limit,))
    return _approval_rows(rows)


def approval_detail(db: Database, approval_uid: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM approval_requests WHERE approval_uid = ?", (approval_uid,))
    if row is None:
        return None
    approval = _approval_rows([row])[0]
    payload = from_json(approval.pop("payload"), {})
    application = job = None
    if approval.get("application_id"):
        application = row_dict(db.query_one(
            "SELECT id, application_uid, status, job_id FROM applications WHERE id = ?",
            (approval["application_id"],)))
        if application:
            job = row_dict(db.query_one("SELECT id, employer, title, job_url, application_url FROM jobs "
                                        "WHERE id = ?", (application["job_id"],)))
    return {"approval": approval, "payload": payload if isinstance(payload, dict) else {"value": payload},
            "application": application, "job": job}


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
def list_emails(db: Database, *, category: str = "", attention_only: bool = False, page: int = 1) -> Page:
    page = clamp_page(page)
    where = ["1 = 1"]
    params: list[Any] = []
    if category in {c.value for c in EmailCategory}:
        where.append("m.category = ?")
        params.append(category)
    if attention_only:
        where.append("m.requires_attention = 1")
    clause = " AND ".join(where)
    total = int(db.scalar(f"SELECT COUNT(*) FROM email_messages m WHERE {clause}", params) or 0)
    rows = db.query_all(
        f"SELECT m.* FROM email_messages m WHERE {clause} ORDER BY m.received_at DESC, m.id DESC LIMIT ? OFFSET ?",
        [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE],
    )
    items = rows_dicts(rows)
    ids = [e["id"] for e in items]
    assoc: dict[int, list[dict[str, Any]]] = {i: [] for i in ids}
    if ids:
        marks = ",".join("?" for _ in ids)
        for r in db.query_all(
            "SELECT ea.email_id, ea.job_id, ea.application_id, ea.method, ea.confidence, ea.confirmed_by_user, "
            "ea.status_applied, j.employer, j.title, a.application_uid FROM email_associations ea "
            "LEFT JOIN applications a ON a.id = ea.application_id "
            "LEFT JOIN jobs j ON j.id = COALESCE(ea.job_id, a.job_id) "
            f"WHERE ea.email_id IN ({marks}) ORDER BY ea.id", ids,
        ):
            assoc[r["email_id"]].append(dict(r))
    for e in items:
        e["associations"] = assoc.get(e["id"], [])
    return Page(items=items, total=total, page=page)


# --------------------------------------------------------------------------- #
# Follow-ups
# --------------------------------------------------------------------------- #
def list_followups(db: Database, *, show: str = "open", limit: int = 200) -> list[dict[str, Any]]:
    select = (
        "SELECT f.*, j.id AS related_job_id, j.employer, j.title, a.application_uid FROM followups f "
        "LEFT JOIN applications a ON a.id = f.application_id "
        "LEFT JOIN jobs j ON j.id = COALESCE(f.job_id, a.job_id) "
    )
    if show == "all":
        rows = db.query_all(select + "ORDER BY f.due_at DESC, f.id DESC LIMIT ?", (limit,))
    else:
        marks = ",".join("?" for _ in OPEN_FOLLOWUP_STATUSES)
        rows = db.query_all(select + f"WHERE f.status IN ({marks}) ORDER BY f.due_at, f.id LIMIT ?",
                            (*OPEN_FOLLOWUP_STATUSES, limit))
    now_iso = to_iso(utcnow()) or ""
    items = rows_dicts(rows)
    for f in items:
        f["overdue"] = f["status"] in OPEN_FOLLOWUP_STATUSES and (f.get("due_at") or "") <= now_iso
    return items


# --------------------------------------------------------------------------- #
# Reports + trends
# --------------------------------------------------------------------------- #
def list_reports(db: Database, limit: int = 120) -> list[dict[str, Any]]:
    return rows_dicts(db.query_all("SELECT * FROM reports ORDER BY report_date DESC LIMIT ?", (limit,)),
                      ("stats",), {"stats": {}})


def get_report(db: Database, report_id: int) -> dict[str, Any] | None:
    return row_dict(db.query_one("SELECT * FROM reports WHERE id = ?", (report_id,)), ("stats",), {"stats": {}})


@dataclass
class Series:
    key: str
    label: str
    points: list[tuple[str, int]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(c for _, c in self.points)

    @property
    def peak(self) -> int:
        return max((c for _, c in self.points), default=0)


#: (series key, label, ``reporting.queries.trend_counts`` key)
_TREND_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("jobs", "Jobs discovered", "discovered"),
    ("applications", "Applications submitted", "submitted"),  # evidence-based, like the report
    ("emails", "Job emails received", "emails"),
)


def trends(db: Database, cfg: AppConfig, days: int = 30) -> list[Series]:
    """Daily counts for the last ``days`` local days.

    Same numbers as the daily report: ``reporting.queries.trend_counts`` buckets
    each timestamp by its own local date in ``app.timezone`` (DST-aware).
    """
    from job_agent.reporting.queries import trend_counts

    rows = trend_counts(db, days, tz_name=cfg.app.timezone, end_date=local_today(cfg.app.timezone))
    return [Series(key=key, label=label, points=[(r["date"], int(r[source])) for r in rows])
            for key, label, source in _TREND_SOURCES]


# --------------------------------------------------------------------------- #
# Controls: runs, errors, documents
# --------------------------------------------------------------------------- #
def recent_runs(db: Database, limit: int = 25) -> list[dict[str, Any]]:
    return rows_dicts(db.query_all(
        "SELECT id, run_uid, task_name, trigger, status, started_at, finished_at, summary, error_kind, "
        "error_message, attempt FROM agent_runs ORDER BY id DESC LIMIT ?", (limit,)
    ), ("summary",), {"summary": {}})


def get_run(db: Database, run_uid: str) -> dict[str, Any] | None:
    return row_dict(db.query_one("SELECT * FROM agent_runs WHERE run_uid = ?", (run_uid,)))


def unresolved_errors(db: Database, limit: int = 50) -> list[dict[str, Any]]:
    return rows_dicts(db.query_all(
        "SELECT id, occurred_at, component, task_name, error_kind, message, human_action, retryable, "
        "requires_human_action FROM errors WHERE resolved = 0 ORDER BY occurred_at DESC, id DESC LIMIT ?",
        (limit,),
    ))


def task_states(db: Database) -> dict[str, dict[str, Any]]:
    return {r["task_name"]: dict(r) for r in db.query_all("SELECT * FROM scheduled_tasks")}


def get_document(db: Database, document_id: int) -> dict[str, Any] | None:
    return row_dict(db.query_one(
        "SELECT id, job_id, doc_type, format, file_name, relative_path FROM document_versions WHERE id = ?",
        (document_id,)))
