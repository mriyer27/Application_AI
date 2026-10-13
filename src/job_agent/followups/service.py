"""Follow-up rules, idempotent creation, and user status changes.

Rules (thresholds from ``config.followups``):
  * application_no_response -- submitted/confirmed, no employer email since, N days
  * recruiter_reply         -- recruiter message still needing attention after N days
  * interview_upcoming      -- application in interview_requested / interview_scheduled
  * assessment_due          -- application in assessment_received
  * deadline                -- shortlisted job / unsubmitted application closing within 3 days
  * stale_application       -- no activity for N days (suggest marking closed)

Each rule match has a deterministic ``dedupe_key`` so repeated reviews never
create duplicates, there is never more than one open follow-up of a type per
application, and outreach follow-ups respect ``max_followups_per_application``.
Sending is not implemented: the user may mark a follow-up approved (for manual
sending), then sends it themselves and marks it completed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable, Iterable

from job_agent.database import Database, repo
from job_agent.errors import InvalidTransition, JobAgentError, PermanentError, PermissionDenied
from job_agent.followups.drafts import llm_followup_draft, suggested_action, template_draft
from job_agent.logging_config import get_logger
from job_agent.models import FollowupStatus, FollowupType, Permission, PipelineState
from job_agent.security.permissions import is_allowed
from job_agent.utils.time import parse_iso, to_iso, utcnow, utcnow_iso

if TYPE_CHECKING:  # pragma: no cover
    from job_agent.context import AppContext
    from job_agent.tasks import RunInfo

log = get_logger(__name__)

F = FollowupStatus
T = FollowupType
S = PipelineState

OPEN_STATUSES: frozenset[FollowupStatus] = frozenset({F.PENDING, F.DRAFTED, F.AWAITING_APPROVAL, F.APPROVED})
#: Follow-ups that are messages for the user to send; capped per application.
OUTREACH_TYPES: frozenset[FollowupType] = frozenset({T.APPLICATION_NO_RESPONSE, T.RECRUITER_REPLY})
#: Status changes a human may make.
USER_TRANSITIONS: dict[FollowupStatus, frozenset[FollowupStatus]] = {
    F.PENDING: frozenset({F.APPROVED, F.COMPLETED, F.DISMISSED}),
    F.DRAFTED: frozenset({F.APPROVED, F.COMPLETED, F.DISMISSED}),
    F.APPROVED: frozenset({F.COMPLETED, F.DISMISSED}),
}
HUMAN_ACTORS = frozenset({"user", "cli", "dashboard"})
DEADLINE_WINDOW = timedelta(days=3)

_SUBMITTED = (S.SUBMITTED.value, S.CONFIRMATION_RECEIVED.value)
_INTERVIEW = (S.INTERVIEW_REQUESTED.value, S.INTERVIEW_SCHEDULED.value)
_PRE_SUBMISSION = tuple(s.value for s in (S.DOCUMENTS_PENDING, S.DOCUMENTS_READY, S.IN_PROGRESS,
                                          S.AWAITING_APPROVAL, S.NEEDS_MANUAL_ACTION, S.FAILED))
_NOT_STALE = tuple(s.value for s in (S.CLOSED, S.WITHDRAWN, S.REJECTED, S.OFFER_RECEIVED))
#: Email categories that do not count as an employer *response*.
_NON_RESPONSE_CATEGORIES = ("application_confirmation", "other")


@dataclass
class FollowupSummary:
    created: int = 0
    already_existing: int = 0
    due: int = 0
    skipped_limit: int = 0  # outreach suppressed by max_followups_per_application
    by_type: dict[str, int] = field(default_factory=dict)


@dataclass
class _Proposal:
    followup_type: FollowupType
    dedupe_key: str
    due_at: datetime
    application_id: int | None = None
    job_id: int | None = None
    email_id: int | None = None
    context: dict[str, Any] = field(default_factory=dict)


def _ph(values: Iterable[str]) -> tuple[str, list[str]]:
    vals = list(values)
    return ",".join("?" for _ in vals), vals


def _dt(value: str | None) -> datetime | None:
    try:
        return parse_iso(value)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #
def _no_response(db: Database, now: datetime, days: int) -> list[_Proposal]:
    ph, vals = _ph(_SUBMITTED)
    rows = db.query_all(
        "SELECT a.id, a.job_id, a.submitted_at, a.status_updated_at, j.employer, j.title "
        f"FROM applications a JOIN jobs j ON j.id = a.job_id WHERE a.status IN ({ph})", vals)
    out = []
    for r in rows:
        submitted = _dt(r["submitted_at"]) or _dt(r["status_updated_at"])
        if submitted is None:
            continue
        nph, nvals = _ph(_NON_RESPONSE_CATEGORIES)
        responded = db.scalar(
            "SELECT COUNT(*) FROM email_associations ea JOIN email_messages em ON em.id = ea.email_id "
            "WHERE ea.application_id = ? AND (ea.confidence >= 0.8 OR ea.confirmed_by_user = 1) "
            f"AND em.received_at > ? AND em.category NOT IN ({nph})", [r["id"], to_iso(submitted), *nvals])
        if responded:
            continue
        previous = db.query_all(
            "SELECT status, due_at, COALESCE(completed_at, updated_at) AS closed_at FROM followups "
            "WHERE application_id = ? AND followup_type = ? ORDER BY id", (r["id"], T.APPLICATION_NO_RESPONSE.value))
        if previous and FollowupStatus(previous[-1]["status"]) in OPEN_STATUSES:
            # The current one is still open: re-propose it so it is counted as already existing.
            n, due = len(previous), _dt(previous[-1]["due_at"]) or now
        else:
            # The next follow-up is due N days after the previous one was handled.
            anchor = max(submitted, _dt(previous[-1]["closed_at"]) or submitted) if previous else submitted
            n, due = len(previous) + 1, anchor + timedelta(days=days)
            if due > now:
                continue
        out.append(_Proposal(T.APPLICATION_NO_RESPONSE, f"{T.APPLICATION_NO_RESPONSE.value}:{r['id']}:{n}", due,
                             application_id=r["id"], job_id=r["job_id"],
                             context={"employer": r["employer"], "title": r["title"], "days": days,
                                      "submitted_at": to_iso(submitted)}))
    return out


def _recruiter(db: Database, now: datetime, days: int) -> list[_Proposal]:
    cutoff = to_iso(now - timedelta(days=days))
    rows = db.query_all(
        "SELECT em.id, em.subject, em.received_at, em.suggested_reply, ea.job_id, ea.application_id, "
        "j.employer, j.title FROM email_messages em "
        "LEFT JOIN email_associations ea ON ea.id = (SELECT id FROM email_associations x WHERE x.email_id = em.id "
        "  AND (x.confidence >= 0.8 OR x.confirmed_by_user = 1) "
        "  ORDER BY x.confirmed_by_user DESC, x.confidence DESC, x.id LIMIT 1) "
        "LEFT JOIN jobs j ON j.id = ea.job_id "
        "WHERE em.category = 'recruiter_message' AND em.requires_attention = 1 AND em.received_at IS NOT NULL "
        "AND em.received_at <= ?", (cutoff,))
    out = []
    for r in rows:
        received = _dt(r["received_at"]) or now
        out.append(_Proposal(T.RECRUITER_REPLY, f"{T.RECRUITER_REPLY.value}:email:{r['id']}",
                             received + timedelta(days=days), application_id=r["application_id"],
                             job_id=r["job_id"], email_id=r["id"],
                             context={"employer": r["employer"], "title": r["title"], "subject": r["subject"],
                                      "received_at": r["received_at"], "suggested_reply": r["suggested_reply"]}))
    return out


def _state_reminders(db: Database, now: datetime) -> list[_Proposal]:
    states = (*_INTERVIEW, S.ASSESSMENT_RECEIVED.value)
    ph, vals = _ph(states)
    rows = db.query_all(
        "SELECT a.id, a.job_id, a.status, a.status_updated_at, j.employer, j.title FROM applications a "
        f"JOIN jobs j ON j.id = a.job_id WHERE a.status IN ({ph})", vals)
    out = []
    for r in rows:
        ftype = T.ASSESSMENT_DUE if r["status"] == S.ASSESSMENT_RECEIVED.value else T.INTERVIEW_UPCOMING
        entered = r["status_updated_at"] or ""
        out.append(_Proposal(ftype, f"{ftype.value}:{r['id']}:{r['status']}:{entered}", _dt(entered) or now,
                             application_id=r["id"], job_id=r["job_id"],
                             context={"employer": r["employer"], "title": r["title"], "status": r["status"]}))
    return out


def _deadlines(db: Database, now: datetime) -> list[_Proposal]:
    ph, vals = _ph(_PRE_SUBMISSION)
    rows = db.query_all(
        "SELECT j.id AS job_id, j.closing_at, j.employer, j.title, a.id AS application_id "
        "FROM jobs j LEFT JOIN applications a ON a.job_id = j.id "
        "WHERE j.closing_at IS NOT NULL AND j.listing_status = 'active' AND ("
        "  (a.id IS NULL AND j.pipeline_state = 'shortlisted') OR "
        f"  (a.id IS NOT NULL AND a.submitted_at IS NULL AND a.status IN ({ph})))", vals)
    out = []
    for r in rows:
        closing = _dt(r["closing_at"])
        if closing is None or not (now < closing <= now + DEADLINE_WINDOW):
            continue
        out.append(_Proposal(T.DEADLINE, f"{T.DEADLINE.value}:job:{r['job_id']}:{r['closing_at']}", now,
                             application_id=r["application_id"], job_id=r["job_id"],
                             context={"employer": r["employer"], "title": r["title"],
                                      "closing_at": to_iso(closing)}))
    return out


def _stale(db: Database, now: datetime, days: int) -> list[_Proposal]:
    ph, vals = _ph(_NOT_STALE)
    rows = db.query_all(
        "SELECT a.id, a.job_id, a.last_activity_at, j.employer, j.title, "
        "(SELECT MAX(em.received_at) FROM email_associations ea JOIN email_messages em ON em.id = ea.email_id "
        " WHERE ea.application_id = a.id) AS last_email_at "
        f"FROM applications a JOIN jobs j ON j.id = a.job_id WHERE a.status NOT IN ({ph})", vals)
    out = []
    for r in rows:
        moments = [d for d in (_dt(r["last_activity_at"]), _dt(r["last_email_at"])) if d is not None]
        if not moments:
            continue
        last = max(moments)
        if last + timedelta(days=days) > now:
            continue
        out.append(_Proposal(T.STALE_APPLICATION, f"{T.STALE_APPLICATION.value}:{r['id']}:{to_iso(last)}",
                             last + timedelta(days=days), application_id=r["id"], job_id=r["job_id"],
                             context={"employer": r["employer"], "title": r["title"], "days": days}))
    return out


# --------------------------------------------------------------------------- #
# Creation
# --------------------------------------------------------------------------- #
def _open_duplicate(db: Database, p: _Proposal) -> bool:
    ph, vals = _ph(s.value for s in OPEN_STATUSES)
    if p.application_id is not None:
        where, params = "application_id = ?", [p.application_id]
    elif p.job_id is not None:
        where, params = "application_id IS NULL AND job_id = ?", [p.job_id]
    elif p.email_id is not None:
        where, params = "application_id IS NULL AND job_id IS NULL AND email_id = ?", [p.email_id]
    else:
        return False
    return bool(db.scalar(
        f"SELECT COUNT(*) FROM followups WHERE followup_type = ? AND {where} AND status IN ({ph})",
        [p.followup_type.value, *params, *vals]))


def _outreach_count(db: Database, application_id: int) -> int:
    ph, vals = _ph(t.value for t in OUTREACH_TYPES)
    return int(db.scalar(f"SELECT COUNT(*) FROM followups WHERE application_id = ? AND followup_type IN ({ph})",
                         [application_id, *vals]) or 0)


class _Drafter:
    def __init__(self, ctx: "AppContext", run: "RunInfo | None") -> None:
        self.ctx = ctx
        self.run = run
        self._fn: Callable[..., str] | None = None
        self._loaded = False
        self._failed = False
        try:
            self.candidate_name: str | None = ctx.profile.personal.full_name
        except JobAgentError:
            self.candidate_name = None

    def _llm(self) -> Callable[..., str] | None:
        if self._loaded:
            return None if self._failed else self._fn
        self._loaded = True
        config = self.ctx.config
        if not (config.llm.enabled and is_allowed(config, Permission.DRAFT_EMAILS)):
            return None
        try:
            from job_agent.llm.client import get_client
            from job_agent.llm.generation import draft_followup

            client = get_client(self.ctx)
        except ImportError:
            return None
        except JobAgentError as exc:
            self._fail(exc)
            return None
        if client is None:
            return None
        self._fn = lambda **kw: draft_followup(client, **kw)
        return self._fn

    def _fail(self, exc: BaseException) -> None:
        if not self._failed:
            self._failed = True
            repo.record_error(self.ctx.db, component="followups:llm", error=exc, task_name="followups",
                              run_id=self.run.run_id if self.run else None, retryable=False)

    def draft(self, p: _Proposal) -> str | None:
        ctx = {**p.context, "candidate_name": self.candidate_name}
        if p.followup_type == T.APPLICATION_NO_RESPONSE:
            fn = self._llm()
            if fn is not None:
                try:
                    text = llm_followup_draft(fn, ctx)
                except JobAgentError as exc:
                    self._fail(exc)
                    text = None
                if text:
                    return text
        if not is_allowed(self.ctx.config, Permission.DRAFT_EMAILS):
            return None
        return template_draft(p.followup_type, ctx)


def review_followups(ctx: "AppContext", now: datetime | None = None, *,
                     run: "RunInfo | None" = None) -> FollowupSummary:
    """Apply the follow-up rules; idempotent (safe to run any number of times)."""
    db = ctx.db
    cfg = ctx.config.followups
    now = now or utcnow()
    proposals = [
        *_no_response(db, now, cfg.application_no_response_days),
        *_recruiter(db, now, cfg.recruiter_reply_days),
        *_state_reminders(db, now),
        *_deadlines(db, now),
        *_stale(db, now, cfg.stale_application_days),
    ]
    summary = FollowupSummary()
    drafter = _Drafter(ctx, run)
    for p in proposals:
        if db.scalar("SELECT 1 FROM followups WHERE dedupe_key = ?", (p.dedupe_key,)) or _open_duplicate(db, p):
            summary.already_existing += 1
            continue
        if (p.followup_type in OUTREACH_TYPES and p.application_id is not None
                and _outreach_count(db, p.application_id) >= cfg.max_followups_per_application):
            summary.skipped_limit += 1
            continue
        draft = drafter.draft(p)
        stamp = to_iso(now)
        with db.transaction():
            cur = db.execute(
                "INSERT OR IGNORE INTO followups (application_id, job_id, email_id, followup_type, due_at, dedupe_key,"
                " suggested_action, draft_text, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (p.application_id, p.job_id, p.email_id, p.followup_type.value, to_iso(p.due_at), p.dedupe_key,
                 suggested_action(p.followup_type, p.context), draft,
                 (F.DRAFTED if draft else F.PENDING).value, stamp, stamp))
            if cur.rowcount != 1:
                summary.already_existing += 1
                continue
            if p.application_id is not None:
                db.execute("UPDATE applications SET follow_up_due_at = ? WHERE id = ? AND "
                           "(follow_up_due_at IS NULL OR follow_up_due_at > ?)",
                           (to_iso(p.due_at), p.application_id, to_iso(p.due_at)))
        summary.created += 1
        summary.by_type[p.followup_type.value] = summary.by_type.get(p.followup_type.value, 0) + 1
    ph, vals = _ph(s.value for s in OPEN_STATUSES)
    summary.due = int(db.scalar(f"SELECT COUNT(*) FROM followups WHERE status IN ({ph}) AND due_at <= ?",
                                [*vals, to_iso(now)]) or 0)
    if summary.created:
        repo.audit(db, actor="system", action="followups_reviewed", outcome="success",
                   run_id=run.run_id if run else None, details=asdict(summary))
    return summary


def followups_task(ctx: "AppContext", run: "RunInfo") -> dict[str, Any]:
    summary = asdict(review_followups(ctx, run=run))
    summary["finished_at"] = utcnow_iso()
    return summary


# --------------------------------------------------------------------------- #
# Queries and user actions
# --------------------------------------------------------------------------- #
_LIST_SQL = (
    "SELECT f.*, j.job_uid, j.employer, j.title, a.application_uid, a.status AS application_status, "
    "em.subject AS email_subject FROM followups f LEFT JOIN jobs j ON j.id = f.job_id "
    "LEFT JOIN applications a ON a.id = f.application_id LEFT JOIN email_messages em ON em.id = f.email_id "
)


def list_followups(db: Database, status: FollowupStatus | str | Iterable[FollowupStatus | str] | None = None,
                   *, limit: int = 200) -> list[sqlite3.Row]:
    """Follow-ups (soonest due first). ``status="open"`` = pending/drafted/awaiting/approved."""
    if status is None:
        return db.query_all(_LIST_SQL + "ORDER BY f.due_at, f.id LIMIT ?", (limit,))
    if isinstance(status, str) and status == "open":
        statuses = [s.value for s in OPEN_STATUSES]
    elif isinstance(status, (str, FollowupStatus)):
        statuses = [FollowupStatus(status).value]
    else:
        statuses = [FollowupStatus(s).value for s in status]
    ph, vals = _ph(statuses)
    return db.query_all(_LIST_SQL + f"WHERE f.status IN ({ph}) ORDER BY f.due_at, f.id LIMIT ?", [*vals, limit])


def get_followup(db: Database, followup_id: int) -> sqlite3.Row | None:
    return db.query_one(_LIST_SQL + "WHERE f.id = ?", (int(followup_id),))


def set_followup_status(ctx: "AppContext", followup_id: int, status: FollowupStatus, *, actor: str) -> sqlite3.Row:
    """User decision on a follow-up (pending/drafted -> approved/completed/dismissed; approved -> completed/dismissed).

    "approved" only means *approved for you to send manually*; nothing is sent.
    """
    if actor not in HUMAN_ACTORS:
        raise PermissionDenied("follow-up status can only be changed by the user (cli or dashboard)")
    target = FollowupStatus(status)
    db = ctx.db
    with db.transaction():
        row = db.query_one("SELECT * FROM followups WHERE id = ?", (int(followup_id),))
        if row is None:
            raise PermanentError(f"follow-up {followup_id} not found")
        current = FollowupStatus(row["status"])
        if current == target:
            return get_followup(db, int(followup_id))  # type: ignore[return-value]
        if target not in USER_TRANSITIONS.get(current, frozenset()):
            raise InvalidTransition(f"follow-up {followup_id}: {current.value} -> {target.value} is not allowed")
        now = utcnow_iso()
        values: dict[str, Any] = {"status": target.value, "updated_at": now}
        if target == F.COMPLETED:
            values["completed_at"] = now
        db.update("followups", int(followup_id), values)
        ftype = row["followup_type"]
        if row["email_id"] is not None and ftype == T.RECRUITER_REPLY.value and target in {F.COMPLETED, F.DISMISSED}:
            db.execute("UPDATE email_messages SET requires_attention = 0 WHERE id = ?", (row["email_id"],))
        if target == F.COMPLETED and row["application_id"] is not None:
            repo.add_application_event(db, int(row["application_id"]), "followup_completed", actor="user",
                                       details={"followup_id": int(followup_id), "followup_type": ftype})
        if row["application_id"] is not None and target in {F.COMPLETED, F.DISMISSED}:
            next_due = db.scalar(
                "SELECT MIN(due_at) FROM followups WHERE application_id = ? AND status IN "
                "('pending','drafted','awaiting_approval','approved')", (row["application_id"],))
            db.execute("UPDATE applications SET follow_up_due_at = ? WHERE id = ?", (next_due, row["application_id"]))
        repo.audit(db, actor=actor, action="followup_status_changed", entity_type="followup", entity_id=followup_id,
                   outcome="success", details={"from": current.value, "to": target.value, "type": ftype})
    return get_followup(db, int(followup_id))  # type: ignore[return-value]
