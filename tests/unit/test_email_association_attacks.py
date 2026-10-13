"""Regression tests: emails must not change the wrong application's status (look-alike domains, other roles,
never-submitted / unknown outcomes, marketing "offers", shared company words, forged senders).

Every attack message below is *authenticated* (Gmail DKIM/DMARC pass for its own From domain) unless the test
is about authentication, so each defence is shown to hold on its own. The legitimate counterparts show that
genuine employer mail keeps working.
"""

from __future__ import annotations

import pytest

from job_agent.database import repo
from job_agent.database.connection import from_json
from job_agent.email import associate
from job_agent.email import status as email_status
from job_agent.email.associate import AUTO_THRESHOLD, find_candidates
from job_agent.email.service import associate_email, sync_gmail
from job_agent.errors import InvalidTransition
from job_agent.models import EmailCategory, PipelineState
from test_email_fakes import FakeGmailService, build_message, enable_gmail, make_application
from test_email_sync import _submitted_via_approval

S = PipelineState
UNVERIFIED_CAP = 0.7  # associate.UNVERIFIED_CAP
SUBMITTED_AT = "2026-10-01T00:00:00+00:00"


@pytest.fixture
def gctx(ctx):
    enable_gmail(ctx)
    return ctx


def gmail_ar(domain: str, *, dkim: str = "pass", dmarc: str = "pass", spf: str = "pass",
             dkim_domain: str | None = None) -> str:
    """An Authentication-Results header as Gmail's receiving servers write it."""
    return (f"mx.google.com; dkim={dkim} header.i=@{dkim_domain or domain} header.s=s1 header.b=Xy12Zq; "
            f"spf={spf} (google.com: domain of bounce@{domain} designates 192.0.2.7 as permitted sender) "
            f"smtp.mailfrom=bounce@{domain}; dmarc={dmarc} (p=REJECT sp=REJECT dis=NONE) header.from={domain}")


def mail(mid: str, frm: str, subject: str, text: str, *, auth: str | None = "pass",
         received: str = "2026-10-11T12:00:00Z", thread: str | None = None,
         headers: dict[str, str] | None = None) -> dict:
    domain = frm.rsplit("@", 1)[1].rstrip(">")
    extra: dict[str, str] = {}
    if auth == "pass":
        extra["Authentication-Results"] = gmail_ar(domain)
    elif auth:
        extra["Authentication-Results"] = auth
    extra.update(headers or {})
    spec = {"id": mid, "from": frm, "subject": subject, "text": text, "received": received, "headers": extra}
    if thread:
        spec["threadId"] = thread
    return build_message(spec)


def sync(ctx, *messages: dict) -> object:
    return sync_gmail(ctx, service=FakeGmailService(list(messages)))


def app_status(db, app_id: int) -> str:
    return repo.get_application(db, app_id)["status"]


def email_row(db, gmail_id: str):
    return db.query_one("SELECT * FROM email_messages WHERE gmail_message_id = ?", (gmail_id,))


def assocs(db, gmail_id: str) -> list:
    row = email_row(db, gmail_id)
    return db.query_all("SELECT * FROM email_associations WHERE email_id = ? ORDER BY confidence DESC",
                        (row["id"],))


def review_reasons(db, gmail_id: str) -> list[str]:
    row = db.query_one("SELECT details FROM audit_events WHERE action = 'email_status_needs_review' AND "
                       "entity_id = ? ORDER BY id DESC LIMIT 1", (str(email_row(db, gmail_id)["id"]),))
    return from_json(row["details"])["reasons"] if row else []


def acme_job(make_job, **kw) -> int:
    values = dict(employer="Acme Analytics", employer_normalized="acme analytics", title="Business Analyst",
                  title_normalized="business analyst", job_url="https://boards.greenhouse.io/acmeanalytics/jobs/101")
    values.update(kw)
    return make_job(**values)


CONFIRM = ("Thank you for applying to Acme Analytics",
           "Dear Test,\n\nThank you for applying for the Business Analyst position at Acme Analytics. We have "
           "received your application and our team will review it.\n\nAcme Analytics Recruiting")
REJECT = ("Your application to Acme Analytics",
          "Dear Test,\n\nThank you for your interest in the Business Analyst position. Unfortunately, after careful "
          "consideration, we have decided not to move forward with your application.\n\nAcme Analytics Recruiting")


# --------------------------------------------------------------------------- #
# 1. Look-alike domains
# --------------------------------------------------------------------------- #
def test_lookalike_prefix_domain_does_not_reject_other_employer(gctx, make_job):
    """better.com (another company) must not reject the Betterment application."""
    db = gctx.db
    job = make_job(employer="Betterment", employer_normalized="betterment", title="Data Analyst",
                   title_normalized="data analyst", job_url="https://boards.greenhouse.io/betterment/jobs/1")
    app = make_application(db, job, "submitted", submitted_at=SUBMITTED_AT)
    res = sync(gctx, mail("m1", "Better Recruiting <recruiting@better.com>", "Your application to Better",
                          "Hi Test, thank you for your interest in Better. Unfortunately, we have decided to move "
                          "forward with other candidates for the Loan Officer position."))
    assert res.status_updates == 0 and app_status(db, app) == "submitted"
    assert all(a["confidence"] < AUTO_THRESHOLD and a["status_applied"] is None for a in assocs(db, "m1"))


def test_lookalike_first_word_domain_does_not_reject_capital_one(gctx, make_job):
    db = gctx.db
    job = make_job(employer="Capital One", employer_normalized="capital one", title="Business Analyst",
                   title_normalized="business analyst", job_url="https://www.capitalonecareers.com/job/1")
    app = make_application(db, job, "submitted", submitted_at=SUBMITTED_AT)
    res = sync(gctx, mail("m-cap", "Careers <careers@capital.com>", "Your application to Capital One",
                          "Thank you for your interest in the Business Analyst position. Unfortunately, we have "
                          "decided not to move forward with your application."))
    assert res.status_updates == 0 and app_status(db, app) == "submitted"
    assert all(a["confidence"] <= UNVERIFIED_CAP for a in assocs(db, "m-cap"))
    assert email_row(db, "m-cap")["requires_attention"] == 1


