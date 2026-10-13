"""Gmail sync end-to-end with a fake service: dedupe, storage, association, evidence-based status, safety."""

from __future__ import annotations

import sys
import types

import pytest

from job_agent.database import repo
from job_agent.database.connection import from_json
from job_agent.email import service as email_service
from job_agent.email.gmail_client import GmailReadClient
from job_agent.email.service import associate_email, mark_email_handled, sync_gmail
from job_agent.errors import PermanentError, RecoverableError
from job_agent.models import ApprovalType, EmailCategory, EmailClassification, PipelineState, RunStatus
from test_email_fakes import build_message, enable_gmail, fake_service, http_error, make_application, spec

S = PipelineState


@pytest.fixture
def gctx(ctx):
    enable_gmail(ctx)
    return ctx


def _email(db, gmail_id):
    return db.query_one("SELECT * FROM email_messages WHERE gmail_message_id = ?", (gmail_id,))


def _assoc(db, email_id):
    return db.query_all("SELECT * FROM email_associations WHERE email_id = ? ORDER BY confidence DESC", (email_id,))


def _acme(make_job, **kw):
    values = dict(employer="Acme Analytics", employer_normalized="acme analytics", title="Business Analyst",
                  title_normalized="business analyst", source_name="greenhouse",
                  job_url="https://boards.greenhouse.io/acmeanalytics/jobs/101")
    values.update(kw)
    return make_job(**values)


def _submitted_via_approval(db, job_id):
    """DOCUMENTS_PENDING -> ... -> SUBMISSION_ATTEMPTED (approved) -> NEEDS_MANUAL_ACTION (unknown outcome)."""
    app_id = repo.create_application(db, job_id, actor="system")
    repo.transition_application(db, app_id, S.DOCUMENTS_READY, actor="system")
    repo.transition_application(db, app_id, S.AWAITING_APPROVAL, actor="system")
    payload = {"employer": "Acme Analytics", "role": "Business Analyst"}
    uid = repo.create_approval(db, ApprovalType.SUBMIT_APPLICATION, payload, summary="submit", actor="system",
                               application_id=app_id)
    repo.decide_approval(db, uid, approve=True, actor="user")
    repo.transition_application(db, app_id, S.SUBMISSION_ATTEMPTED, actor="browser", approval_uid=uid,
                                approved_payload=payload)
    repo.transition_application(db, app_id, S.NEEDS_MANUAL_ACTION, actor="browser",
                                details={"reason": "no confirmation page detected"})
    return app_id


# ------------------------------------------------------------- storage ---- #
def test_sync_stores_metadata_and_dedupes_before_fetching(gctx):
    db = gctx.db
    svc = fake_service(page_size=4)
    first = sync_gmail(gctx, service=svc)
    assert (first.listed, first.fetched, first.new, first.skipped_existing) == (9, 9, 9, 0)
    assert first.errors == [] and not first.aborted
    assert first.by_category == {c: 1 for c in (
        "application_confirmation", "recruiter_message", "interview_invitation", "scheduling_request",
        "assessment", "rejection", "offer", "follow_up_opportunity", "other")}
    assert svc.list_calls[0]["q"].endswith("-in:sent -in:drafts -in:chats")
    assert len(svc.get_calls) == 9

    second = sync_gmail(gctx, service=svc)
    assert (second.fetched, second.new, second.skipped_existing) == (0, 0, 9)
    assert len(svc.get_calls) == 9  # known ids were never re-downloaded
    assert db.scalar("SELECT COUNT(*) FROM email_messages") == 9

    svc.add(build_message(spec("s-domain-reject-1")))
    third = sync_gmail(gctx, service=svc)
    assert (third.fetched, third.new, third.skipped_existing) == (1, 1, 9)
    assert db.scalar("SELECT COUNT(*) FROM email_messages") == 10
    assert repo.get_state(db, "gmail_last_success_at")


def test_bodies_are_never_stored(gctx):
    db = gctx.db
    sync_gmail(gctx, service=fake_service())
    dump = "\n".join(db.conn.iterdump())
    assert "BODYMARKER-c0nf1rm-7f3a" not in dump
    assert "We have received your application and our team will review" not in dump
    for row in db.query_all("SELECT snippet, summary, subject FROM email_messages"):
        assert row["snippet"] is None or len(row["snippet"]) <= 200
        assert row["summary"] is None or len(row["summary"]) <= 300


