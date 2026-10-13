"""Follow-up rules, idempotency, per-application limits and user status transitions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from job_agent.database import repo
from job_agent.errors import InvalidTransition, PermanentError, PermissionDenied
from job_agent.followups.service import (
    get_followup,
    list_followups,
    review_followups,
    set_followup_status,
)
from job_agent.models import FollowupStatus, PipelineState, RunStatus
from job_agent.utils.time import to_iso
from test_email_fakes import make_application

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
F = FollowupStatus


def ago(days: float = 0, hours: float = 0) -> str:
    return to_iso(NOW - timedelta(days=days, hours=hours))


def _rows(db, ftype=None):
    if ftype:
        return db.query_all("SELECT * FROM followups WHERE followup_type = ? ORDER BY id", (ftype,))
    return db.query_all("SELECT * FROM followups ORDER BY id")


def _submitted_app(db, make_job, *, days_ago: float = 10, status: str = "submitted", **job):
    job_id = make_job(**job)
    return make_application(db, job_id, status, submitted_at=ago(days_ago), status_updated_at=ago(days_ago),
                            last_activity_at=ago(days_ago))


def _email(db, *, category: str, received: str, attention: int = 1, reply: str | None = None) -> int:
    return db.insert("email_messages", {
        "gmail_message_id": f"g-{category}-{received}", "gmail_thread_id": "t", "received_at": received,
        "from_address": "someone@example.org", "from_domain": "example.org", "subject": f"{category} subject",
        "category": category, "category_confidence": 0.9, "classifier": "rules", "requires_attention": attention,
        "suggested_reply": reply, "processed_at": received})


def _link(db, email_id, job_id, app_id, confidence=0.9):
    db.insert("email_associations", {"email_id": email_id, "job_id": job_id, "application_id": app_id,
                                      "method": "domain", "confidence": confidence, "created_at": ago()})


# ---------------------------------------------------------- no response ---- #
def test_no_response_followup_created_once_with_draft(ctx, make_job):
    db = ctx.db
    app_id = _submitted_app(db, make_job, days_ago=10)
    s1 = review_followups(ctx, now=NOW)
    assert s1.created == 1 and s1.by_type == {"application_no_response": 1}
    row = _rows(db, "application_no_response")[0]
    assert row["application_id"] == app_id and row["status"] == "drafted"
    assert row["dedupe_key"] == f"application_no_response:{app_id}:1"
    assert "Business Analyst" in row["draft_text"] and "Acme Analytics" in row["draft_text"]
    assert "Test Candidate" in row["draft_text"]  # name from the verified profile
    assert row["due_at"] == ago(2)  # submitted 10 days ago + 8 days
    assert repo.get_application(db, app_id)["follow_up_due_at"] == ago(2)

    s2 = review_followups(ctx, now=NOW)
    assert s2.created == 0 and s2.already_existing == 1 and s2.due == 1
    assert len(_rows(db)) == 1


def test_no_response_not_due_yet(ctx, make_job):
    _submitted_app(ctx.db, make_job, days_ago=3)
    assert review_followups(ctx, now=NOW).created == 0


def test_employer_reply_suppresses_no_response_but_confirmation_does_not(ctx, make_job):
    db = ctx.db
    a1 = _submitted_app(db, make_job, days_ago=10)
    a2 = _submitted_app(db, make_job, days_ago=10, status="confirmation_received", employer="Globex",
                        employer_normalized="globex")
    j1 = repo.get_application(db, a1)["job_id"]
    j2 = repo.get_application(db, a2)["job_id"]
    _link(db, _email(db, category="recruiter_message", received=ago(5), attention=0), j1, a1)
    _link(db, _email(db, category="application_confirmation", received=ago(9), attention=0), j2, a2)
    review_followups(ctx, now=NOW)
    apps = {r["application_id"] for r in _rows(db, "application_no_response")}
    assert apps == {a2}


def test_max_followups_per_application_and_one_open_per_type(ctx, make_job):
    db = ctx.db
    ctx.config.followups.max_followups_per_application = 2
    app_id = _submitted_app(db, make_job, days_ago=10)
    review_followups(ctx, now=NOW)
    first = _rows(db, "application_no_response")[0]
    # while it is open, later reviews never add a second one of the same type
    assert "application_no_response" not in review_followups(ctx, now=NOW + timedelta(days=5)).by_type

    set_followup_status(ctx, first["id"], F.COMPLETED, actor="cli")
    # The status change is stamped with the real clock; pin it to the test clock.
    db.execute("UPDATE followups SET completed_at = ?, updated_at = ? WHERE id = ?",
               (to_iso(NOW), to_iso(NOW), first["id"]))
    later = NOW + timedelta(days=9)
    s = review_followups(ctx, now=later)
    assert s.by_type.get("application_no_response") == 1
    second = _rows(db, "application_no_response")[1]
    assert second["dedupe_key"] == f"application_no_response:{app_id}:2"

    set_followup_status(ctx, second["id"], F.DISMISSED, actor="cli")
    s3 = review_followups(ctx, now=later + timedelta(days=30))
    assert s3.skipped_limit == 1
    assert len(_rows(db, "application_no_response")) == 2


def test_zero_max_disables_outreach(ctx, make_job):
    ctx.config.followups.max_followups_per_application = 0
    _submitted_app(ctx.db, make_job, days_ago=10)
    s = review_followups(ctx, now=NOW)
    assert s.skipped_limit == 1 and not _rows(ctx.db, "application_no_response")


# ------------------------------------------------------------ recruiter ---- #
def test_recruiter_reply_followup_and_done_clears_attention(ctx):
    db = ctx.db
    reply = "Hi [recruiter name],\n\nThank you for reaching out. [your availability]\n\nBest,\nTest Candidate"
    old = _email(db, category="recruiter_message", received=ago(3), reply=reply)
    _email(db, category="recruiter_message", received=ago(hours=5), reply=reply)  # too recent
    _email(db, category="recruiter_message", received=ago(4), attention=0)  # already handled
    review_followups(ctx, now=NOW)
    rows = _rows(db, "recruiter_reply")
    assert len(rows) == 1 and rows[0]["email_id"] == old
    assert rows[0]["draft_text"] == reply and rows[0]["status"] == "drafted"
    assert rows[0]["dedupe_key"] == f"recruiter_reply:email:{old}"
    set_followup_status(ctx, rows[0]["id"], F.COMPLETED, actor="dashboard")
    assert db.scalar("SELECT requires_attention FROM email_messages WHERE id = ?", (old,)) == 0
    assert review_followups(ctx, now=NOW).created == 0


# ------------------------------------------------- reminders by status ---- #
def test_interview_and_assessment_reminders_once_per_state(ctx, make_job):
    db = ctx.db
    a1 = make_application(db, make_job(), "interview_requested", status_updated_at=ago(1), last_activity_at=ago(1))
    a2 = make_application(db, make_job(employer="Hooli", employer_normalized="hooli"), "assessment_received",
                          status_updated_at=ago(1), last_activity_at=ago(1))
    s = review_followups(ctx, now=NOW)
    assert s.by_type == {"interview_upcoming": 1, "assessment_due": 1}
    interview = _rows(db, "interview_upcoming")[0]
    assert interview["application_id"] == a1 and interview["status"] == "pending" and interview["draft_text"] is None
    assert "nothing is accepted automatically" in interview["suggested_action"]
    assert _rows(db, "assessment_due")[0]["application_id"] == a2
    assert review_followups(ctx, now=NOW).created == 0

    # entering a new interview state later creates a fresh reminder (after the old one is handled)
    set_followup_status(ctx, interview["id"], F.COMPLETED, actor="cli")
    repo.transition_application(db, a1, PipelineState.INTERVIEW_SCHEDULED, actor="user")
    s2 = review_followups(ctx, now=NOW + timedelta(hours=1))
    assert s2.by_type.get("interview_upcoming") == 1


# ------------------------------------------------------------- deadline ---- #
def test_deadline_reminders_for_shortlisted_and_unsubmitted(ctx, make_job):
    db = ctx.db
    soon = to_iso(NOW + timedelta(days=2))
    later = to_iso(NOW + timedelta(days=5))
    j_short = make_job(pipeline_state="shortlisted", closing_at=soon)
    make_job(pipeline_state="shortlisted", closing_at=later)  # outside the 3-day window
    j_app = make_job(pipeline_state="shortlisted", closing_at=soon, employer="Globex", employer_normalized="globex")
    app_id = make_application(db, j_app, "documents_ready", last_activity_at=ago(1))
    j_done = make_job(pipeline_state="shortlisted", closing_at=soon, employer="Initech", employer_normalized="initech")
    make_application(db, j_done, "submitted", submitted_at=ago(1), last_activity_at=ago(1))
    make_job(pipeline_state="scored", closing_at=soon)  # not shortlisted
    s = review_followups(ctx, now=NOW)
    rows = _rows(db, "deadline")
    assert s.by_type.get("deadline") == 2
    assert {(r["job_id"], r["application_id"]) for r in rows} == {(j_short, None), (j_app, app_id)}
    assert review_followups(ctx, now=NOW).created == 0


# ---------------------------------------------------------------- stale ---- #
def test_stale_application_suggests_closing(ctx, make_job):
    db = ctx.db
    app_id = make_application(db, make_job(), "documents_ready", last_activity_at=ago(40))
    make_application(db, make_job(employer="X", employer_normalized="x"), "rejected", last_activity_at=ago(90))
    s = review_followups(ctx, now=NOW)
    rows = _rows(db, "stale_application")
    assert s.by_type.get("stale_application") == 1 and rows[0]["application_id"] == app_id
    assert "closed" in rows[0]["suggested_action"]
    assert review_followups(ctx, now=NOW + timedelta(days=1)).created == 0


# ------------------------------------------------------ status changes ---- #
def test_set_followup_status_transitions_and_audit(ctx, make_job):
    db = ctx.db
    _submitted_app(db, make_job, days_ago=10)
    review_followups(ctx, now=NOW)
    fid = _rows(db)[0]["id"]

    row = set_followup_status(ctx, fid, F.APPROVED, actor="dashboard")
    assert row["status"] == "approved"
    with pytest.raises(InvalidTransition):
        set_followup_status(ctx, fid, F.PENDING, actor="cli")
    with pytest.raises(PermissionDenied):
        set_followup_status(ctx, fid, F.COMPLETED, actor="system")
    with pytest.raises(PermanentError):
        set_followup_status(ctx, 9999, F.COMPLETED, actor="cli")
    row = set_followup_status(ctx, fid, F.COMPLETED, actor="cli")
    assert row["status"] == "completed" and row["completed_at"]
    with pytest.raises(InvalidTransition):
        set_followup_status(ctx, fid, F.DISMISSED, actor="cli")
    assert set_followup_status(ctx, fid, "completed", actor="cli")["status"] == "completed"  # no-op
    audits = db.query_all("SELECT * FROM audit_events WHERE action = 'followup_status_changed' ORDER BY id")
    assert [a["actor"] for a in audits] == ["dashboard", "cli"]
    event = db.query_one("SELECT * FROM application_events WHERE event_type = 'followup_completed'")
    assert event is not None and event["actor"] == "user"
    # nothing was "sent": approving never creates approval requests or outgoing mail
    assert db.scalar("SELECT COUNT(*) FROM approval_requests") == 0


def test_list_followups_filters(ctx, make_job):
    db = ctx.db
    _submitted_app(db, make_job, days_ago=10)
    make_application(db, make_job(employer="Hooli", employer_normalized="hooli"), "assessment_received",
                     status_updated_at=ago(1), last_activity_at=ago(1))
    review_followups(ctx, now=NOW)
    assert len(list_followups(db)) == 2
    assert len(list_followups(db, "open")) == 2
    assert [r["followup_type"] for r in list_followups(db, F.DRAFTED)] == ["application_no_response"]
    assert len(list_followups(db, [F.PENDING, F.DRAFTED])) == 2
    assert list_followups(db, "completed") == []
    fid = list_followups(db, F.PENDING)[0]["id"]
    assert get_followup(db, fid)["employer"] == "Hooli"
    with pytest.raises(ValueError):
        list_followups(db, "bogus")


def test_draft_emails_permission_off_means_no_drafts(ctx, make_job):
    ctx.config.permissions.draft_emails = False
    _submitted_app(ctx.db, make_job, days_ago=10)
    review_followups(ctx, now=NOW)
    row = _rows(ctx.db)[0]
    assert row["draft_text"] is None and row["status"] == "pending"


def test_unsafe_llm_followup_draft_is_rejected(ctx, make_job, monkeypatch):
    import sys
    import types

    ctx.config.llm.enabled = True
    client_mod = types.ModuleType("job_agent.llm.client")
    client_mod.get_client = lambda c: object()
    gen_mod = types.ModuleType("job_agent.llm.generation")
    gen_mod.draft_followup = lambda client, **kw: "I accept your offer and my salary expectation is $120,000."
    monkeypatch.setitem(sys.modules, "job_agent.llm.client", client_mod)
    monkeypatch.setitem(sys.modules, "job_agent.llm.generation", gen_mod)
    _submitted_app(ctx.db, make_job, days_ago=10)
    review_followups(ctx, now=NOW)
    draft = _rows(ctx.db)[0]["draft_text"]
    assert "salary" not in draft and "Following up on my application" in draft  # template fallback


def test_followups_task_via_runner(ctx, make_job):
    from job_agent.tasks import run_task

    _submitted_app(ctx.db, make_job, days_ago=30)
    result = run_task(ctx, "followups", trigger="manual")
    assert result.status == RunStatus.SUCCEEDED, result.error
    assert result.summary["created"] >= 1