@pytest.mark.parametrize("sender,start,mail_text", [
    ("Acme Analytics <careers@acme-analytics.xyz>", S.SUBMITTED, REJECT),
    ("Acme Analytics <careers@acmeanalytics.xyz>", S.SUBMITTED, REJECT),
    ("Acme Analytics <careers@acme.top>", S.SUBMITTED, REJECT),
    ("Acme <noreply@acmeana.click>", S.SUBMITTED, REJECT),
    ("Acme Analytics <careers@acme-analytics.xyz>", S.NEEDS_MANUAL_ACTION, CONFIRM),
])
def test_lookalike_domains_never_change_status(gctx, make_job, sender, start, mail_text):
    db = gctx.db
    app = make_application(db, acme_job(make_job), start.value, submitted_at=SUBMITTED_AT)
    gid = "m-" + sender.split("@")[1][:-1]
    res = sync(gctx, mail(gid, sender, mail_text[0], mail_text[1]))
    assert res.status_updates == 0 and res.errors == []
    assert app_status(db, app) == start.value
    rows = assocs(db, gid)
    assert rows and all(a["confidence"] <= UNVERIFIED_CAP and a["status_applied"] is None for a in rows)
    assert email_row(db, gid)["requires_attention"] == 1
    assert any("uncertain" in r for r in review_reasons(db, gid))


def test_lookalike_does_not_resolve_unknown_outcome(gctx, make_job):
    db = gctx.db
    app = _submitted_via_approval(db, acme_job(make_job))
    sync(gctx, mail("m-unknown", "Acme Analytics <careers@acme-analytics.xyz>", *CONFIRM))
    row = repo.get_application(db, app)
    assert row["status"] == "needs_manual_action" and row["confirmation_status"] != "email_confirmed"


def test_name_spelled_domain_needs_one_confirmation_then_is_trusted(gctx, make_job):
    """acmeanalytics.com is not the job's site (posting on Greenhouse): ask once, then trust that exact domain."""
    db = gctx.db
    app = make_application(db, acme_job(make_job), "submitted", submitted_at=SUBMITTED_AT)
    first = mail("m-first", "Acme Analytics Careers <careers@acmeanalytics.com>", "Your application is under review",
                 "Hi Test, thank you for your application for the Business Analyst position. Our team will review "
                 "it. Please share your availability for a short call.", received="2026-10-05T12:00:00Z")
    sync(gctx, first)
    top = assocs(db, "m-first")[0]
    assert top["method"] == "domain_name" or top["confidence"] <= UNVERIFIED_CAP
    assert top["confidence"] < AUTO_THRESHOLD and app_status(db, app) == "submitted"

    app_uid = repo.get_application(db, app)["application_uid"]
    associate_email(gctx, email_row(db, "m-first")["id"], application_ref=app_uid)  # you confirm the sender

    sync(gctx, mail("m-later", "Acme Analytics Careers <careers@acmeanalytics.com>", *REJECT,
                    received="2026-10-09T12:00:00Z"))
    later = assocs(db, "m-later")[0]
    assert later["method"] == "confirmed_domain" and later["confidence"] >= AUTO_THRESHOLD
    assert app_status(db, app) == "rejected"

    # ...but only that exact domain: a look-alike stays untrusted.
    job2 = acme_job(make_job, title="Data Analyst", title_normalized="data analyst")
    app2 = make_application(db, job2, "submitted", submitted_at=SUBMITTED_AT)
    sync(gctx, mail("m-xyz", "Acme Analytics <careers@acmeanalytics.xyz>", "Your application to Acme Analytics",
                    "Thank you for your interest in the Data Analyst position. Unfortunately, we have decided not "
                    "to move forward with your application."))
    assert app_status(db, app2) == "submitted"


def test_platform_domain_confirmation_is_not_a_trusted_employer_domain(gctx, make_job):
    """Confirming one Greenhouse email must not make every greenhouse-mail.io sender 'Acme'."""
    db = gctx.db
    app = make_application(db, acme_job(make_job), "submitted", submitted_at=SUBMITTED_AT)
    sync(gctx, mail("m-gh", "Hiring Team <no-reply@us.greenhouse-mail.io>", "An update", "Unfortunately we will "
                    "not be moving forward.", received="2026-10-05T12:00:00Z"))
    associate_email(gctx, email_row(db, "m-gh")["id"], application_ref=repo.get_application(db, app)["application_uid"])
    cands = find_candidates(db, thread_id="t-new", from_address="no-reply@us.greenhouse-mail.io",
                            from_domain="us.greenhouse-mail.io", from_name="Hiring Team", subject="Update",
                            body="Unfortunately we will not be moving forward.")
    assert all(c.method != "confirmed_domain" for c in cands)


def test_job_on_a_shared_host_does_not_trust_that_hosts_mail(gctx, make_job):
    """A job posted as a Google Form must not make every google.com sender 'the employer'."""
    db = gctx.db
    app = make_application(db, acme_job(make_job, job_url="https://docs.google.com/forms/d/e/abc/viewform"),
                           "submitted", submitted_at=SUBMITTED_AT)
    res = sync(gctx, mail("m-goog", "Google Careers <careers-noreply@google.com>", *REJECT))
    assert res.status_updates == 0 and app_status(db, app) == "submitted"


def test_exact_job_domain_still_updates(gctx, make_job):
    db = gctx.db
    app = make_application(db, acme_job(make_job, job_url="https://careers.acmeanalytics.com/jobs/1"), "submitted",
                           submitted_at=SUBMITTED_AT)
    res = sync(gctx, mail("m-real", "Acme Talent <talent@mail.acmeanalytics.com>", *REJECT))
    assert res.status_updates == 1 and app_status(db, app) == "rejected"
    event = db.query_one("SELECT * FROM application_events WHERE application_id = ? ORDER BY id DESC LIMIT 1",
                         (app,))
    assert event["actor"] == "email" and from_json(event["evidence"])["gmail_message_id"] == "m-real"


# --------------------------------------------------------------------------- #
# 2. Same employer, different role
# --------------------------------------------------------------------------- #
def test_rejection_for_a_different_role_does_not_reject_this_application(gctx, make_job):
    db = gctx.db
    app = make_application(db, acme_job(make_job, job_url="https://careers.acmeanalytics.com/jobs/1"), "submitted",
                           submitted_at=SUBMITTED_AT)
    res = sync(gctx, mail("m2", "Acme Talent <talent@acmeanalytics.com>",
                          "Update on your Senior Data Engineer application",
                          "Thank you for your interest in the Senior Data Engineer role (req 7781). After careful "
                          "review we have decided not to move forward with your application for this position."))
    assert res.status_updates == 0 and app_status(db, app) == "submitted"
    top = assocs(db, "m2")[0]
    assert top["confidence"] < AUTO_THRESHOLD
    assert email_row(db, "m2")["requires_attention"] == 1
    assert any("different role" in r and "Senior Data Engineer" in r for r in review_reasons(db, "m2"))


