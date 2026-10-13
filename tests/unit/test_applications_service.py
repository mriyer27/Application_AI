"""Preparation, approval and submission workflow with the in-memory fake browser."""

from __future__ import annotations

import pytest

from job_agent.applications import service
from job_agent.applications.payload import build_payload, submission_blockers
from job_agent.browser.forms import FormField
from job_agent.database import repo
from job_agent.errors import ApprovalError, HumanActionRequired, InvalidTransition, PermanentError, PermissionDenied
from job_agent.models import PipelineState as S
from test_applications_support import (
    URL,
    add_sponsorship_answer,
    fake_for,
    setup_application,
    simple_fields,
    status_of,
)


def _answers(ctx, app_id):
    return {r["field_key"]: dict(r) for r in ctx.db.query_all(
        "SELECT * FROM application_answers WHERE application_id = ?", (app_id,))}


def _approval(ctx, uid):
    return repo.get_approval(ctx.db, uid)


def _ready_simple(ctx, make_job, *, outcome="confirm", **form_kw):
    """Prepared + all review items resolved + approved, with browser submission enabled."""
    add_sponsorship_answer(ctx)
    info = setup_application(ctx, make_job)
    fake = fake_for(fields=simple_fields(), outcome=outcome, **form_kw)
    prep = service.prepare_application(ctx, info["job_uid"], browser_factory=fake)
    assert prep.status == S.AWAITING_APPROVAL
    approval_uid = service.set_answer(ctx, info["app_uid"], "certify", "yes")
    service.decide(ctx, approval_uid, approve=True, actor="cli")
    ctx.config.applications.browser_submission_enabled = True
    ctx.config.applications.browser_timeout_seconds = 0.3
    return info, fake, approval_uid


# ------------------------------------------------------------ prepare ---- #
def test_prepare_fills_only_verified_answers_and_requests_approval(ctx, make_job):
    add_sponsorship_answer(ctx)
    info = setup_application(ctx, make_job)
    fake = fake_for()
    result = service.prepare_application(ctx, info["job_uid"], browser_factory=fake)

    assert result.status == S.AWAITING_APPROVAL and status_of(ctx, info["app_id"]) == "awaiting_approval"
    assert fake.submissions == 0  # never clicks submit
    assert fake.options.headless is True and fake.options.max_concurrent == 1
    assert set(fake.filled) == {"first_name", "last_name", "email", "phone", "source", "sponsorship"}
    assert fake.filled["sponsorship"] == "No" and fake.filled["source"] == "Company careers page"
    assert fake.uploaded["resume"].suffix == ".pdf"  # PDF preferred over DOCX
    for untouched in ("work_auth", "gender", "certify", "salary", "why"):
        assert untouched not in fake.filled

    answers = _answers(ctx, info["app_id"])
    assert answers["work_auth"]["is_legal_declaration"] == 1 and answers["work_auth"]["answer"] is None
    assert answers["sponsorship"]["answer_source"] == "verified_answer:sponsorship"
    assert answers["sponsorship"]["is_legal_declaration"] == 1 and answers["sponsorship"]["requires_review"] == 1
    assert answers["gender"]["requires_review"] == 1 and answers["gender"]["answer"] is None
    assert answers["certify"]["requires_review"] == 1 and answers["certify"]["answer"] is None

    approval = _approval(ctx, result.approval_uid)
    assert approval["status"] == "pending" and approval["request_type"] == "submit_application"
    payload = repo.approval_payload(approval)
    assert payload["employer"] == "Acme Analytics" and payload["application_url"] == URL
    assert payload["resume"]["file"].endswith(".pdf") and payload["cover_letter"]["format"] == "md"
    assert {d["field_key"] for d in payload["legal_declarations"]} == {"work_auth", "sponsorship"}
    assert {d["field_key"] for d in payload["unanswered_required"]} == {"work_auth", "certify"}
    assert {d["field_key"] for d in payload["agreements"]} == {"certify"}
    assert {d["field_key"] for d in payload["requires_review"]} >= {"gender", "certify", "salary", "why"}
    assert payload["work_authorization_reference"]["current_status"] == "F-1"
    assert "notes" not in payload["work_authorization_reference"]
    assert payload["screenshot"] == f"screenshots/{info['app_uid']}/prepared.png"
    assert (ctx.config.data_dir / payload["screenshot"]).is_file()
    assert repo.payload_hash(build_payload(ctx.db, info["app_id"])) == approval["payload_hash"]  # deterministic
    assert "approvals show" in result.message


def test_prepare_requires_application_with_resume(ctx, make_job):
    job_id = make_job()
    uid = ctx.db.scalar("SELECT job_uid FROM jobs WHERE id = ?", (job_id,))
    with pytest.raises(HumanActionRequired, match="no prepared résumé") as exc:
        service.prepare_application(ctx, uid, browser_factory=fake_for())
    assert exc.value.human_action == f"run job-agent prepare {uid} first"


def test_prepare_refuses_changed_document_before_opening_browser(ctx, make_job):
    info = setup_application(ctx, make_job, formats=("pdf",))
    path = ctx.config.documents_dir / ctx.db.scalar("SELECT relative_path FROM document_versions WHERE id = ?",
                                                    (info["resume_ids"]["pdf"],))
    path.write_bytes(b"tampered")
    fake = fake_for()
    with pytest.raises(HumanActionRequired, match="changed on disk"):
        service.prepare_application(ctx, info["job_uid"], browser_factory=fake)
    assert fake.opened == [] and status_of(ctx, info["app_id"]) == "documents_ready"