def test_store_snippets_false_keeps_metadata_only(gctx):
    gctx.config.gmail.store_snippets = False
    sync_gmail(gctx, service=fake_service())
    assert gctx.db.scalar("SELECT COUNT(*) FROM email_messages WHERE snippet IS NOT NULL") == 0
    row = _email(gctx.db, "m-recruiter-1")
    assert row["summary"].startswith("Recruiter message from Jordan Lee: ")


def test_metadata_scope_lists_inbox_without_query(gctx):
    gctx.config.gmail.scopes = ["https://www.googleapis.com/auth/gmail.metadata"]
    svc = fake_service("m-recruiter-1")
    sync_gmail(gctx, service=svc)
    assert "q" not in svc.list_calls[0] and svc.list_calls[0]["labelIds"] == ["INBOX"]
    assert svc.get_calls[0]["format"] == "metadata"


# ------------------------------------------------------ status updates ---- #
SUBMITTED_AT = "2026-10-01T00:00:00+00:00"


def test_confirmation_email_flags_unknown_outcome_for_you_to_confirm(gctx, make_job):
    """An unknown submission outcome is never resolved by email automatically; you confirm it."""
    db = gctx.db
    job_id = _acme(make_job)
    app_id = _submitted_via_approval(db, job_id)
    assert repo.get_application(db, app_id)["status"] == "needs_manual_action"
    events_before = db.scalar("SELECT COUNT(*) FROM application_events WHERE application_id = ?", (app_id,))

    result = sync_gmail(gctx, service=fake_service("m-confirm-1"))
    assert result.status_updates == 0 and result.associated == 1 and result.attention == 1
    assert repo.get_application(db, app_id)["status"] == "needs_manual_action"
    assert db.scalar("SELECT COUNT(*) FROM application_events WHERE application_id = ?", (app_id,)) == events_before
    email = _email(db, "m-confirm-1")
    assoc = _assoc(db, email["id"])[0]
    assert assoc["application_id"] == app_id and assoc["confidence"] >= 0.8  # the link itself is trusted
    assert assoc["status_applied"] is None and email["requires_attention"] == 1
    skipped = db.query_one("SELECT * FROM audit_events WHERE action = 'email_status_update_skipped'")
    assert "unknown submission outcome" in from_json(skipped["details"])["reason"]

    # You confirm it: the email becomes the evidence.
    app_uid = repo.get_application(db, app_id)["application_uid"]
    res = associate_email(gctx, email["id"], application_ref=app_uid, apply_status=True)
    assert res.status_applied == "confirmation_received" and res.previous_status == "needs_manual_action"
    app = repo.get_application(db, app_id)
    assert app["status"] == "confirmation_received" and app["confirmation_status"] == "email_confirmed"
    event = db.query_one("SELECT * FROM application_events WHERE application_id = ? ORDER BY id DESC LIMIT 1",
                         (app_id,))
    assert event["actor"] == "user" and event["from_status"] == "needs_manual_action"
    evidence = from_json(event["evidence"])
    assert evidence["type"] == "email" and evidence["gmail_message_id"] == "m-confirm-1"
    assert evidence["category"] == "application_confirmation" and evidence["received_at"]
    assert _email(db, "m-confirm-1")["requires_attention"] == 0


def test_confirmation_from_submission_attempted(gctx, make_job):
    db = gctx.db
    app_id = make_application(db, _acme(make_job), "submission_attempted")
    db.update("applications", app_id, {"submission_attempts": 1})
    result = sync_gmail(gctx, service=fake_service("m-confirm-1"))
    assert result.status_updates == 1
    app = repo.get_application(db, app_id)
    assert app["status"] == "confirmation_received" and app["confirmation_status"] == "email_confirmed"
    event = db.query_one("SELECT * FROM application_events WHERE application_id = ? ORDER BY id DESC LIMIT 1",
                         (app_id,))
    assert event["actor"] == "email" and from_json(event["evidence"])["gmail_message_id"] == "m-confirm-1"
    assert _email(db, "m-confirm-1")["requires_attention"] == 0