def test_several_applications_at_one_employer_need_the_role_named(gctx, make_job):
    db = gctx.db
    url = "https://careers.acmeanalytics.com/jobs/"
    ba = make_application(db, acme_job(make_job, job_url=url + "1"), "submitted", submitted_at=SUBMITTED_AT)
    da = make_application(db, acme_job(make_job, title="Data Analyst", title_normalized="data analyst",
                                       job_url=url + "2"), "submitted", submitted_at=SUBMITTED_AT)
    untitled = mail("m-untitled", "Acme Talent <talent@acmeanalytics.com>", "Your application",
                    "Unfortunately, we have decided not to move forward with your application.",
                    received="2026-10-08T12:00:00Z")
    res = sync(gctx, untitled)
    assert res.status_updates == 0
    assert (app_status(db, ba), app_status(db, da)) == ("submitted", "submitted")
    assert email_row(db, "m-untitled")["requires_attention"] == 1

    named = mail("m-named", "Acme Talent <talent@acmeanalytics.com>", "Your application",
                 "Thank you for applying for the Data Analyst position. Unfortunately, we have decided not to move "
                 "forward with your application.", received="2026-10-09T12:00:00Z")
    res = sync(gctx, named)
    assert res.status_updates == 1
    assert (app_status(db, ba), app_status(db, da)) == ("submitted", "rejected")


def test_matching_role_still_updates_single_application(gctx, make_job):
    db = gctx.db
    app = make_application(db, acme_job(make_job, job_url="https://careers.acmeanalytics.com/jobs/1",
                                        title="Business Analyst (Remote)"), "submitted", submitted_at=SUBMITTED_AT)
    res = sync(gctx, mail("m-ok", "Acme Talent <talent@acmeanalytics.com>", *REJECT))
    assert res.status_updates == 1 and app_status(db, app) == "rejected"


def test_title_helpers():
    titles = associate.mentioned_titles
    assert titles("Update on your Senior Data Engineer application", "") == ["Senior Data Engineer"]
    assert titles("Thank You For Your Interest In The Business Analyst Role", "") == ["Business Analyst"]
    assert titles("Update on your Acme application", "Thank you for applying.") == []
    assert titles("Complete your Workday application", "") == []
    assert associate.titles_match("Business Analyst", "Business Analyst (Remote)")
    assert associate.titles_match("Data Analyst II", "Data Analyst")
    assert not associate.titles_match("Senior Data Engineer", "Data Engineer")

    def status(title, body, mentioned=()):
        return associate.title_status(title, "Acme", list(mentioned), subject="x", body=body)[0]

    assert status("Data Engineer", "the senior data engineer role") == "none"
    assert status("Data Engineer", "the data engineer role") == "match"
    assert status("Business Analyst", "", ["Marketing Coordinator"]) == "mismatch"


# --------------------------------------------------------------------------- #
# 3. Never-submitted applications and unknown outcomes
# --------------------------------------------------------------------------- #
def test_confirmation_for_other_role_never_confirms_never_submitted_app(gctx, make_job):
    db = gctx.db
    app = make_application(db, acme_job(make_job, job_url="https://careers.acmeanalytics.com/jobs/1"),
                           "needs_manual_action")
    m = mail("m3", "Acme Careers <careers@acmeanalytics.com>", "We've received your application",
             "Thank you for applying for the Marketing Coordinator position. We have received your application and "
             "our team will review it.")
    svc = FakeGmailService([m])
    for _ in range(3):
        res = sync_gmail(gctx, service=svc)
        assert res.errors == []
    row = repo.get_application(db, app)
    assert row["status"] == "needs_manual_action" and row["submitted_at"] is None
    assert db.scalar("SELECT COUNT(*) FROM email_messages") == 1 and len(svc.get_calls) == 1


def test_genuine_confirmation_for_never_submitted_app_is_stored_once_and_left_to_you(gctx, make_job):
    """SEC-8: the repo guard must not roll back the message (re-downloaded and re-errored every sync)."""
    db = gctx.db
    app = make_application(db, acme_job(make_job, job_url="https://careers.acmeanalytics.com/jobs/1"),
                           "needs_manual_action")
    svc = FakeGmailService([mail("m-nma", "Acme Analytics <careers@acmeanalytics.com>", *CONFIRM)])
    for i in range(3):
        res = sync_gmail(gctx, service=svc)
        assert res.errors == [] and res.new == (1 if i == 0 else 0)
    assert len(svc.get_calls) == 1
    assert db.scalar("SELECT COUNT(*) FROM email_messages") == 1
    assert db.scalar("SELECT COUNT(*) FROM errors") == 0
    assert app_status(db, app) == "needs_manual_action"
    email = email_row(db, "m-nma")
    assert email["requires_attention"] == 1
    assert any("never submitted" in r for r in review_reasons(db, "m-nma"))

    # You applied manually after the CAPTCHA: confirm it yourself.
    res = associate_email(gctx, email["id"], application_ref=repo.get_application(db, app)["application_uid"],
                          apply_status=True)
    assert res.status_applied == "confirmation_received"
    assert app_status(db, app) == "confirmation_received"


def test_user_only_correction_is_a_skip_reason_not_an_error(gctx, make_job):
    db = gctx.db
    app = make_application(db, acme_job(make_job, job_url="https://careers.acmeanalytics.com/jobs/1"), "rejected",
                           submitted_at=SUBMITTED_AT)
    svc = FakeGmailService([mail("m-int", "Acme Talent <talent@acmeanalytics.com>", "Interview invitation",
                                 "We would like to invite you to a first-round interview for the Business Analyst "
                                 "position.")])
    res = sync_gmail(gctx, service=svc)
    assert res.errors == [] and res.new == 1 and app_status(db, app) == "rejected"
    assert any("correction only you can make" in r for r in review_reasons(db, "m-int"))
    assert sync_gmail(gctx, service=svc).fetched == 0