def test_prepare_permission_denied(ctx, make_job):
    info = setup_application(ctx, make_job)
    ctx.config.permissions.prepare_applications = False
    with pytest.raises(PermissionDenied):
        service.prepare_application(ctx, info["job_uid"], browser_factory=fake_for())


def test_captcha_stops_with_manual_instructions(ctx, make_job):
    info = setup_application(ctx, make_job)
    fake = fake_for(iframe_srcs=("https://www.google.com/recaptcha/api2/anchor?k=1",))
    result = service.prepare_application(ctx, info["job_uid"], browser_factory=fake)
    assert result.status == S.NEEDS_MANUAL_ACTION and result.challenge == "captcha"
    assert URL in result.message and "mark-submitted" in result.message
    assert fake.submissions == 0 and fake.filled == {} and _answers(ctx, info["app_id"]) == {}
    err = ctx.db.query_one("SELECT * FROM errors WHERE application_id = ?", (info["app_id"],))
    assert err["error_kind"] == "human_action_required"
    assert ctx.db.scalar("SELECT COUNT(*) FROM approval_requests") == 0


@pytest.mark.parametrize("form_kw,kind", [
    ({"visible_password_fields": 1, "text": "Sign in"}, "login"),
    ({"redirect_to": "https://sso.other-domain.com/login"}, "navigation_blocked"),
    ({"fields": []}, "no_form"),
    ({"text": "Sorry, this position has been filled."}, "closed"),
])
def test_unsupported_pages_need_manual_action(ctx, make_job, form_kw, kind):
    info = setup_application(ctx, make_job)
    fields = form_kw.pop("fields", None)
    fake = fake_for(fields=fields, **form_kw)
    result = service.prepare_application(ctx, info["job_uid"], browser_factory=fake)
    assert result.status == S.NEEDS_MANUAL_ACTION
    event = ctx.db.query_one("SELECT details FROM application_events WHERE application_id = ? "
                             "AND event_type = 'manual_action_required'", (info["app_id"],))
    assert f'"kind": "{kind}"' in event["details"]
    assert fake.submissions == 0


def test_closed_listing_not_opened(ctx, make_job):
    info = setup_application(ctx, make_job, listing_status="closed")
    fake = fake_for()
    result = service.prepare_application(ctx, info["job_uid"], browser_factory=fake)
    assert result.status == S.NEEDS_MANUAL_ACTION and fake.opened == []


def test_fill_failure_becomes_review_item(ctx, make_job):
    info = setup_application(ctx, make_job)
    result = service.prepare_application(ctx, info["job_uid"], browser_factory=fake_for(refuse_fill={"phone"}))
    assert result.status == S.AWAITING_APPROVAL
    row = _answers(ctx, info["app_id"])["phone"]
    assert row["requires_review"] == 1 and "could not be entered" in row["review_reason"]


def test_reprepare_keeps_user_answers(ctx, make_job):
    info = setup_application(ctx, make_job)
    service.prepare_application(ctx, info["job_uid"], browser_factory=fake_for())
    service.set_answer(ctx, info["app_uid"], "salary", "Open to discussion")
    fake = fake_for()
    service.prepare_application(ctx, info["job_uid"], browser_factory=fake)
    assert fake.filled["salary"] == "Open to discussion"
    assert _answers(ctx, info["app_id"])["salary"]["answer_source"] == "user"


# ------------------------------------------------------------ answers ---- #
def test_set_answer_supersedes_approval_and_keeps_legal_flag(ctx, make_job):
    info = setup_application(ctx, make_job)
    first = service.prepare_application(ctx, info["job_uid"], browser_factory=fake_for()).approval_uid
    new_uid = service.set_answer(ctx, info["app_uid"], "work_auth", "yes")
    assert new_uid and new_uid != first
    assert _approval(ctx, first)["status"] == "superseded" and _approval(ctx, new_uid)["status"] == "pending"
    row = _answers(ctx, info["app_id"])["work_auth"]
    assert (row["answer"], row["answer_source"], row["confidence"]) == ("Yes", "user", "verified")
    assert row["is_legal_declaration"] == 1 and row["requires_review"] == 1
    legal = {d["field_key"]: d for d in repo.approval_payload(_approval(ctx, new_uid))["legal_declarations"]}
    assert legal["work_auth"]["verified"] is True and legal["work_auth"]["source"] == "user"


def test_set_answer_validates_options_and_files(ctx, make_job):
    info = setup_application(ctx, make_job)
    service.prepare_application(ctx, info["job_uid"], browser_factory=fake_for())
    with pytest.raises(PermanentError, match="not one of the options"):
        service.set_answer(ctx, info["app_uid"], "gender", "Unknown")
    with pytest.raises(PermanentError, match="file uploads"):
        service.set_answer(ctx, info["app_uid"], "resume", "/etc/passwd")
    with pytest.raises(PermanentError, match="yes or no"):
        service.set_answer(ctx, info["app_uid"], "certify", "maybe")
    assert service.set_answer(ctx, info["app_uid"], "#9", "decline to self-identify")  # "#n" reference, gender
    assert _answers(ctx, info["app_id"])["gender"]["answer"] == "Decline to self-identify"