def test_rejection_by_sender_domain(gctx, make_job):
    db = gctx.db
    job_id = make_job(employer="Umbrella Health", employer_normalized="umbrella health",
                      job_url="https://careers.umbrellahealth.com/jobs/77")
    app_id = make_application(db, job_id, "submitted", submitted_at=SUBMITTED_AT)
    sync_gmail(gctx, service=fake_service("s-domain-reject-1"))
    assert repo.get_application(db, app_id)["status"] == "rejected"
    assoc = _assoc(db, _email(db, "s-domain-reject-1")["id"])[0]
    assert assoc["method"] == "domain" and assoc["confidence"] >= 0.8


def test_rejection_naming_no_role_waits_for_you(gctx, make_job):
    """Even from the job's own domain, an email that does not name the role is only flagged for you."""
    db = gctx.db
    job_id = make_job(employer="Umbrella Health", employer_normalized="umbrella health",
                      job_url="https://careers.umbrellahealth.com/jobs/77")
    app_id = make_application(db, job_id, "submitted", submitted_at=SUBMITTED_AT)
    result = sync_gmail(gctx, service=fake_service("s-domain-reject-untitled"))
    assert result.status_updates == 0 and repo.get_application(db, app_id)["status"] == "submitted"
    email = _email(db, "s-domain-reject-untitled")
    assoc = _assoc(db, email["id"])[0]
    assert assoc["method"] == "domain" and assoc["confidence"] < 0.8 and email["requires_attention"] == 1
    review = db.query_one("SELECT details FROM audit_events WHERE action = 'email_status_needs_review'")
    assert "does not name this application's role" in " ".join(from_json(review["details"])["reasons"])
    res = associate_email(gctx, email["id"], application_ref=repo.get_application(db, app_id)["application_uid"],
                          apply_status=True)
    assert res.status_applied == "rejected"


def test_thread_association_and_own_messages_ignored(gctx, make_job):
    db = gctx.db
    job_id = make_job(employer="Initech", employer_normalized="initech", title="Data Analyst",
                      title_normalized="data analyst", job_url="https://careers.initech.com/jobs/da-1")
    app_id = make_application(db, job_id, "submitted", submitted_at=SUBMITTED_AT)
    result = sync_gmail(gctx, service=fake_service("s-thread-first", "s-thread-reply", "s-thread-reply-same",
                                                   "s-sent-1"))
    assert repo.get_application(db, app_id)["status"] == "interview_requested"
    assert result.status_updates == 1
    # A reply from the employer's own domain inherits the thread's link.
    same = _assoc(db, _email(db, "s-thread-reply-same")["id"])
    assert same and same[0]["method"] == "thread" and same[0]["application_id"] == app_id
    assert same[0]["confidence"] >= 0.8 and same[0]["status_applied"] is None  # already interview_requested
    # A message from another domain (here a personal gmail.com address) in the thread is only a suggestion.
    reply = _email(db, "s-thread-reply")
    assoc = _assoc(db, reply["id"])
    assert assoc and assoc[0]["method"] == "thread_other_sender" and assoc[0]["application_id"] == app_id
    assert assoc[0]["confidence"] <= 0.6 and assoc[0]["status_applied"] is None
    assert reply["requires_attention"] == 1
    sent = _email(db, "s-sent-1")
    assert sent["classifier"] == "skipped:own_message" and sent["requires_attention"] == 0
    assert _assoc(db, sent["id"]) == []
    # interviews are never accepted: the suggested reply only offers placeholders
    first = _email(db, "s-thread-first")
    assert first["requires_attention"] == 1 and "[your availability]" in first["suggested_reply"]


def test_disallowed_transition_is_skipped_and_recorded(gctx, make_job):
    db = gctx.db
    job_id = make_job(employer="Umbrella Health", employer_normalized="umbrella health",
                      job_url="https://umbrellahealth.com/careers/1")
    app_id = make_application(db, job_id, "withdrawn", submitted_at=SUBMITTED_AT)
    result = sync_gmail(gctx, service=fake_service("s-domain-reject-1"))
    assert result.status_updates == 0 and result.errors == []
    assert repo.get_application(db, app_id)["status"] == "withdrawn"
    audit = db.query_one("SELECT * FROM audit_events WHERE action = 'email_status_update_skipped'")
    assert audit is not None and audit["entity_id"] == str(app_id)
    assert "not an allowed transition" in from_json(audit["details"])["reason"]
    email = _email(db, "s-domain-reject-1")
    assert email["requires_attention"] == 1
    review = db.query_one("SELECT * FROM audit_events WHERE action = 'email_status_needs_review'")
    assert review["entity_id"] == str(email["id"])
    assert "not an allowed transition" in from_json(review["details"])["reasons"][0]