def test_refused_transition_keeps_the_message_and_records_the_error(gctx, make_job, monkeypatch):
    db = gctx.db
    app = make_application(db, acme_job(make_job, job_url="https://careers.acmeanalytics.com/jobs/1"), "submitted",
                           submitted_at=SUBMITTED_AT)

    def refuse(*args, **kwargs):
        raise InvalidTransition("concurrent change")

    monkeypatch.setattr(email_status.repo, "transition_application", refuse)
    svc = FakeGmailService([mail("m-race", "Acme Talent <talent@acmeanalytics.com>", *REJECT)])
    res = sync_gmail(gctx, service=svc)
    assert res.errors == [] and res.new == 1 and res.status_updates == 0
    assert app_status(db, app) == "submitted"
    assert db.scalar("SELECT COUNT(*) FROM errors WHERE component = 'email:status'") == 1
    assert email_row(db, "m-race")["requires_attention"] == 1
    assert assocs(db, "m-race")[0]["status_applied"] is None
    assert sync_gmail(gctx, service=svc).fetched == 0  # stored: never downloaded again


@pytest.mark.parametrize("current,attempts,submitted,actor,expected", [
    (S.NEEDS_MANUAL_ACTION, 1, None, "email", None),
    (S.NEEDS_MANUAL_ACTION, 0, None, "email", None),
    (S.NEEDS_MANUAL_ACTION, 0, None, "user", S.CONFIRMATION_RECEIVED),
    (S.SUBMITTED, 0, None, "email", None),
    (S.SUBMITTED, 0, SUBMITTED_AT, "email", S.CONFIRMATION_RECEIVED),
    (S.SUBMISSION_ATTEMPTED, 1, None, "email", S.CONFIRMATION_RECEIVED),
    (S.INTERVIEW_REQUESTED, 0, SUBMITTED_AT, "email", None),
])
def test_check_transition_confirmation(current, attempts, submitted, actor, expected):
    target, reason = email_status.check_transition(current, EmailCategory.APPLICATION_CONFIRMATION, actor=actor,
                                                   submission_attempts=attempts, submitted_at=submitted)
    assert target == expected
    assert (reason is None) == (expected is not None)


def test_check_transition_mirrors_user_only_corrections():
    target, reason = email_status.check_transition(S.REJECTED, EmailCategory.INTERVIEW_INVITATION, actor="email",
                                                   submitted_at=SUBMITTED_AT)
    assert target is None and "only you" in reason
    target, _ = email_status.check_transition(S.REJECTED, EmailCategory.INTERVIEW_INVITATION, actor="user",
                                              submitted_at=SUBMITTED_AT)
    assert target == S.INTERVIEW_REQUESTED
    target, _ = email_status.check_transition(S.SUBMITTED, EmailCategory.REJECTION, actor="email",
                                              submitted_at=SUBMITTED_AT)
    assert target == S.REJECTED


# --------------------------------------------------------------------------- #
# 4. Marketing "offers" and negated offers
# --------------------------------------------------------------------------- #
def test_marketing_offer_from_employer_brand_is_not_an_offer(gctx, make_job):
    db = gctx.db
    job = make_job(employer="Capital One", employer_normalized="capital one", title="Business Analyst",
                   title_normalized="business analyst", job_url="https://www.capitalone.com/careers/1")
    app = make_application(db, job, "submitted", submitted_at=SUBMITTED_AT)
    res = sync(gctx, mail("m4", "Capital One <capitalone@notification.capitalone.com>", "Test, your offer is ready",
                          "Good news: your offer is waiting. You're pre-approved for a new card with no annual fee. "
                          "To stop receiving these messages, unsubscribe here."))
    assert res.status_updates == 0 and app_status(db, app) == "submitted"
    assert email_row(db, "m4")["category"] != "offer"


@pytest.mark.parametrize("subject,body", [
    ("Update on your offer", "Dear Test, due to a hiring freeze we must rescind your offer for the Business Analyst "
                             "position."),
    ("Your application", "We are unable to extend an offer of employment for the Business Analyst position."),
    ("Your application", "We are not able to offer you the Business Analyst position at this time."),
])
def test_negated_or_rescinded_offer_never_sets_offer_received(gctx, make_job, subject, body):
    db = gctx.db
    app = make_application(db, acme_job(make_job, job_url="https://careers.acmeanalytics.com/jobs/1"),
                           "interview_requested", submitted_at=SUBMITTED_AT)
    sync(gctx, mail("m-neg", "Acme HR <hr@acmeanalytics.com>", subject, body))
    assert app_status(db, app) != "offer_received"
    assert email_row(db, "m-neg")["category"] in {"rejection", "other"}


def test_promotional_markers_block_automatic_updates(gctx, make_job):
    db = gctx.db
    app = make_application(db, acme_job(make_job, job_url="https://careers.acmeanalytics.com/jobs/1"), "submitted",
                           submitted_at=SUBMITTED_AT)
    res = sync(gctx, mail("m-promo", "Acme Analytics <news@acmeanalytics.com>", *(REJECT[0], REJECT[1] +
                          "\n\nManage your email preferences or unsubscribe.")))
    assert res.status_updates == 0 and app_status(db, app) == "submitted"
    assert "bulk or promotional mail" in review_reasons(db, "m-promo")
    assert email_row(db, "m-promo")["requires_attention"] == 1


def test_genuine_offer_still_applies(gctx, make_job):
    db = gctx.db
    app = make_application(db, acme_job(make_job, job_url="https://careers.acmeanalytics.com/jobs/1"),
                           "interview_requested", submitted_at=SUBMITTED_AT)
    res = sync(gctx, mail("m-offer", "Acme HR <hr@acmeanalytics.com>", "Offer letter - Business Analyst",
                          "We are pleased to extend you an offer for the Business Analyst position. We reserve the "
                          "right to rescind this offer if the background check is not completed."))
    assert res.status_updates == 1 and app_status(db, app) == "offer_received"
    assert email_row(db, "m-offer")["requires_attention"] == 1  # offers always need you


# --------------------------------------------------------------------------- #
# 5. A shared word is not the employer
# --------------------------------------------------------------------------- #
def test_longer_company_name_from_its_own_domain_is_not_the_employer(gctx, make_job):
    db = gctx.db
    job = make_job(employer="Mercury", employer_normalized="mercury", title="Data Analyst",
                   title_normalized="data analyst", job_url="https://jobs.ashbyhq.com/mercury/1")
    app = make_application(db, job, "submitted", submitted_at=SUBMITTED_AT)
    res = sync(gctx, mail("m5c", "Careers <careers@mercuryinsurance.com>", "Your application",
                          "Thank you for your interest in Mercury Insurance. Unfortunately, we will not be moving "
                          "forward with your application.\n\nThe Mercury Talent Team"))
    assert res.status_updates == 0 and app_status(db, app) == "submitted"
    assert assocs(db, "m5c") == []