def test_explicit_blank_resolves_optional_review_item(ctx, make_job):
    info = setup_application(ctx, make_job)
    service.prepare_application(ctx, info["job_uid"], browser_factory=fake_for())
    uid = service.set_answer(ctx, info["app_uid"], "why", "")
    payload = repo.approval_payload(_approval(ctx, uid))
    assert "why" not in {r["field_key"] for r in payload["requires_review"]}
    assert {"field_key": "why", "label": "Why do you want to work here?"} in payload["left_blank_by_you"]


# ------------------------------------------------------------- decide ---- #
def test_reject_returns_to_documents_ready(ctx, make_job):
    info = setup_application(ctx, make_job)
    uid = service.prepare_application(ctx, info["job_uid"], browser_factory=fake_for()).approval_uid
    result = service.decide(ctx, uid, approve=False, actor="cli", note="wrong résumé")
    assert result.status == S.DOCUMENTS_READY and status_of(ctx, info["app_id"]) == "documents_ready"
    assert "wrong résumé" in result.next_action
    assert _approval(ctx, uid)["status"] == "rejected"


def test_approve_with_browser_submission_disabled_means_manual_submission(ctx, make_job):
    info = setup_application(ctx, make_job)
    uid = service.prepare_application(ctx, info["job_uid"], browser_factory=fake_for()).approval_uid
    result = service.decide(ctx, uid, approve=True, actor="dashboard")
    expected = (f"Approved - submit manually at {URL} using the prepared answers, then run "
                f"job-agent applications mark-submitted {info['app_uid']}")
    assert result.next_action == expected
    app = repo.get_application(ctx.db, info["app_id"])
    assert app["status"] == "awaiting_approval" and app["next_action"] == expected
    assert _approval(ctx, uid)["status"] == "approved"


def test_only_humans_decide_and_hash_must_match(ctx, make_job):
    info = setup_application(ctx, make_job)
    uid = service.prepare_application(ctx, info["job_uid"], browser_factory=fake_for()).approval_uid
    with pytest.raises(ApprovalError):
        service.decide(ctx, uid, approve=True, actor="system")
    with pytest.raises(ApprovalError, match="changed since you reviewed"):
        service.decide(ctx, uid, approve=True, actor="cli", expected_payload_hash="0" * 64)
    assert _approval(ctx, uid)["status"] == "pending"


# ------------------------------------------------------------- submit ---- #
def test_submit_refused_when_browser_submission_disabled(ctx, make_job):
    info, fake, uid = _ready_simple(ctx, make_job)
    ctx.config.applications.browser_submission_enabled = False
    with pytest.raises(PermissionDenied, match="browser_submission_enabled"):
        service.submit_approved(ctx, uid, browser_factory=fake)
    assert fake.submissions == 0 and _approval(ctx, uid)["status"] == "approved"


def test_submit_without_approval_refused(ctx, make_job):
    add_sponsorship_answer(ctx)
    info = setup_application(ctx, make_job)
    fake = fake_for(fields=simple_fields())
    service.prepare_application(ctx, info["job_uid"], browser_factory=fake)
    pending = service.set_answer(ctx, info["app_uid"], "certify", "yes")
    ctx.config.applications.browser_submission_enabled = True
    with pytest.raises(ApprovalError, match="not been approved"):
        service.submit_approved(ctx, pending, browser_factory=fake)
    with pytest.raises(ApprovalError, match="not found"):
        service.submit_approved(ctx, "AP-doesnotexist", browser_factory=fake)
    assert fake.submissions == 0 and status_of(ctx, info["app_id"]) == "awaiting_approval"


def test_submit_refused_with_unresolved_review_items(ctx, make_job):
    add_sponsorship_answer(ctx)
    info = setup_application(ctx, make_job)
    fake = fake_for(fields=simple_fields())
    uid = service.prepare_application(ctx, info["job_uid"], browser_factory=fake).approval_uid
    service.decide(ctx, uid, approve=True, actor="cli")  # approved for manual use despite open items
    ctx.config.applications.browser_submission_enabled = True
    with pytest.raises(service.SubmissionBlocked, match="certify"):
        service.submit_approved(ctx, uid, browser_factory=fake)
    assert fake.submissions == 0 and _approval(ctx, uid)["status"] == "approved"


def test_unverified_legal_declaration_blocks_submission(ctx, make_job):
    info = setup_application(ctx, make_job)  # no verified sponsorship answer
    fake = fake_for(fields=simple_fields())
    service.prepare_application(ctx, info["job_uid"], browser_factory=fake)
    uid = service.set_answer(ctx, info["app_uid"], "certify", "yes")
    payload = repo.approval_payload(_approval(ctx, uid))
    assert any("legal declaration" in b for b in submission_blockers(payload))


def test_answer_changed_after_approval_is_refused(ctx, make_job):
    info, fake, uid = _ready_simple(ctx, make_job)
    ctx.db.execute("UPDATE application_answers SET answer = 'Someone Else' WHERE application_id = ? "
                   "AND field_key = 'first_name'", (info["app_id"],))
    with pytest.raises(ApprovalError, match="changed after approval"):
        service.submit_approved(ctx, uid, browser_factory=fake)
    assert fake.submissions == 0 and _approval(ctx, uid)["status"] == "approved"