def test_never_submitted_application_is_not_changed_by_email(gctx, make_job):
    db = gctx.db
    job_id = make_job(employer="Umbrella Health", employer_normalized="umbrella health",
                      job_url="https://umbrellahealth.com/careers/1")
    app_id = make_application(db, job_id, "awaiting_approval")
    result = sync_gmail(gctx, service=fake_service("s-domain-reject-1"))
    assert result.status_updates == 0 and result.errors == []
    assert repo.get_application(db, app_id)["status"] == "awaiting_approval"
    audit = db.query_one("SELECT * FROM audit_events WHERE action = 'email_status_update_skipped'")
    assert "no record that this application was submitted" in from_json(audit["details"])["reason"]
    assert _email(db, "s-domain-reject-1")["requires_attention"] == 1


def test_low_confidence_only_flags_attention_then_user_confirms(gctx, make_job):
    db = gctx.db
    app_id = make_application(db, _acme(make_job), "submitted")
    events_before = db.scalar("SELECT COUNT(*) FROM application_events WHERE application_id = ?", (app_id,))
    result = sync_gmail(gctx, service=fake_service("s-lowconf-reject-1"))
    assert result.status_updates == 0 and result.attention == 1
    email = _email(db, "s-lowconf-reject-1")
    assert email["category"] == "rejection" and email["requires_attention"] == 1
    assoc = _assoc(db, email["id"])
    assert assoc and assoc[0]["confidence"] < 0.8 and assoc[0]["status_applied"] is None
    assert repo.get_application(db, app_id)["status"] == "submitted"
    assert db.scalar("SELECT COUNT(*) FROM application_events WHERE application_id = ?", (app_id,)) == events_before

    app_uid = repo.get_application(db, app_id)["application_uid"]
    res = associate_email(gctx, email["id"], application_ref=app_uid, apply_status=True)
    assert res.status_applied == "rejected" and res.previous_status == "submitted"
    assert repo.get_application(db, app_id)["status"] == "rejected"
    rows = _assoc(db, email["id"])
    assert len(rows) == 1 and rows[0]["confirmed_by_user"] == 1 and rows[0]["method"] == "user"
    assert rows[0]["confidence"] == 1.0 and rows[0]["status_applied"] == "rejected"
    event = db.query_one("SELECT * FROM application_events WHERE application_id = ? ORDER BY id DESC LIMIT 1",
                         (app_id,))
    assert event["actor"] == "user" and from_json(event["evidence"])["type"] == "email"
    assert _email(db, "s-lowconf-reject-1")["requires_attention"] == 0
    # applying twice is refused, not repeated
    again = associate_email(gctx, email["id"], application_ref=app_uid, apply_status=True)
    assert again.status_applied is None and "already applied" in again.skipped_reason


def test_injection_email_changes_nothing_beyond_classification_and_flag(gctx, make_job):
    db = gctx.db
    job_id = _acme(make_job, job_url="https://acmeanalytics.com/careers/ba-101")
    app_id = make_application(db, job_id, "submitted")
    events_before = db.scalar("SELECT COUNT(*) FROM application_events")
    audits_before = db.scalar("SELECT COUNT(*) FROM audit_events WHERE action = 'application_status_changed'")
    svc = fake_service("s-injection-1")
    result = sync_gmail(gctx, service=svc)

    email = _email(db, "s-injection-1")
    assert email["category"] == "offer" and email["injection_suspected"] == 1
    assert email["requires_attention"] == 1 and email["suggested_reply"] is None
    assert result.injection_flagged == 1 and result.status_updates == 0
    assert repo.get_application(db, app_id)["status"] == "submitted"
    assert db.scalar("SELECT COUNT(*) FROM application_events") == events_before
    assert db.scalar("SELECT COUNT(*) FROM audit_events WHERE action = 'application_status_changed'") == audits_before
    assert db.scalar("SELECT COUNT(*) FROM approval_requests") == 0
    assert db.scalar("SELECT COUNT(*) FROM followups") == 0
    assert all(a["status_applied"] is None for a in _assoc(db, email["id"]))
    # only read calls ever reached the (fake) Gmail service
    assert len(svc.list_calls) == 1 and [c["id"] for c in svc.get_calls] == ["s-injection-1"]