def test_longer_company_name_is_not_the_employer(gctx, make_job):
    db = gctx.db
    job = make_job(employer="Mercury", employer_normalized="mercury", title="Data Analyst",
                   title_normalized="data analyst", job_url="https://jobs.ashbyhq.com/mercury/1")
    app = make_application(db, job, "submitted", submitted_at=SUBMITTED_AT)
    res = sync(gctx, mail("m5", "Mercury Insurance Careers <no-reply@mercuryinsurance.icims.com>",
                          "Your application with Mercury Insurance",
                          "Thank you for your interest in Mercury Insurance. Unfortunately, we will not be moving "
                          "forward with your application at this time."))
    assert res.status_updates == 0 and app_status(db, app) == "submitted"
    assert assocs(db, "m5") == []


def test_platform_mail_naming_the_full_employer_still_links(gctx, make_job):
    """Genuine Greenhouse mail about a job posted on Greenhouse, naming the employer and the role."""
    db = gctx.db
    job = make_job(employer="Mercury", employer_normalized="mercury", title="Data Analyst",
                   title_normalized="data analyst", job_url="https://boards.greenhouse.io/mercury/jobs/1")
    app = make_application(db, job, "submitted", submitted_at=SUBMITTED_AT)
    res = sync(gctx, mail("m5b", "Mercury Careers <no-reply@us.greenhouse-mail.io>", "Your application to Mercury",
                          "Thank you for applying for the Data Analyst position at Mercury. Unfortunately, we will "
                          "not be moving forward with your application.",
                          auth=gmail_ar("us.greenhouse-mail.io", dkim_domain="greenhouse-mail.io")))
    assert res.status_updates == 1 and app_status(db, app) == "rejected"
    assert assocs(db, "m5b")[0]["method"] == "platform"


def test_platform_mail_for_a_job_posted_elsewhere_waits_for_you(gctx, make_job):
    """The job is on Ashby; a Greenhouse account calling itself "Mercury" proves nothing."""
    db = gctx.db
    job = make_job(employer="Mercury", employer_normalized="mercury", title="Data Analyst",
                   title_normalized="data analyst", job_url="https://jobs.ashbyhq.com/mercury/1")
    app = make_application(db, job, "submitted", submitted_at=SUBMITTED_AT)
    res = sync(gctx, mail("m5d", "Mercury Careers <no-reply@us.greenhouse-mail.io>", "Your application to Mercury",
                          "Thank you for applying for the Data Analyst position at Mercury. Unfortunately, we will "
                          "not be moving forward with your application.",
                          auth=gmail_ar("us.greenhouse-mail.io", dkim_domain="greenhouse-mail.io")))
    assert res.status_updates == 0 and app_status(db, app) == "submitted"
    top = assocs(db, "m5d")[0]
    assert top["confidence"] <= UNVERIFIED_CAP and email_row(db, "m5d")["requires_attention"] == 1
    assert any("not posted on greenhouse" in r for r in review_reasons(db, "m5d"))


def test_names_employer_rules():
    assert not associate.names_employer("Mercury Insurance Careers", "Mercury")
    assert not associate.names_employer("Your application with Mercury Insurance", "Mercury")
    assert not associate.names_employer("MERCURY INSURANCE", "Mercury")
    assert not associate.names_employer("Thanks for applying to Mercury Insurance.\n\nThe Mercury team", "Mercury")
    assert associate.names_employer("Thank you for applying to Mercury.", "Mercury")
    assert associate.names_employer("Mercury Careers", "Mercury")
    assert associate.names_employer("Mercury's recruiting team", "Mercury")
    assert associate.names_employer("Acme Analytics Hiring Team", "Acme Analytics, Inc.")
    assert associate.names_employer("Your Application To Acme Analytics Was Received", "Acme Analytics")
    assert associate.names_employer("Acme Analytics Business Analyst update", "Acme Analytics",
                                    title="Business Analyst")
    assert not associate.names_employer("Acme Analytics Group", "Acme Analytics")
    assert associate.names_employer("Acme Group careers", "Acme Group")


# --------------------------------------------------------------------------- #
# 6. Forged senders
# --------------------------------------------------------------------------- #
OFFER = ("Offer letter - Business Analyst", "We are pleased to extend you an offer for the Business Analyst position.")


@pytest.mark.parametrize("auth", [
    "mx.google.com; spf=fail smtp.mailfrom=evil.example; dkim=none; dmarc=fail",
    None,  # no Authentication-Results at all
    # written by someone other than Gmail
    "mx.acme-relay.example; dkim=pass header.i=@acmeanalytics.com; dmarc=pass header.from=acmeanalytics.com",
    # Gmail says DKIM passed -- for a different domain; no DMARC policy
    "mx.google.com; dkim=pass header.i=@evil.example; spf=pass smtp.mailfrom=evil.example; dmarc=none",
    # SPF alone never counts
    "mx.google.com; spf=pass smtp.mailfrom=acmeanalytics.com; dkim=none",
])
def test_unverified_sender_never_changes_status(gctx, make_job, auth):
    db = gctx.db
    app = make_application(db, acme_job(make_job, job_url="https://careers.acmeanalytics.com/jobs/1"),
                           "interview_requested", submitted_at=SUBMITTED_AT)
    res = sync(gctx, mail("m6", "Acme HR <hr@acmeanalytics.com>", *OFFER, auth=auth))
    assert res.status_updates == 0 and app_status(db, app) == "interview_requested"
    assert email_row(db, "m6")["requires_attention"] == 1
    assert any(r.startswith("sender not verified") for r in review_reasons(db, "m6"))


def test_forged_gmail_header_below_the_real_one_is_ignored(gctx, make_job):
    db = gctx.db
    app = make_application(db, acme_job(make_job, job_url="https://careers.acmeanalytics.com/jobs/1"),
                           "interview_requested", submitted_at=SUBMITTED_AT)
    forged = gmail_ar("acmeanalytics.com")  # sender-supplied, claims a pass
    m = mail("m7", "Acme HR <hr@acmeanalytics.com>", *OFFER, auth=forged,
             headers={"ARC-Authentication-Results": "i=9; mx.google.com; dmarc=pass header.from=acmeanalytics.com"})
    # Gmail prepends its own verdict on top.
    m["payload"]["headers"].insert(0, {"name": "Authentication-Results",
                                       "value": "mx.google.com; dkim=none; spf=softfail smtp.mailfrom=evil.example; "
                                                "dmarc=fail (p=NONE) header.from=acmeanalytics.com"})
    res = sync(gctx, m)
    assert res.status_updates == 0 and app_status(db, app) == "interview_requested"