def test_set_answer_after_approval_requires_new_approval(ctx, make_job):
    info, fake, uid = _ready_simple(ctx, make_job)
    new_uid = service.set_answer(ctx, info["app_uid"], "first_name", "Testy")
    with pytest.raises(ApprovalError, match="replaced by a newer request"):
        service.submit_approved(ctx, uid, browser_factory=fake)
    assert _approval(ctx, new_uid)["status"] == "pending" and fake.submissions == 0


def test_submit_confirmed_records_page_evidence_and_consumes_once(ctx, make_job):
    info, fake, uid = _ready_simple(ctx, make_job)
    result = service.submit_approved(ctx, uid, browser_factory=fake)
    assert result.outcome == "submitted" and result.status == S.SUBMITTED
    assert fake.submissions == 1
    assert fake.submitted_values[0]["certify"] == "yes" and fake.submitted_values[0]["sponsorship"] == "No"
    assert fake.submitted_values[0]["first_name"] == "Test"
    app = repo.get_application(ctx.db, info["app_id"])
    assert app["status"] == "submitted" and app["confirmation_status"] == "page_evidence"
    assert app["submission_attempts"] == 1 and app["submitted_at"]
    assert result.evidence["type"] == "page" and "Thank you for applying" in result.evidence["matched_text"]
    assert result.evidence["url"].endswith("/thank-you") and result.evidence["screenshot"]
    assert _approval(ctx, uid)["status"] == "consumed"
    with pytest.raises(ApprovalError, match="already used"):
        service.submit_approved(ctx, uid, browser_factory=fake)
    assert fake.submissions == 1


def test_unknown_outcome_needs_manual_action_and_is_never_retried(ctx, make_job):
    info, fake, uid = _ready_simple(ctx, make_job, outcome="hang")
    result = service.submit_approved(ctx, uid, browser_factory=fake)
    assert result.outcome == "unknown" and result.status == S.NEEDS_MANUAL_ACTION
    assert "check your email/portal before doing anything else" in result.message.lower()
    err = ctx.db.query_one("SELECT * FROM errors WHERE application_id = ? AND error_kind = 'unknown_outcome'",
                           (info["app_id"],))
    assert err is not None and err["retryable"] == 0 and err["requires_human_action"] == 1
    assert fake.submissions == 1 and _approval(ctx, uid)["status"] == "consumed"
    # Preparing it again is refused: it may already have reached the employer (r2 finding).
    with pytest.raises(HumanActionRequired, match="UNKNOWN outcome") as refused:
        service.prepare_application(ctx, info["job_uid"], browser_factory=fake)
    assert "--confirm-not-submitted" in refused.value.human_action
    assert "mark-submitted" in refused.value.human_action and "email" in refused.value.human_action
    assert status_of(ctx, info["app_id"]) == "needs_manual_action"
    assert ctx.db.scalar("SELECT COUNT(*) FROM approval_requests WHERE status = 'pending'") == 0
    # Even after you confirm it was not sent, a fresh prepare + approval cannot trigger another browser submission.
    fresh = service.prepare_application(ctx, info["job_uid"], browser_factory=fake,
                                        confirm_not_submitted=True).approval_uid
    confirmed = ctx.db.query_one("SELECT * FROM application_events WHERE application_id = ? "
                                 "AND event_type = 'confirmed_not_submitted'", (info["app_id"],))
    assert confirmed is not None and confirmed["actor"] == "user"
    decision = service.decide(ctx, fresh, approve=True, actor="cli")
    assert "will never submit it again" in decision.next_action and "submit manually" in decision.next_action.lower()
    with pytest.raises(service.SubmissionBlocked, match="unknown outcome"):
        service.submit_approved(ctx, fresh, browser_factory=fake)
    assert fake.submissions == 1
    # The human resolves it.
    service.mark_submitted(ctx, info["app_uid"], note="confirmation email arrived")
    assert status_of(ctx, info["app_id"]) == "submitted"


def test_validation_errors_after_submit_mark_failed(ctx, make_job):
    info, fake, uid = _ready_simple(ctx, make_job, outcome="validation")
    result = service.submit_approved(ctx, uid, browser_factory=fake)
    assert result.outcome == "failed" and result.status == S.FAILED
    assert result.validation_errors == ["Phone number is invalid"]
    assert status_of(ctx, info["app_id"]) == "failed"


def test_challenge_before_click_stops_without_using_approval(ctx, make_job):
    info, fake, uid = _ready_simple(ctx, make_job)
    fake.forms[URL].markers = (".h-captcha",)
    result = service.submit_approved(ctx, uid, browser_factory=fake)
    assert result.outcome == "not_submitted" and result.status == S.NEEDS_MANUAL_ACTION
    assert fake.submissions == 0 and _approval(ctx, uid)["status"] == "approved"


def test_form_changed_since_approval_stops_before_click(ctx, make_job):
    info, fake, uid = _ready_simple(ctx, make_job)
    fake.forms[URL].fields.append(FormField("ssn", "Social security number", "text", required=True))
    result = service.submit_approved(ctx, uid, browser_factory=fake)
    assert result.outcome == "not_submitted" and "not approved" in result.message
    assert fake.submissions == 0


