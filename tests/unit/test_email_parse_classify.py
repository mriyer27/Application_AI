"""Parsing, rules classification, LLM combination and suggested-reply safety."""

from __future__ import annotations

import pytest

from job_agent.email import classify as email_classify
from job_agent.email import parse as email_parse
from job_agent.email.classify import (
    RULES_CONFIDENT,
    classify_rules,
    combine,
    validate_llm_classification,
)
from job_agent.email.parse import (
    domain_core,
    extract_body,
    html_to_text,
    parse_from,
    parse_message,
    registrable_domain,
)
from job_agent.email.replies import REPLY_CATEGORIES, reply_is_safe, suggest_reply, template_reply
from job_agent.errors import PermanentError
from job_agent.models import EmailCategory, EmailClassification
from test_email_fakes import build_message, load_specs, spec


def _parsed(message_id: str, limit: int = 4000):
    return parse_message(build_message(spec(message_id)), body_char_limit=limit)


# ----------------------------------------------------------------- parse ---- #
def test_parse_headers_and_plain_body():
    p = _parsed("m-confirm-1")
    assert p.gmail_message_id == "m-confirm-1" and p.gmail_thread_id == "t-confirm-1"
    assert p.from_name == "Acme Analytics Hiring Team"
    assert p.from_address == "no-reply@us.greenhouse-mail.io"
    assert p.from_domain == "us.greenhouse-mail.io"
    assert p.received_at == "2026-10-05T14:00:00+00:00"
    assert p.date_header_at == "2026-10-05T14:00:00+00:00"
    assert p.subject == "Thank you for applying to Acme Analytics"
    assert "We have received your application" in p.body_text
    assert len(p.snippet) <= 200 and not p.bulk and not p.is_from_self


def test_date_header_with_offset_converted_to_utc_when_no_internal_date():
    msg = build_message(spec("m-confirm-1"))
    del msg["internalDate"]
    for h in msg["payload"]["headers"]:
        if h["name"] == "Date":
            h["value"] = "Mon, 05 Oct 2026 10:00:00 -0400"
    p = parse_message(msg, body_char_limit=4000)
    assert p.received_at == "2026-10-05T14:00:00+00:00"


def test_html_only_body_is_converted_and_attachments_skipped():
    s = spec("m-schedule-1")
    s["attachment"] = True
    p = parse_message(build_message(s), body_char_limit=4000)
    assert "select a time" in p.body_text
    assert "<b>" not in p.body_text and "color:red" not in p.body_text


def test_plain_text_preferred_over_html():
    s = spec("m-recruiter-1")
    s["html"] = "<p>HTML VERSION ONLY</p>"
    p = parse_message(build_message(s), body_char_limit=4000)
    assert "HTML VERSION ONLY" not in p.body_text and "technical recruiter" in p.body_text


def test_body_truncated_to_limit():
    s = spec("m-recruiter-1")
    s["text"] = "word " * 2000
    p = parse_message(build_message(s), body_char_limit=200)
    assert len(p.body_text) <= 200 + len("\n[...truncated...]")


def test_list_unsubscribe_marks_bulk():
    assert _parsed("m-newsletter-1").bulk is True


def test_from_and_domain_helpers():
    assert parse_from('"Lee, Jordan" <Jordan.Lee@Globex-Corp.com>') == ("Lee, Jordan", "jordan.lee@globex-corp.com")
    assert parse_from("not an address") == ("", None) or parse_from("not an address")[1] is None
    assert registrable_domain("mail.jobs.acme.co.uk") == "acme.co.uk"
    assert registrable_domain("us.greenhouse-mail.io") == "greenhouse-mail.io"
    assert domain_core("talent.acme-analytics.com") == "acmeanalytics"


def test_html_to_text_and_extract_body_empty():
    assert html_to_text("<div>a<br>b</div><script>evil()</script>") == "a\nb"
    assert extract_body(None, 100) == ""


# ------------------------------------------------- sender authentication ---- #
def _auth(*values, from_domain="acmeanalytics.com"):
    payload = {"headers": [{"name": "Authentication-Results", "value": v} for v in values]}
    return email_parse.sender_auth(payload, from_domain)


