"""Parsing, rules classification, LLM combination and suggested-reply safety."""

from __future__ import annotations

import pytest

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