# ------------------------------------------------------------ tracking --- #
def test_mark_submitted_withdraw_notes_and_views(ctx, make_job):
    info = setup_application(ctx, make_job)
    uid = service.prepare_application(ctx, info["job_uid"], browser_factory=fake_for()).approval_uid
    service.add_note(ctx, info["app_uid"], "Recruiter is Sam")
    service.mark_submitted(ctx, info["app_uid"], note="applied on the website")
    app = repo.get_application(ctx.db, info["app_id"])
    assert app["status"] == "submitted" and app["confirmation_status"] == "user_reported"
    assert "Recruiter is Sam" in app["notes"]
    closed = _approval(ctx, uid)
    assert closed["status"] == "superseded"  # pending request closed by your report
    assert "marked submitted by you" in closed["decision_note"]

    rows = service.list_applications(ctx.db)
    assert rows[0]["application_uid"] == info["app_uid"] and rows[0]["employer"] == "Acme Analytics"
    assert service.list_applications(ctx.db, S.WITHDRAWN) == []
    detail = service.application_detail(ctx.db, info["app_uid"])
    types = [e["event_type"] for e in detail["events"]]
    assert "form_prepared" in types and "note" in types and types[-1] == "status_change"
    assert detail["documents"]["resume"][0]["format"] == "pdf" and len(detail["documents"]["resume"]) == 2
    assert detail["approvals"][0]["approval_uid"] == uid and detail["answers"]

    service.withdraw(ctx, info["app_uid"], note="took another offer")
    assert status_of(ctx, info["app_id"]) == "withdrawn"
    with pytest.raises(InvalidTransition):
        service.set_answer(ctx, info["app_uid"], "salary", "x")


def test_approval_view_sections(ctx, make_job):
    add_sponsorship_answer(ctx)
    info = setup_application(ctx, make_job)
    uid = service.prepare_application(ctx, info["job_uid"], browser_factory=fake_for()).approval_uid
    view = service.approval_view(ctx.db, uid)
    s = view["sections"]
    assert set(s) >= {"employer_role", "url", "documents", "answers", "legal_declarations", "unanswered",
                      "uncertain", "agreements"}
    assert s["employer_role"]["employer"] == "Acme Analytics" and s["url"] == URL
    assert s["documents"]["resume"]["sha256"]
    sponsorship = next(d for d in s["legal_declarations"] if d["field_key"] == "sponsorship")
    assert sponsorship["answer"] == "No" and sponsorship["verified"] and sponsorship["ref"].startswith("#")
    assert view["browser_submission_blockers"] and view["status"] == "pending"
    with pytest.raises(ApprovalError):
        service.approval_view(ctx.db, "AP-missing")


def test_decide_records_other_approval_types_without_application_side_effects(ctx):
    from job_agent.models import ApprovalType

    uid = repo.create_approval(ctx.db, ApprovalType.SEND_FOLLOWUP, {"draft": "Hello"}, summary="Follow up",
                               actor="system", expires_in_hours=1)
    result = service.decide(ctx, uid, approve=True, actor="dashboard")
    assert result.application_uid is None and result.status is None
    assert repo.get_approval(ctx.db, uid)["status"] == "approved"


def test_unapproved_documents_block_browser_submission():
    """Security review: documents still 'needs_review' must not be uploaded by browser submission."""
    from job_agent.applications.payload import submission_blockers

    base = {"unanswered_required": [], "requires_review": [], "legal_declarations": [], "validation_errors": []}
    blocked = submission_blockers({**base, "resume": {"document_id": 3, "status": "needs_review"},
                                   "cover_letter": {"document_id": 4, "status": "draft"}})
    assert any("résumé has not been approved" in b and "documents approve 3" in b for b in blocked)
    assert any("cover letter has not been approved" in b for b in blocked)
    assert submission_blockers({**base, "resume": {"document_id": 3, "status": "approved"},
                                "cover_letter": None}) == []


# ------------------------------------------- r2: interrupted submissions --- #
def _unknown_rows(ctx, app_id):
    events = ctx.db.scalar("SELECT COUNT(*) FROM application_events WHERE application_id = ? "
                           "AND event_type = 'submission_outcome_unknown'", (app_id,))
    errors = ctx.db.scalar("SELECT COUNT(*) FROM errors WHERE application_id = ? AND error_kind = 'unknown_outcome' "
                           "AND retryable = 0 AND requires_human_action = 1", (app_id,))
    return events, errors


@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit])
def test_interrupt_after_click_records_unknown_outcome(ctx, make_job, monkeypatch, exc_type):
    """r2 HIGH: Ctrl+C / SystemExit after the click left SUBMISSION_ATTEMPTED with no unknown-outcome record."""
    from job_agent.browser.fakes import FakePage

    info, fake, uid = _ready_simple(ctx, make_job, outcome="hang")
    original = FakePage.click_submit

    def click_then_interrupt(self, control):
        original(self, control)  # the form was posted ...
        raise exc_type()  # ... and the user pressed Ctrl+C while waiting

    monkeypatch.setattr(FakePage, "click_submit", click_then_interrupt)
    with pytest.raises(exc_type):
        service.submit_approved(ctx, uid, browser_factory=fake)
    assert fake.submissions == 1
    app = repo.get_application(ctx.db, info["app_id"])
    assert app["status"] == "needs_manual_action" and "Unknown outcome" in app["next_action"]
    assert _unknown_rows(ctx, info["app_id"]) == (1, 1)
    assert _approval(ctx, uid)["status"] == "consumed"
    assert service.application_detail(ctx.db, info["app_uid"])["unknown_outcome"] is True
    # You can now record either outcome yourself; nothing is retried.
    service.set_status(ctx, info["app_uid"], "closed", note="it never arrived")
    assert status_of(ctx, info["app_id"]) == "closed" and fake.submissions == 1