GMAIL_PASS = ("mx.google.com; dkim=pass header.i=@acmeanalytics.com header.s=s1 header.b=Ab12; spf=pass "
              "(google.com: domain of hr@acmeanalytics.com designates 192.0.2.1 as permitted sender) "
              "smtp.mailfrom=hr@acmeanalytics.com; dmarc=pass (p=REJECT sp=REJECT dis=NONE) "
              "header.from=acmeanalytics.com")


def test_sender_auth_dmarc_pass_from_gmail():
    result = _auth(GMAIL_PASS)
    assert result.authenticated and result.method == "dmarc" and result.label == "verified (dmarc)"


def test_sender_auth_aligned_dkim_without_dmarc_policy():
    result = _auth("mx.google.com; dkim=pass header.i=@mail.acmeanalytics.com; spf=neutral; dmarc=none")
    assert result.authenticated and result.method == "dkim"
    assert _auth("mx.google.com; dkim=pass header.d=acmeanalytics.com", from_domain="talent.acmeanalytics.com"
                 ).authenticated


@pytest.mark.parametrize("value,reason", [
    ("mx.google.com; spf=fail smtp.mailfrom=evil.example; dkim=none; dmarc=fail", "dmarc=fail"),
    ("mx.google.com; spf=pass smtp.mailfrom=acmeanalytics.com; dkim=none", "no DKIM/DMARC pass"),
    ("mx.google.com; dkim=pass header.i=@evil.example; dmarc=none", "no DKIM/DMARC pass"),
    ("mx.google.com; dkim=pass header.i=@acmeanalytics.com; dmarc=fail header.from=acmeanalytics.com", "dmarc=fail"),
    ("mx.evil.example; dkim=pass header.i=@acmeanalytics.com; dmarc=pass header.from=acmeanalytics.com",
     "not written by Gmail"),
    ("mx.google.com (comment; dmarc=pass header.from=acmeanalytics.com); dkim=neutral", "no DKIM/DMARC pass"),
])
def test_sender_auth_failures(value, reason):
    result = _auth(value)
    assert not result.authenticated and reason in result.reason and result.label.startswith("not verified")


def test_sender_auth_uses_only_the_topmost_gmail_header():
    # Gmail prepends its own verdict; a forged "pass" below it (sent with the message) is ignored.
    assert not _auth("mx.google.com; dkim=none; dmarc=fail header.from=acmeanalytics.com", GMAIL_PASS).authenticated
    # A forged header on top that is not Gmail's also means "not verified" (no fallback further down).
    assert not _auth("mx.evil.example; dmarc=pass header.from=acmeanalytics.com", GMAIL_PASS).authenticated


def test_sender_auth_ignores_arc_results_and_missing_headers():
    payload = {"headers": [{"name": "ARC-Authentication-Results",
                            "value": "i=1; mx.google.com; dmarc=pass header.from=acmeanalytics.com"}]}
    assert not email_parse.sender_auth(payload, "acmeanalytics.com").authenticated
    assert not email_parse.sender_auth({"headers": []}, "acmeanalytics.com").authenticated
    assert not _auth(GMAIL_PASS, from_domain=None).authenticated


def test_parse_message_carries_sender_auth():
    p = _parsed("m-offer-1")
    assert p.auth.authenticated and p.auth.dmarc == "pass"
    s = spec("m-offer-1")
    s["headers"] = {}
    assert not parse_message(build_message(s), body_char_limit=4000).auth.authenticated


# -------------------------------------------------------------- classify ---- #
@pytest.mark.parametrize("message_spec", load_specs(), ids=lambda s: s["id"])
def test_rules_classify_each_fixture(message_spec):
    p = parse_message(build_message(message_spec), body_char_limit=4000)
    result = classify_rules(subject=p.subject, body=p.body_text, from_address=p.from_address,
                            from_domain=p.from_domain, from_name=p.from_name, bulk=p.bulk)
    assert result.category == EmailCategory(message_spec["expected"]), result
    assert result.classifier == "rules"
    assert 0 <= result.confidence <= 1
    assert result.confidence >= RULES_CONFIDENT, result
    assert len(result.summary) <= 300


def test_newsletter_is_other_even_with_job_words():
    p = _parsed("m-newsletter-1")
    result = classify_rules(subject=p.subject, body=p.body_text, from_address=p.from_address,
                            from_domain=p.from_domain, bulk=p.bulk)
    assert result.category == EmailCategory.OTHER and result.requires_attention is False