def test_llm_label_alone_never_changes_status(gctx, make_job, monkeypatch):
    db = gctx.db
    app_id = make_application(db, _acme(make_job, job_url="https://acmeanalytics.com/careers/1"), "submitted")
    s = spec("s-injection-1")
    s.update(id="s-vague-1", threadId="t-vague-1", subject="Next steps",
             text="Please see the attached document regarding your candidacy.", snippet="Please see the attached")
    llm_result = EmailClassification(category=EmailCategory.OFFER, confidence=0.99, summary="Offer (per LLM)",
                                     classifier="llm:fake")
    monkeypatch.setattr(email_service._Processor, "llm_classify", lambda self, parsed: llm_result)
    sync_gmail(gctx, service=fake_service().__class__([build_message(s)]))
    email = _email(db, "s-vague-1")
    assert email["category"] == "offer" and email["classifier"] == "llm:fake"
    assert email["requires_attention"] == 1
    assert repo.get_application(db, app_id)["status"] == "submitted"


def test_llm_module_missing_or_failing_degrades_to_rules(gctx, monkeypatch):
    gctx.config.llm.enabled = True
    monkeypatch.setitem(sys.modules, "job_agent.llm.client", None)  # import -> ImportError
    result = sync_gmail(gctx, service=fake_service())
    assert result.new == 9 and result.errors == []
    assert gctx.db.scalar("SELECT COUNT(*) FROM email_messages WHERE classifier != 'rules'") == 0

    client_mod = types.ModuleType("job_agent.llm.client")
    client_mod.get_client = lambda ctx: object()
    gen_mod = types.ModuleType("job_agent.llm.generation")

    def down(*args, **kwargs):
        raise RecoverableError("ollama not reachable")

    gen_mod.classify_email = down
    gen_mod.draft_email_reply = down
    monkeypatch.setitem(sys.modules, "job_agent.llm.client", client_mod)
    monkeypatch.setitem(sys.modules, "job_agent.llm.generation", gen_mod)
    vague = spec("m-recruiter-1")
    vague.update(id="v-1", threadId="v-1", subject="hello", text="see attached", snippet="")
    vague2 = dict(vague, id="v-2", threadId="v-2")
    res = sync_gmail(gctx, service=fake_service().__class__([build_message(vague), build_message(vague2)]))
    assert res.new == 2 and res.errors == []
    errors = gctx.db.query_all("SELECT * FROM errors WHERE component = 'email:llm'")
    assert len(errors) == 1  # recorded once per run, then rules-only


# ---------------------------------------------------- partial failures ---- #
def test_permanent_get_error_is_recorded_and_sync_continues(gctx):
    svc = fake_service("m-confirm-1", "m-recruiter-1", get_failures={"m-confirm-1": [http_error(404)]})
    result = sync_gmail(gctx, service=svc)
    assert result.new == 1 and len(result.errors) == 1 and not result.aborted
    assert gctx.db.scalar("SELECT COUNT(*) FROM errors WHERE component = 'email:gmail'") == 1
    assert repo.get_state(gctx.db, "gmail_last_success_at")


def test_quota_errors_abort_fetching_without_marking_success(gctx):
    # newest first: m-recruiter-1 is fetched, then m-confirm-1 keeps hitting the quota
    svc = fake_service("m-confirm-1", "m-recruiter-1", get_failures={"m-confirm-1": [http_error(429)] * 5})
    client = GmailReadClient(svc, max_retries=2, sleep=lambda s: None)
    result = sync_gmail(gctx, client=client)
    assert result.aborted and len(result.errors) == 1 and result.new == 1
    assert repo.get_state(gctx.db, "gmail_last_success_at") is None
    assert gctx.db.scalar("SELECT retryable FROM errors WHERE component = 'email:gmail'") == 1
    # the message that failed is fetched again next time; the stored one is not
    svc.get_failures.clear()
    again = sync_gmail(gctx, client=client)
    assert (again.new, again.skipped_existing, again.fetched) == (1, 1, 1)
    assert repo.get_state(gctx.db, "gmail_last_success_at")
    assert gctx.db.scalar("SELECT COUNT(*) FROM email_messages") == 2