def test_interrupt_while_polling_records_unknown_outcome(ctx, make_job, monkeypatch):
    info, fake, uid = _ready_simple(ctx, make_job, outcome="hang")

    def interrupted_poll(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(service, "poll_outcome", interrupted_poll)
    with pytest.raises(KeyboardInterrupt):
        service.submit_approved(ctx, uid, browser_factory=fake)
    assert status_of(ctx, info["app_id"]) == "needs_manual_action"
    assert _unknown_rows(ctx, info["app_id"]) == (1, 1)
    service.withdraw(ctx, info["app_uid"])
    assert status_of(ctx, info["app_id"]) == "withdrawn"


@pytest.mark.skipif(not hasattr(__import__("signal"), "SIGTERM") or __import__("os").name == "nt",
                    reason="POSIX signals")
def test_sigterm_while_waiting_for_outcome_is_recorded(ctx, make_job, monkeypatch):
    """A stop signal (default action: die silently) during the wait becomes SystemExit, recorded first."""
    import os
    import signal
    import time

    info, fake, uid = _ready_simple(ctx, make_job, outcome="hang")

    def poll_then_terminated(*args, **kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(2)  # the handler raises before this returns
        raise AssertionError("SIGTERM was not turned into an exception")

    monkeypatch.setattr(service, "poll_outcome", poll_then_terminated)
    with pytest.raises(SystemExit):
        service.submit_approved(ctx, uid, browser_factory=fake)
    assert status_of(ctx, info["app_id"]) == "needs_manual_action"
    assert _unknown_rows(ctx, info["app_id"]) == (1, 1)
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL  # handler restored


def test_interrupt_after_known_outcome_keeps_that_outcome(ctx, make_job, monkeypatch):
    """Ctrl+C after the confirmation page was seen (e.g. during the screenshot) keeps the evidence."""
    info, fake, uid = _ready_simple(ctx, make_job)
    real_screenshot = service._screenshot

    def screenshot(ctx_, page, app_uid, name):
        if name.startswith("submit-"):
            raise KeyboardInterrupt
        return real_screenshot(ctx_, page, app_uid, name)

    monkeypatch.setattr(service, "_screenshot", screenshot)
    with pytest.raises(KeyboardInterrupt):
        service.submit_approved(ctx, uid, browser_factory=fake)
    app = repo.get_application(ctx.db, info["app_id"])
    assert app["status"] == "submitted" and app["confirmation_status"] == "page_evidence"
    assert _unknown_rows(ctx, info["app_id"]) == (0, 0)


def _stuck_attempt(ctx, make_job, *, pid, minutes_ago=0):
    """SUBMISSION_ATTEMPTED left behind by a process that was killed (kill -9 / power loss)."""
    import socket
    from datetime import timedelta

    from job_agent.utils.time import to_iso, utcnow

    info, fake, uid = _ready_simple(ctx, make_job)
    repo.transition_application(ctx.db, info["app_id"], S.SUBMISSION_ATTEMPTED, actor="browser", approval_uid=uid,
                                approved_payload=build_payload(ctx.db, info["app_id"]),
                                details={"url": URL, "pid": pid, "host": socket.gethostname()})
    if minutes_ago:
        ctx.db.execute("UPDATE applications SET status_updated_at = ? WHERE id = ?",
                       (to_iso(utcnow() - timedelta(minutes=minutes_ago)), info["app_id"]))
    return info, fake, uid


def _dead_pid():
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_killed_submission_process_can_be_resolved_by_you(ctx, make_job):
    """r2 HIGH: after a kill, withdraw / set-status closed / apply each raised InvalidTransition forever."""
    info, fake, uid = _stuck_attempt(ctx, make_job, pid=_dead_pid())
    detail = service.application_detail(ctx.db, info["app_uid"])
    assert detail["submission_abandoned"] is True and detail["application"]["status"] == "submission_attempted"
    service.withdraw(ctx, info["app_uid"], note="checked: it never arrived")
    assert status_of(ctx, info["app_id"]) == "withdrawn"
    assert _unknown_rows(ctx, info["app_id"]) == (1, 1)
    statuses = [(e["from_status"], e["to_status"]) for e in service.application_detail(ctx.db, info["app_uid"])
                ["events"] if e["to_status"]]
    assert ("submission_attempted", "needs_manual_action") in statuses
    assert fake.submissions == 0


def test_killed_submission_then_apply_needs_confirmation(ctx, make_job):
    info, fake, uid = _stuck_attempt(ctx, make_job, pid=_dead_pid())
    with pytest.raises(HumanActionRequired, match="UNKNOWN outcome"):
        service.prepare_application(ctx, info["job_uid"], browser_factory=fake)
    assert status_of(ctx, info["app_id"]) == "needs_manual_action"  # recorded first, then refused
    result = service.prepare_application(ctx, info["job_uid"], browser_factory=fake, confirm_not_submitted=True)
    assert result.status == S.AWAITING_APPROVAL and fake.submissions == 0


def test_live_submission_process_is_not_taken_over(ctx, make_job):
    import os

    info, fake, uid = _stuck_attempt(ctx, make_job, pid=os.getppid())  # a running process
    with pytest.raises(service.SubmissionInProgress, match="still in progress"):
        service.withdraw(ctx, info["app_uid"])
    with pytest.raises(service.SubmissionInProgress):
        service.prepare_application(ctx, info["job_uid"], browser_factory=fake)
    assert status_of(ctx, info["app_id"]) == "submission_attempted"
    assert service.application_detail(ctx.db, info["app_uid"])["submission_abandoned"] is False
    # You can still say it went through.
    service.mark_submitted(ctx, info["app_uid"], note="confirmation email arrived")
    assert status_of(ctx, info["app_id"]) == "submitted"


def test_stale_submission_without_checkable_process_is_recovered(ctx, make_job):
    import os

    info, fake, uid = _stuck_attempt(ctx, make_job, pid=os.getppid(), minutes_ago=service.STUCK_SUBMISSION_MINUTES + 5)
    service.set_status(ctx, info["app_uid"], "closed")
    assert status_of(ctx, info["app_id"]) == "closed" and _unknown_rows(ctx, info["app_id"]) == (1, 1)


def test_repo_recovered_submission_counts_as_unknown_outcome(ctx, make_job):
    """repo.recover_stuck_submissions (daemon start-up) writes no browser event; it still blocks re-preparing."""
    info, fake, uid = _stuck_attempt(ctx, make_job, pid=_dead_pid(), minutes_ago=30)
    assert repo.recover_stuck_submissions(ctx.db) == [info["app_id"]]
    assert service.has_unknown_outcome(ctx.db, info["app_id"])
    assert service.application_detail(ctx.db, info["app_uid"])["unknown_outcome"] is True
    with pytest.raises(HumanActionRequired, match="UNKNOWN outcome"):
        service.prepare_application(ctx, info["job_uid"], browser_factory=fake)


def test_unknown_outcome_resolved_by_confirmation_still_works(ctx, make_job):
    """Legitimate path: an unknown outcome resolved by you is a normal submitted application."""
    info, fake, uid = _ready_simple(ctx, make_job, outcome="hang")
    service.submit_approved(ctx, uid, browser_factory=fake)
    service.set_status(ctx, info["app_uid"], "confirmation_received", note="email arrived")
    assert status_of(ctx, info["app_id"]) == "confirmation_received"


# ----------------------------------------------- r2: current URL, documents --- #
def test_apply_uses_the_jobs_current_application_url(ctx, make_job):
    """r2: the URL copied at `prepare` time was used even after discovery updated the job's apply URL."""
    info = setup_application(ctx, make_job)
    new_url = "https://jobs.example-employer.com/apply/123/form"
    ctx.db.execute("UPDATE jobs SET application_url = ? WHERE id = ?", (new_url, info["job_id"]))
    fake = fake_for(url=new_url)
    result = service.prepare_application(ctx, info["job_uid"], browser_factory=fake)
    assert fake.opened == [new_url] and result.url == new_url
    assert repo.get_application(ctx.db, info["app_id"])["application_url"] == new_url
    assert repo.approval_payload(_approval(ctx, result.approval_uid))["application_url"] == new_url


def test_apply_falls_back_to_current_job_url(ctx, make_job):
    info = setup_application(ctx, make_job)
    listing = "https://jobs.example-employer.com/jobs/123"
    ctx.db.execute("UPDATE jobs SET application_url = NULL, job_url = ? WHERE id = ?", (listing, info["job_id"]))
    fake = fake_for(url=listing)
    service.prepare_application(ctx, info["job_uid"], browser_factory=fake)
    assert fake.opened == [listing]


def _reject(ctx, doc_id, note="wrong bullets"):
    from job_agent.resume.service import set_document_status

    set_document_status(ctx, doc_id, "rejected", actor="cli", note=note)


def test_rejected_resume_is_never_uploaded(ctx, make_job):
    """r2: `apply` uploaded a résumé you explicitly rejected and let you approve the request."""
    add_sponsorship_answer(ctx)
    info = setup_application(ctx, make_job)
    _reject(ctx, info["resume_ids"]["pdf"])  # every format of the version
    fake = fake_for(fields=simple_fields())
    with pytest.raises(HumanActionRequired, match="rejected the résumé") as exc:
        service.prepare_application(ctx, info["job_uid"], browser_factory=fake)
    assert f"job-agent prepare {info['job_uid']}" in exc.value.human_action
    assert fake.opened == [] and fake.uploaded == {} and status_of(ctx, info["app_id"]) == "documents_ready"
    assert ctx.db.scalar("SELECT COUNT(*) FROM approval_requests") == 0


def test_rejected_cover_letter_is_never_used(ctx, make_job):
    info = setup_application(ctx, make_job)
    _reject(ctx, info["cover_id"])
    fake = fake_for()
    with pytest.raises(HumanActionRequired, match="rejected the cover letter"):
        service.prepare_application(ctx, info["job_uid"], browser_factory=fake)
    assert fake.opened == []


def test_rejected_version_falls_back_to_latest_unrejected_version(ctx, make_job):
    from test_applications_support import _write_doc

    info = setup_application(ctx, make_job, formats=("pdf",))
    v2 = _write_doc(ctx, info["job_id"], "resume", "pdf", b"%PDF-1.4 resume v2", version=2)
    ctx.db.update("applications", info["app_id"], {"resume_document_id": v2})
    _reject(ctx, v2)
    fake = fake_for()
    result = service.prepare_application(ctx, info["job_uid"], browser_factory=fake)
    assert result.status == S.AWAITING_APPROVAL
    assert fake.uploaded["resume"].name.endswith("_v1.pdf")
    payload = repo.approval_payload(_approval(ctx, result.approval_uid))
    assert payload["resume"]["document_id"] == info["resume_ids"]["pdf"] and payload["resume"]["status"] == "approved"


def test_resume_rejected_after_request_cannot_be_approved_or_submitted(ctx, make_job):
    add_sponsorship_answer(ctx)
    info = setup_application(ctx, make_job)
    fake = fake_for(fields=simple_fields())
    service.prepare_application(ctx, info["job_uid"], browser_factory=fake)
    uid = service.set_answer(ctx, info["app_uid"], "certify", "yes")
    _reject(ctx, info["resume_ids"]["pdf"])
    with pytest.raises(ApprovalError, match="you rejected"):
        service.decide(ctx, uid, approve=True, actor="cli")
    assert _approval(ctx, uid)["status"] == "pending"
    assert service.decide(ctx, uid, approve=False, actor="cli", note="regenerate").status == S.DOCUMENTS_READY


def test_resume_rejected_after_approval_is_not_submitted(ctx, make_job):
    info, fake, uid = _ready_simple(ctx, make_job)
    opened = len(fake.opened)
    _reject(ctx, info["resume_ids"]["pdf"])
    with pytest.raises(ApprovalError, match="changed after approval"):
        service.submit_approved(ctx, uid, browser_factory=fake)
    assert fake.submissions == 0 and len(fake.opened) == opened  # the form was never even opened


def test_needs_review_resume_still_prepares(ctx, make_job):
    """Legitimate: a résumé you have not reviewed yet is prepared (browser submission waits for approval)."""
    info = setup_application(ctx, make_job)
    ctx.db.execute("UPDATE document_versions SET status = 'needs_review' WHERE job_id = ?", (info["job_id"],))
    result = service.prepare_application(ctx, info["job_uid"], browser_factory=fake_for())
    assert result.status == S.AWAITING_APPROVAL
    blockers = submission_blockers(repo.approval_payload(_approval(ctx, result.approval_uid)))
    assert any("not been approved by you yet" in b for b in blockers)


# ------------------------------------------------ r2: approvals are closed --- #
@pytest.mark.parametrize("action", ["mark_submitted", "withdraw", "set_status_closed", "set_status_submitted"])
def test_approved_unused_approval_is_closed_when_you_record_the_outcome(ctx, make_job, action):
    """r2: an approved, never-used approval stayed 'approved' after mark-submitted / withdraw / set-status."""
    info, fake, uid = _ready_simple(ctx, make_job)
    {"mark_submitted": lambda: service.mark_submitted(ctx, info["app_uid"], note="applied on the site"),
     "withdraw": lambda: service.withdraw(ctx, info["app_uid"]),
     "set_status_closed": lambda: service.set_status(ctx, info["app_uid"], "closed"),
     "set_status_submitted": lambda: service.set_status(ctx, info["app_uid"], "submitted")}[action]()
    row = _approval(ctx, uid)
    assert row["status"] == "superseded" and "by you" in row["decision_note"]
    with pytest.raises(ApprovalError, match="no longer valid"):
        service.submit_approved(ctx, uid, browser_factory=fake)
    assert fake.submissions == 0


def test_you_can_withdraw_an_approval_before_it_is_used(ctx, make_job):
    info, fake, uid = _ready_simple(ctx, make_job)
    result = service.decide(ctx, uid, approve=False, actor="cli", note="changed my mind")
    assert result.status == S.DOCUMENTS_READY and "withdrawn" in result.next_action
    row = _approval(ctx, uid)
    assert row["status"] == "superseded" and "changed my mind" in row["decision_note"]
    with pytest.raises(ApprovalError):
        service.submit_approved(ctx, uid, browser_factory=fake)
    assert fake.submissions == 0
    with pytest.raises(ApprovalError, match="not pending"):  # an approval cannot be re-approved
        service.decide(ctx, uid, approve=True, actor="cli")


def test_consumed_approval_cannot_be_withdrawn(ctx, make_job):
    info, fake, uid = _ready_simple(ctx, make_job)
    service.submit_approved(ctx, uid, browser_factory=fake)
    with pytest.raises(ApprovalError, match="consumed"):
        service.decide(ctx, uid, approve=False, actor="cli", note="too late")
    assert _approval(ctx, uid)["status"] == "consumed" and status_of(ctx, info["app_id"]) == "submitted"
