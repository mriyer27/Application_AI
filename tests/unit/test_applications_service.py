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
    # Even a fresh prepare + approval cannot trigger another automatic submission.
    fresh = service.prepare_application(ctx, info["job_uid"], browser_factory=fake).approval_uid
    service.decide(ctx, fresh, approve=True, actor="cli")
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
    assert _approval(ctx, uid)["status"] == "rejected"  # pending request closed by your report

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