def test_forged_arc_header_without_gmail_results_is_ignored(gctx, make_job):
    db = gctx.db
    app = make_application(db, acme_job(make_job, job_url="https://careers.acmeanalytics.com/jobs/1"),
                           "interview_requested", submitted_at=SUBMITTED_AT)
    m = mail("m8", "Acme HR <hr@acmeanalytics.com>", *OFFER, auth=None,
             headers={"ARC-Authentication-Results": "i=1; mx.google.com; dkim=pass header.i=@acmeanalytics.com; "
                                                    "dmarc=pass header.from=acmeanalytics.com"})
    res = sync(gctx, m)
    assert res.status_updates == 0 and app_status(db, app) == "interview_requested"


def test_verified_sender_offer_applies(gctx, make_job):
    db = gctx.db
    app = make_application(db, acme_job(make_job, job_url="https://careers.acmeanalytics.com/jobs/1"),
                           "interview_requested", submitted_at=SUBMITTED_AT)
    res = sync(gctx, mail("m9", "Acme HR <hr@acmeanalytics.com>", *OFFER))
    assert res.status_updates == 1 and app_status(db, app) == "offer_received"


def test_unverified_cap_constant_matches():
    assert associate.UNVERIFIED_CAP == UNVERIFIED_CAP < AUTO_THRESHOLD


# --------------------------------------------------------------------------- #
# 7. Round-2 attacks (final verification): each message passes Gmail's DKIM/DMARC for its own domain
# --------------------------------------------------------------------------- #
GH_AUTH = gmail_ar("us.greenhouse-mail.io", dkim_domain="greenhouse-mail.io")


@pytest.mark.parametrize("employer,other", [("Mercury", "Blue Mercury"), ("Express", "American Express"),
                                            ("Mercury", "blue mercury")])  # the sender picks the case
def test_ats_mail_from_a_longer_company_ending_in_the_employer_name(gctx, make_job, employer, other):
    """E1: 'Blue Mercury' is not Mercury, even on the ATS the Mercury job was posted on."""
    db = gctx.db
    job = make_job(employer=employer, employer_normalized=employer.lower(), title="Data Analyst",
                   title_normalized="data analyst", job_url=f"https://boards.greenhouse.io/{employer.lower()}/jobs/1")
    app = make_application(db, job, "submitted", submitted_at=SUBMITTED_AT)
    res = sync(gctx, mail("r2-e1", f"{other} <no-reply@us.greenhouse-mail.io>", f"Your application to {other}",
                          f"Thank you for applying to the Data Analyst position at {other}. Unfortunately, we will "
                          "not be moving forward with your application at this time.", auth=GH_AUTH))
    assert res.status_updates == 0 and app_status(db, app) == "submitted"
    assert all(a["confidence"] < AUTO_THRESHOLD for a in assocs(db, "r2-e1"))


def test_ats_mail_with_look_alike_letters_in_the_longer_name(gctx, make_job):
    """'Вlue Mercury' with a Cyrillic В must not lose its first word and become 'Mercury'."""
    db = gctx.db
    job = make_job(employer="Mercury", employer_normalized="mercury", title="Data Analyst",
                   title_normalized="data analyst", job_url="https://boards.greenhouse.io/mercury/jobs/1")
    app = make_application(db, job, "submitted", submitted_at=SUBMITTED_AT)
    sync(gctx, mail("r2-e1h", "Вlue Mercury <no-reply@us.greenhouse-mail.io>", "Your application to Вlue Mercury",
                    "Thank you for applying to the Data Analyst position at Вlue Mercury. Unfortunately, we "
                    "will not be moving forward.", auth=GH_AUTH))
    assert app_status(db, app) == "submitted"


def test_self_serve_ats_account_named_like_the_employer(gctx, make_job):
    """E2: a Workable account's display name is whatever its holder typed; the job is on Acme's own site."""
    db = gctx.db
    app = make_application(db, acme_job(make_job, job_url="https://careers.acmeanalytics.com/jobs/1"), "submitted",
                           submitted_at=SUBMITTED_AT)
    res = sync(gctx, mail("r2-e2", "Acme Analytics <noreply@candidates.workablemail.com>",
                          "Your application to Acme Analytics",
                          "Thank you for applying for the Business Analyst position. Unfortunately, we have decided "
                          "to move forward with other candidates.",
                          auth=gmail_ar("candidates.workablemail.com", dkim_domain="workablemail.com")))
    assert res.status_updates == 0 and app_status(db, app) == "submitted"
    assert all(a["confidence"] <= UNVERIFIED_CAP for a in assocs(db, "r2-e2"))
    assert email_row(db, "r2-e2")["requires_attention"] == 1
    assert any("anyone with an account there" in r for r in review_reasons(db, "r2-e2"))


def test_ats_account_of_the_job_posting_is_trusted_and_another_account_is_not(gctx, make_job):
    """Workday: the job is on acme's Workday site; acme@myworkday.com is that account, globex@myworkday.com is not."""
    db = gctx.db
    job = make_job(employer="Acme", employer_normalized="acme", title="Data Analyst", title_normalized="data analyst",
                   job_url="https://acme.wd5.myworkdayjobs.com/en-US/External/job/NYC/Data-Analyst_R123")
    app = make_application(db, job, "submitted", submitted_at=SUBMITTED_AT)
    text = ("Thank you for applying for the Data Analyst position at Acme. Unfortunately, we will not be moving "
            "forward with your application.")
    sync(gctx, mail("r2-wd-other", "Acme <globex@myworkday.com>", "Your Data Analyst application at Acme", text,
                    auth=gmail_ar("myworkday.com"), received="2026-10-10T12:00:00Z"))
    assert app_status(db, app) == "submitted"
    assert any("not the one the job was posted by" in r for r in review_reasons(db, "r2-wd-other"))
    res = sync(gctx, mail("r2-wd", "Acme <acme@myworkday.com>", "Your Data Analyst application at Acme", text,
                          auth=gmail_ar("myworkday.com")))
    assert res.status_updates == 1 and app_status(db, app) == "rejected"
    assert assocs(db, "r2-wd")[0]["method"] == "platform"