def test_rejection_beats_thank_you_for_applying():
    result = classify_rules(subject="Your application to Acme",
                            body="Thank you for applying. Unfortunately we will not be moving forward.",
                            from_address="careers@acme.com", from_domain="acme.com")
    assert result.category == EmailCategory.REJECTION


def test_unrelated_mail_is_other_with_low_confidence():
    result = classify_rules(subject="Dinner on Friday?", body="Are we still on for dinner?",
                            from_address="friend@gmail.com", from_domain="gmail.com")
    assert result.category == EmailCategory.OTHER and result.confidence < RULES_CONFIDENT


def test_actionable_categories_require_attention():
    p = _parsed("m-interview-1")
    result = classify_rules(subject=p.subject, body=p.body_text, from_address=p.from_address,
                            from_domain=p.from_domain)
    assert result.requires_attention is True
    p = _parsed("m-reject-1")
    result = classify_rules(subject=p.subject, body=p.body_text, from_address=p.from_address,
                            from_domain=p.from_domain)
    assert result.requires_attention is False


def test_combine_confident_rules_win_over_llm():
    rules = EmailClassification(category=EmailCategory.REJECTION, confidence=0.9, classifier="rules")
    llm = EmailClassification(category=EmailCategory.OFFER, confidence=0.99, classifier="llm:test")
    assert combine(rules, llm) is rules


def test_combine_uses_llm_when_rules_uncertain():
    rules = EmailClassification(category=EmailCategory.OTHER, confidence=0.55, classifier="rules")
    llm = EmailClassification(category=EmailCategory.RECRUITER_MESSAGE, confidence=0.7, classifier="llm:test")
    assert combine(rules, llm) is llm
    assert combine(rules, None) is rules


def test_validate_llm_classification_bounds_and_rejects_bad_enum():
    ok = validate_llm_classification({"category": "assessment", "confidence": 7, "summary": "x" * 900,
                                      "employer_guess": "Acme" * 100, "classifier": "llm:qwen"})
    assert ok.confidence == 1.0 and len(ok.summary) <= 300 and len(ok.employer_guess or "") <= 120
    assert ok.requires_attention is True and ok.classifier == "llm:qwen"
    assert validate_llm_classification({"category": "other", "confidence": -1}).classifier == "llm:unknown"
    with pytest.raises(PermanentError):
        validate_llm_classification({"category": "accept_offer", "confidence": 0.9})
    with pytest.raises(PermanentError):
        validate_llm_classification("not an object")


# --------------------------------------------------------------- replies ---- #
@pytest.mark.parametrize("category", sorted(REPLY_CATEGORIES, key=lambda c: c.value))
def test_reply_templates_are_safe_and_use_placeholders(category):
    text = template_reply(category, candidate_name="Test Candidate")
    assert text and reply_is_safe(text)
    assert "[" in text and "Test Candidate" in text
    lowered = text.lower()
    for forbidden in ("i accept", "works for me", "salary", "see you"):
        assert forbidden not in lowered


def test_scheduling_reply_uses_availability_placeholder():
    text = template_reply(EmailCategory.SCHEDULING_REQUEST, candidate_name=None)
    assert "[your availability]" in text and "[your name]" in text


def test_no_reply_for_non_reply_categories():
    for category in (EmailCategory.OFFER, EmailCategory.REJECTION, EmailCategory.OTHER,
                     EmailCategory.APPLICATION_CONFIRMATION):
        assert suggest_reply(category, candidate_name="X") == (None, "none")


def test_unsafe_llm_reply_falls_back_to_template():
    def bad_llm(**kwargs):
        return "Yes, Tuesday at 3pm works for me, see you then! My salary expectation is $95,000."

    text, source = suggest_reply(EmailCategory.INTERVIEW_INVITATION, candidate_name="T", llm_draft=bad_llm)
    assert source == "template" and "3pm" not in text and reply_is_safe(text)


def test_safe_llm_reply_is_used():
    def good_llm(**kwargs):
        assert kwargs["category"] == "recruiter_message"
        return "Hi [name],\n\nThank you for reaching out. I am available [your availability].\n\nBest,\nT"

    text, source = suggest_reply(EmailCategory.RECRUITER_MESSAGE, candidate_name="T", llm_draft=good_llm)
    assert source == "llm" and "[your availability]" in text