def test_429_then_success_during_sync(gctx):
    sleeps: list[float] = []
    svc = fake_service("m-confirm-1", list_failures=[http_error(429, "rateLimitExceeded")])
    result = sync_gmail(gctx, client=GmailReadClient(svc, sleep=sleeps.append))
    assert result.new == 1 and len(sleeps) == 1


def test_email_sync_task_through_task_runner(gctx, monkeypatch):
    from job_agent.tasks import run_task

    svc = fake_service()
    monkeypatch.setattr(email_service, "_make_client", lambda ctx, service, fmt: GmailReadClient(svc))
    result = run_task(gctx, "email_sync", trigger="manual")
    assert result.status == RunStatus.SUCCEEDED, result.error
    assert result.summary["new"] == 9 and result.summary["partial"] is False


# ------------------------------------------------------- user actions ---- #
def test_associate_email_validation_and_job_only_link(gctx, make_job):
    db = gctx.db
    sync_gmail(gctx, service=fake_service("m-recruiter-1"))
    email_id = _email(db, "m-recruiter-1")["id"]
    job_id = make_job(employer="Globex", employer_normalized="globex", pipeline_state="shortlisted")
    with pytest.raises(PermanentError):
        associate_email(gctx, 99999, job_ref=job_id)
    with pytest.raises(PermanentError):
        associate_email(gctx, email_id)
    with pytest.raises(PermanentError):
        associate_email(gctx, email_id, job_ref="J-missing")
    other_job = make_job(employer="Other", employer_normalized="other")
    other_app = make_application(db, other_job, "submitted")
    with pytest.raises(PermanentError, match="different job"):
        associate_email(gctx, email_id, job_ref=job_id, application_ref=other_app)

    job_uid = db.scalar("SELECT job_uid FROM jobs WHERE id = ?", (job_id,))
    res = associate_email(gctx, email_id, job_ref=job_uid, apply_status=True)
    assert res.job_id == job_id and res.application_id is None
    assert res.skipped_reason == "no application exists for this job"
    # recruiter messages still need a reply, so attention stays on
    assert _email(db, "m-recruiter-1")["requires_attention"] == 1
    assert db.scalar("SELECT COUNT(*) FROM audit_events WHERE action = 'email_associated'") == 1


def test_mark_email_handled(gctx):
    sync_gmail(gctx, service=fake_service("m-recruiter-1"))
    email_id = _email(gctx.db, "m-recruiter-1")["id"]
    assert mark_email_handled(gctx, email_id) is True
    assert mark_email_handled(gctx, email_id) is False
    assert _email(gctx.db, "m-recruiter-1")["requires_attention"] == 0


def test_email_never_undoes_your_status_correction(gctx, make_job):
    """r3: after you correct an email-set outcome, a later (mis)classified email must not flip it back."""
    from job_agent.email.status import apply_email_status

    db = gctx.db
    job_id = make_job(employer="Umbrella Health", employer_normalized="umbrella health",
                      job_url="https://careers.umbrellahealth.com/jobs/77")
    app_id = make_application(db, job_id, "submitted", submitted_at=SUBMITTED_AT)
    sync_gmail(gctx, service=fake_service("s-domain-reject-1"))
    assert repo.get_application(db, app_id)["status"] == "rejected"
    repo.transition_application(db, app_id, S.INTERVIEW_REQUESTED, actor="user", details={"note": "misread email"})

    email_id = _email(db, "s-domain-reject-1")["id"]
    assoc_id = _assoc(db, email_id)[0]["id"]
    evidence = {"type": "email", "gmail_message_id": "s-domain-reject-1"}
    auto = apply_email_status(db, application_id=app_id, association_id=assoc_id, category=EmailCategory.REJECTION,
                              evidence=evidence, actor="email", email_id=email_id)
    assert auto.applied is None and "corrected this application's status yourself" in auto.skipped_reason
    assert repo.get_application(db, app_id)["status"] == "interview_requested"

    mine = apply_email_status(db, application_id=app_id, association_id=assoc_id, category=EmailCategory.REJECTION,
                              evidence=evidence, actor="user", email_id=email_id)
    assert mine.applied == S.REJECTED  # you can still apply it yourself