def test_ats_account_you_confirmed_is_trusted_afterwards(gctx, make_job):
    """acmeanalytics.icims.com for a job on Acme's own site: ask once, then trust that account (and only it)."""
    db = gctx.db
    app = make_application(db, acme_job(make_job, job_url="https://careers.acmeanalytics.com/jobs/1"), "submitted",
                           submitted_at=SUBMITTED_AT)
    first = mail("r2-icims-1", "Acme Analytics <no-reply@acmeanalytics.icims.com>",
                 "Thank you for applying to Acme Analytics", CONFIRM[1], received="2026-10-05T12:00:00Z",
                 auth=gmail_ar("acmeanalytics.icims.com", dkim_domain="icims.com"))
    assert sync(gctx, first).status_updates == 0 and app_status(db, app) == "submitted"
    associate_email(gctx, email_row(db, "r2-icims-1")["id"], application_ref=repo.get_application(db, app)[
        "application_uid"], apply_status=True)
    assert app_status(db, app) == "confirmation_received"
    later = mail("r2-icims-2", "Acme Analytics <no-reply@acmeanalytics.icims.com>", *REJECT,
                 auth=gmail_ar("acmeanalytics.icims.com", dkim_domain="icims.com"))
    res = sync(gctx, later)
    assert res.status_updates == 1 and app_status(db, app) == "rejected"
    assert assocs(db, "r2-icims-2")[0]["method"] == "confirmed_platform"


@pytest.mark.parametrize("body", [
    "Thank you for interviewing for the Business Analyst position. We were pleased to extend an offer to another "
    "candidate, who has accepted it. We wish you the best in your search.",
    "After careful consideration, we were happy to make an offer to another candidate for the Business Analyst "
    "position.",
    "We were thrilled to extend an offer to a candidate whose experience more closely matched the role.",
    "Thank you for your time. We are pleased to extend this offer to the candidate we selected for the position.",
    "We are delighted to offer you the position of applicant in our talent community for future roles.",
    "We are pleased to extend an offer to anоther candidate for the Business Analyst position.",  # Cyrillic о
])
def test_offer_made_to_someone_else_never_sets_offer_received(gctx, make_job, body):
    """E3: these come from the job's own domain and name the role; none is an offer to you."""
    db = gctx.db
    app = make_application(db, acme_job(make_job, job_url="https://careers.acmeanalytics.com/jobs/1"), "submitted",
                           submitted_at=SUBMITTED_AT)
    sync(gctx, mail("r2-e3", "Acme Talent <talent@acmeanalytics.com>", "Business Analyst position update", body))
    assert app_status(db, app) == "submitted"
    row = email_row(db, "r2-e3")
    assert row["requires_attention"] == 1
    assert row["category"] != "offer" or row["category_confidence"] < 0.8


def test_position_filled_rejection_still_applies(gctx, make_job):
    db = gctx.db
    app = make_application(db, acme_job(make_job, job_url="https://careers.acmeanalytics.com/jobs/1"), "submitted",
                           submitted_at=SUBMITTED_AT)
    res = sync(gctx, mail("r2-filled", "Acme Talent <talent@acmeanalytics.com>", "Business Analyst position update",
                          "Thank you for your interest in the Business Analyst position. Unfortunately, the position "
                          "has been filled."))
    assert res.status_updates == 1 and app_status(db, app) == "rejected"


@pytest.mark.parametrize("subject,body", [
    ("Senior Data Engineer (Req 7781) - next steps",
     "Thank you for your interest in joining Acme Analytics as a Senior Data Engineer. Unfortunately, we will not be "
     "moving forward with your candidacy."),
    ("Re: Req 7781 / Senior Data Engineer",
     "Hi Test, the Senior Data Engineer opening (req 7781) has been closed and we have decided not to move forward."),
    ("Your application", "Unfortunately, we will not be moving forward with your candidacy."),
    ("Your application", "Thank you for applying for the Business Analyst position. We have decided to move forward "
                         "with other candidates for the Senior Data Engineer role."),
])
def test_email_must_name_this_role_to_change_its_status(gctx, make_job, subject, body):
    """E4: another role named in an unusual way, no role at all, or two roles: you decide."""
    db = gctx.db
    app = make_application(db, acme_job(make_job, job_url="https://careers.acmeanalytics.com/jobs/1"), "submitted",
                           submitted_at=SUBMITTED_AT)
    res = sync(gctx, mail("r2-e4", "Acme Talent <talent@acmeanalytics.com>", subject, body))
    assert res.status_updates == 0 and app_status(db, app) == "submitted"
    assert assocs(db, "r2-e4")[0]["confidence"] < AUTO_THRESHOLD
    assert email_row(db, "r2-e4")["requires_attention"] == 1
    reasons = " ".join(review_reasons(db, "r2-e4"))
    assert "role" in reasons


def test_role_named_only_in_the_subject_still_applies(gctx, make_job):
    db = gctx.db
    app = make_application(db, acme_job(make_job, job_url="https://careers.acmeanalytics.com/jobs/1"), "submitted",
                           submitted_at=SUBMITTED_AT)
    res = sync(gctx, mail("r2-subj", "Acme Talent <talent@acmeanalytics.com>", "Business Analyst - next steps",
                          "Hi Test, unfortunately we will not be moving forward with your candidacy."))
    assert res.status_updates == 1 and app_status(db, app) == "rejected"


def test_authentication_results_injection_via_quoted_mailfrom(gctx, make_job):
    """E5: ';dmarc=pass ...' inside a quoted smtp.mailfrom local part is not a result; dmarc=fail stands."""
    db = gctx.db
    app = make_application(db, acme_job(make_job, job_url="https://careers.acmeanalytics.com/jobs/1"),
                           "interview_requested", submitted_at=SUBMITTED_AT)
    inj = '"x;dmarc=pass header.from=acmeanalytics.com;dkim=pass header.i=@acmeanalytics.com"@evil.io'
    ar = (f"mx.google.com; dkim=pass header.i=@evil.io header.s=s1 header.b=AbC; spf=pass (google.com: domain of "
          f"{inj} designates 192.0.2.7 as permitted sender) smtp.mailfrom={inj}; dmarc=fail (p=NONE sp=NONE "
          f"dis=NONE) header.from=acmeanalytics.com")
    res = sync(gctx, mail("r2-e5", "Acme HR <hr@acmeanalytics.com>", *OFFER, auth=ar))
    assert res.status_updates == 0 and app_status(db, app) == "interview_requested"
    assert any(r.startswith("sender not verified") and "dmarc=fail" in r for r in review_reasons(db, "r2-e5"))