# ------------------------------------------------- offers, negations, marketing ---- #
def _classify(subject, body, domain="acme.com"):
    return classify_rules(subject=subject, body=body, from_address=f"x@{domain}", from_domain=domain)


def test_marketing_your_offer_is_not_an_employment_offer():
    result = _classify("Test, your offer is ready",
                       "Good news: your offer is waiting. You're pre-approved for a new card with no annual fee. "
                       "To stop receiving these messages, unsubscribe here.", domain="notification.capitalone.com")
    assert result.category != EmailCategory.OFFER
    assert _classify("Special for you", "We're excited to present you an offer: 20% off.").category \
        != EmailCategory.OFFER


@pytest.mark.parametrize("subject,body", [
    ("Update on your offer", "Unfortunately, we must rescind your offer for the Business Analyst position."),
    ("Your application", "We are unable to extend an offer of employment at this time."),
    ("Your application", "We are not able to offer you the Business Analyst position."),
    ("Business Analyst", "The offer of employment has been withdrawn for the Business Analyst role."),
])
def test_negated_offers_are_rejections(subject, body):
    result = _classify(subject, body)
    assert result.category == EmailCategory.REJECTION, result


@pytest.mark.parametrize("clause", [
    "We reserve the right to rescind this offer if the background check is not completed.",
    "If you decide not to move forward with the offer, please let us know.",
])
def test_conditional_rescind_clause_keeps_a_genuine_offer(clause):
    result = _classify("Offer letter - Business Analyst",
                       "We are pleased to extend you an offer for the Business Analyst position. " + clause)
    assert result.category == EmailCategory.OFFER and result.confidence >= RULES_CONFIDENT


def test_contradictory_offer_and_rejection_is_never_confident():
    result = _classify("Your applications",
                       "We are pleased to extend you an offer for the Data Analyst position. Unfortunately we are "
                       "unable to extend an offer for the Business Analyst role.")
    assert result.confidence < RULES_CONFIDENT


def test_is_promotional():
    assert email_classify.is_promotional(subject="Hi", body="Manage your email preferences or unsubscribe.")
    assert email_classify.is_promotional(subject="Get 20% off", body="")
    assert not email_classify.is_promotional(subject="Offer letter - Business Analyst",
                                             body="We are pleased to extend you an offer for the position.")


# ------------------------------------------- RFC 8601 Authentication-Results ---- #
_INJECTED_MAILFROM = '"x;dmarc=pass header.from=acmeanalytics.com;dkim=pass header.i=@acmeanalytics.com"@evil.io'


@pytest.mark.parametrize("value,reason", [
    # E5: results written inside a quoted smtp.mailfrom local part; Gmail's real verdict is dmarc=fail
    (f"mx.google.com; dkim=pass header.i=@evil.io header.s=s1 header.b=AbC; spf=pass (google.com: domain of "
     f"{_INJECTED_MAILFROM} designates 192.0.2.7 as permitted sender) smtp.mailfrom={_INJECTED_MAILFROM}; "
     "dmarc=fail (p=NONE sp=NONE dis=NONE) header.from=acmeanalytics.com", "dmarc=fail"),
    # ... and without any dmarc result: the quoted text still adds nothing
    (f"mx.google.com; dkim=pass header.i=@evil.io; spf=pass smtp.mailfrom={_INJECTED_MAILFROM}",
     "no DKIM/DMARC pass"),
    # a quoted reason / comment holding results
    ('mx.google.com; dkim=neutral reason="x; dkim=pass header.i=@acmeanalytics.com"', "no DKIM/DMARC pass"),
    # RFC 5322: a quote inside a comment is ordinary text, so ')' ends the comment and a quote is left open
    ("mx.google.com; dkim=neutral (bad; \"quoted ) paren\"; dkim=pass header.i=@acmeanalytics.com)",
     "could not be parsed"),
    # unbalanced quoting / comments fail closed
    ('mx.google.com; spf=pass smtp.mailfrom="x; dmarc=pass header.from=acmeanalytics.com', "could not be parsed"),
    ("mx.google.com; dkim=pass (unterminated; dmarc=pass header.from=acmeanalytics.com", "could not be parsed"),
    # two DMARC verdicts, or one for another domain
    ("mx.google.com; dmarc=pass header.from=acmeanalytics.com; dmarc=fail header.from=acmeanalytics.com",
     "several dmarc results"),
    ("mx.google.com; dkim=pass header.i=@acmeanalytics.com; dmarc=pass header.from=evil.io",
     "different From domain"),
    # a property value that is not a domain
    ('mx.google.com; dkim=pass header.i="@acmeanalytics.com;evil"; dmarc=none', "no DKIM/DMARC pass"),
])
def test_sender_auth_rfc8601_lexing_fails_closed(value, reason):
    result = _auth(value)
    assert not result.authenticated and reason in result.reason, result


def test_sender_auth_rfc8601_legitimate_forms_still_verify():
    assert _auth(GMAIL_PASS).authenticated
    # quoted property values, versioned methods, nested comments and extra results are all fine
    assert _auth('mx.google.com 1; dkim/1=pass (2048-bit key; unprotected (nested)) header.i="@acmeanalytics.com" '
                 'header.s=s1; spf=pass smtp.mailfrom="hr"@acmeanalytics.com; dmarc=pass (p=REJECT) '
                 'header.from="acmeanalytics.com"; arc=none').authenticated
    # several DKIM signatures (employer + mail provider) are normal
    assert _auth("mx.google.com; dkim=pass header.i=@acmeanalytics.com; dkim=pass header.i=@sendgrid.info; "
                 "spf=pass smtp.mailfrom=bounces@sendgrid.net; dmarc=pass header.from=acmeanalytics.com").authenticated


def test_several_from_headers_are_never_verified():
    s = spec("m-offer-1")
    msg = build_message(s)
    msg["payload"]["headers"].insert(1, {"name": "From", "value": "Evil <hr@evil.example>"})
    p = parse_message(msg, body_char_limit=4000)
    assert not p.auth.authenticated and "several From headers" in p.auth.reason


# --------------------------------------------- offers to someone else (E3) ---- #
@pytest.mark.parametrize("body", [
    "We were pleased to extend an offer to another candidate, who has accepted it. We wish you the best.",
    "After careful consideration, we were happy to make an offer to another candidate for the Business Analyst "
    "position.",
    "We were thrilled to extend an offer to a candidate whose experience more closely matched the role.",
    "Thank you for your time. We are pleased to extend this offer to the candidate we selected for the position.",
    "We have extended an offer to Jordan for the Business Analyst position.",
    "We are pleased to extend an offer to anоther candidate for the position.",  # Cyrillic о
])
def test_offer_to_another_candidate_is_never_a_confident_offer(body):
    result = _classify("Business Analyst position update", body)
    assert result.category != EmailCategory.OFFER, result
    assert result.category == EmailCategory.REJECTION
    assert result.confidence < RULES_CONFIDENT  # a misread offer must never be filed away by itself


@pytest.mark.parametrize("body", [
    "We are delighted to offer you the position of applicant in our talent community for future roles.",
    "We are pleased to extend an offer for the Business Analyst position.",  # not addressed to you
])
def test_offer_not_clearly_made_to_you_needs_review(body):
    result = _classify("Business Analyst position update", body)
    assert result.confidence < RULES_CONFIDENT, result


@pytest.mark.parametrize("subject,body", [
    ("Offer letter - Business Analyst", "We are pleased to extend you an offer for the Business Analyst position."),
    ("Your offer", "We are pleased to extend an offer of employment to you for the Business Analyst position."),
    ("Offer", "We are delighted to offer you the Business Analyst position, with a base salary of [x]."),
    ("Offer", "We are pleased to offer you employment as a Business Analyst. Your start date is Monday."),
    ("Business Analyst", "We are pleased to extend an offer for the position. Your offer letter is attached."),
])
def test_offers_made_to_you_stay_confident(subject, body):
    result = _classify(subject, body)
    assert result.category == EmailCategory.OFFER and result.confidence >= RULES_CONFIDENT, result


def test_explicit_rejections_with_offer_words_stay_confident():
    for body in ("Unfortunately, the position has been filled.",
                 "Unfortunately we have moved forward with another candidate for the Business Analyst role."):
        result = _classify("Business Analyst position update", body)
        assert result.category == EmailCategory.REJECTION and result.confidence >= RULES_CONFIDENT, result
    welcome = _classify("Welcome aboard", "We're thrilled that you have accepted our offer for the Business Analyst "
                                          "position! Your start date is Monday.")
    assert welcome.category != EmailCategory.REJECTION