def test_message_from_another_domain_in_a_linked_thread_is_not_trusted(gctx, make_job):
    """A genuine Acme thread; a later, authenticated evil.io message in it changes nothing."""
    db = gctx.db
    app = make_application(db, acme_job(make_job, job_url="https://careers.acmeanalytics.com/jobs/1"), "submitted",
                           submitted_at=SUBMITTED_AT)
    sync(gctx, mail("r2-t1", "Acme Careers <careers@acmeanalytics.com>", *CONFIRM, thread="T1",
                    received="2026-10-09T15:00:00Z"))
    assert app_status(db, app) == "confirmation_received"
    res = sync(gctx, mail("r2-t2", "Recruiting <jobs@evil.io>", "Re: Your application to Acme Analytics",
                          "Unfortunately, we will not be moving forward with your Business Analyst application.",
                          thread="T1"))
    assert res.status_updates == 0 and app_status(db, app) == "confirmation_received"
    rows = assocs(db, "r2-t2")
    assert rows and all(a["confidence"] < AUTO_THRESHOLD and a["method"] != "thread" for a in rows)
    assert email_row(db, "r2-t2")["requires_attention"] == 1


def test_reply_from_the_same_domain_in_a_linked_thread_needs_no_role(gctx, make_job):
    db = gctx.db
    app = make_application(db, acme_job(make_job, job_url="https://careers.acmeanalytics.com/jobs/1"), "submitted",
                           submitted_at=SUBMITTED_AT)
    sync(gctx, mail("r2-t3", "Acme Careers <careers@acmeanalytics.com>", *CONFIRM, thread="T2",
                    received="2026-10-09T15:00:00Z"))
    res = sync(gctx, mail("r2-t4", "Jo Smith <jo.smith@mail.acmeanalytics.com>", "Re: Thank you for applying",
                          "Hi Test, unfortunately we will not be moving forward.", thread="T2"))
    assert res.status_updates == 1 and app_status(db, app) == "rejected"
    assert assocs(db, "r2-t4")[0]["method"] == "thread"


# ---------------------------------------------------------------- helpers ---- #
def test_names_employer_checks_the_word_before():
    assert not associate.names_employer("Your application to Blue Mercury", "Mercury")
    assert not associate.names_employer("American Express <no-reply@us.greenhouse-mail.io>", "Express")
    assert not associate.names_employer("BLUE MERCURY CAREERS", "Mercury")
    assert not associate.names_employer("Blue-Mercury recruiting", "Mercury")
    assert not associate.names_employer("Thank you for applying to Bank of America.", "America")
    assert not associate.names_employer("Ernst & Young careers", "Ernst")
    assert not associate.names_employer("Вlue Mercury", "Mercury")  # Cyrillic В
    assert not associate.names_employer("蓝 Mercury", "Mercury")  # another script's word before
    assert associate.employer_mention("Thanks from Blue Mercury", "Mercury") == "other"
    # subjects / display names: capitalization is the sender's choice
    assert associate.employer_mention("Your application to blue mercury", "Mercury", header=True) == "other"
    assert associate.employer_mention("Application to Mercury received", "Mercury", header=True) == "named"
    assert associate.employer_mention("Mercury values your time.", "Mercury") == "named"  # running text
    assert associate.employer_mention("Thanks from Acme", "Mercury") == "none"
    for text in ("Thank You For Applying To Mercury", "Welcome to Express!", "Join Mercury’s team",
                 "Hi Test,\nMercury will review it.", "Your Data Analyst application at Mercury",
                 "Update From Mercury", "Re: Mercury - next steps", "Mercury Careers"):
        assert associate.names_employer(text, "Mercury" if "Express" not in text else "Express"), text


def test_title_extraction_beyond_the_common_phrasings():
    titles = associate.mentioned_titles
    assert "Senior Data Engineer" in titles("Senior Data Engineer (Req 7781) - next steps", "")
    assert "Senior Data Engineer" in titles("Re: Req 7781 / Senior Data Engineer", "")
    assert "Senior Data Engineer" in titles("Update", "joining Acme Analytics as a Senior Data Engineer. Thanks")
    assert "Data Engineer" in titles("Update", "for the position of Data Engineer in our team.")
    assert titles("Offer letter - Business Analyst", "") == ["Business Analyst"]
    assert titles("Thank you for applying to Acme Analytics", "") == []

    def status(body, subject="x"):
        return associate.title_status("Business Analyst", "Acme Analytics", titles(subject, body), subject=subject,
                                      body=body)[0]

    assert status("Thank you for applying for the Business Analyst position.") == "match"
    assert status("Thank you for applying for the Business Analyst position. We are hiring for the Data Engineer "
                  "role instead.") == "mixed"
    assert status("Unfortunately we will not move forward.") == "none"
    assert status("", subject="Senior Business Analyst - update") == "mismatch"


def test_platform_tenant_helpers():
    assert associate.url_tenant("https://boards.greenhouse.io/acmeanalytics/jobs/101") == ("greenhouse",
                                                                                          "acmeanalytics")
    assert associate.url_tenant("https://boards.greenhouse.io/embed/job_app?for=acme&token=1") == ("greenhouse", "acme")
    assert associate.url_tenant("https://acme.wd5.myworkdayjobs.com/en-US/External/job/1") == ("workday", "acme")
    assert associate.url_tenant("https://careers-acme.icims.com/jobs/1234/job") == ("icims.com", "acme")
    assert associate.url_tenant("https://careers.acmeanalytics.com/jobs/1") is None
    assert associate.platform_family("us.greenhouse-mail.io") == associate.platform_family("boards.greenhouse.io")
    assert associate.sender_tenants("us.greenhouse-mail.io", "no-reply") == set()
    assert associate.sender_tenants("candidates.workablemail.com", "noreply") == set()
    assert associate.sender_tenants("myworkday.com", "acmecareers") == {"acme"}
    assert associate.sender_tenants("mercuryinsurance.icims.com", "no-reply") == {"mercuryinsurance"}
