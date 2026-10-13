"""Suggested replies and follow-up drafts never carry work-authorization statements or unverified claims."""

from __future__ import annotations

import sys
import types

import pytest

from job_agent.email import service as email_service
from job_agent.email.replies import REPLY_CATEGORIES, reply_is_safe, reply_problems, suggest_reply, template_reply
from job_agent.email.service import sync_gmail
from job_agent.followups.drafts import llm_followup_draft
from job_agent.followups.service import review_followups
from job_agent.models import EmailCategory
from test_email_fakes import enable_gmail, fake_service
from test_followups import NOW, _rows, _submitted_app

PROBE = ("Hi [recruiter name],\n\nThank you for reaching out. Yes, I am authorized to work in the United States for "
         "any employer and am eligible to work without restriction. I have three years of Snowflake and dbt "
         "experience and led a team of five analysts. I am generally available [your availability].\n\n"
         "Best regards,\nTest Candidate")
GOOD = ("Hi [recruiter name],\n\nThank you for reaching out. I would be glad to learn more about the role and the "
        "next steps. I am generally available [your availability].\n\nBest regards,\nTest Candidate")
FACT_CLAIM = ("Hi [name],\n\nThank you for reaching out. I converted nine legacy SAS programs to Python. I am "
              "available [your availability].\n\nBest regards,\nTest Candidate")


def _llm(text):
    return lambda **kwargs: text


def test_probe_reply_falls_back_to_template():
    text, source = suggest_reply(EmailCategory.RECRUITER_MESSAGE, candidate_name="Test Candidate",
                                 subject="Data Analyst role", body="Are you authorized to work in the US?",
                                 llm_draft=_llm(PROBE))
    assert source == "template"
    assert text == template_reply(EmailCategory.RECRUITER_MESSAGE, candidate_name="Test Candidate")
    assert "authorized" not in text and "Snowflake" not in text


@pytest.mark.parametrize("statement", [
    "I am eligible to work for any employer.",
    "I will not need any employer support.",
    "I am authorized to work in the United States.",
    "I can start without sponsorship.",
    "I can legally work here.",
])
def test_work_authorization_statements_fall_back_to_template(statement):
    draft = f"Hi [name],\n\nThank you for reaching out. {statement}\n\nBest regards,\nT"
    assert not reply_is_safe(draft)
    _, source = suggest_reply(EmailCategory.RECRUITER_MESSAGE, candidate_name="T", llm_draft=_llm(draft))
    assert source == "template"


@pytest.mark.parametrize("claim", [
    "I have three years of Snowflake and dbt experience.",
    "I led a team of analysts.",
    "I have 4 years of experience.",
    "I am proficient in Snowflake.",
])
def test_unverified_experience_claims_fall_back_to_template(claim):
    draft = f"Hi [name],\n\nThank you for reaching out. {claim} I am available [your availability].\n\nBest,\nT"
    problems = reply_problems(draft)
    assert problems, claim
    _, source = suggest_reply(EmailCategory.RECRUITER_MESSAGE, candidate_name="T", llm_draft=_llm(draft))
    assert source == "template"


def test_genuine_safe_reply_is_kept():
    text, source = suggest_reply(EmailCategory.RECRUITER_MESSAGE, candidate_name="Test Candidate",
                                 llm_draft=_llm(GOOD))
    assert source == "llm" and text == GOOD


def test_fact_backed_claim_needs_the_facts(profile):
    _, unknown = suggest_reply(EmailCategory.RECRUITER_MESSAGE, candidate_name="T", llm_draft=_llm(FACT_CLAIM))
    assert unknown == "template"
    text, known = suggest_reply(EmailCategory.RECRUITER_MESSAGE, candidate_name="T", llm_draft=_llm(FACT_CLAIM),
                                facts=profile.all_facts(), profile_skills=profile.skills)
    assert known == "llm" and text == FACT_CLAIM
    inflated = FACT_CLAIM.replace("nine", "fifteen")  # 15 is in no verified fact
    _, source = suggest_reply(EmailCategory.RECRUITER_MESSAGE, candidate_name="T", llm_draft=_llm(inflated),
                              facts=profile.all_facts(), profile_skills=profile.skills)
    assert source == "template"


@pytest.mark.parametrize("category", sorted(REPLY_CATEGORIES, key=lambda c: c.value))
def test_templates_pass_the_new_checks(category):
    assert reply_problems(template_reply(category, candidate_name="Test Candidate")) == []


# ------------------------------------------------------------ follow-ups ---- #
CTX = {"employer": "Acme", "title": "Data Analyst", "submitted_at": "2026-09-30T10:00:00+00:00", "days": 12,
       "candidate_name": "Test Candidate"}


def test_followup_probe_is_rejected():
    assert llm_followup_draft(_llm(PROBE), dict(CTX)) is None
    claims_only = PROBE.replace("Yes, I am authorized to work in the United States for any employer and am eligible "
                                "to work without restriction. ", "")
    assert llm_followup_draft(_llm(claims_only), dict(CTX)) is None


def test_followup_with_application_dates_is_kept():
    text = ("Dear Hiring Team,\n\nI applied for the Data Analyst position at Acme on 2026-09-30 and wanted to "
            "follow up on my application. I remain interested in the role.\n\nBest regards,\nTest Candidate")
    assert llm_followup_draft(_llm(text), dict(CTX)) == text


def test_followup_fact_claims_checked_against_context_facts(profile):
    text = ("Dear Hiring Team,\n\nI wanted to follow up on my application for the Data Analyst position. "
            "I converted nine legacy SAS programs to Python.\n\nBest regards,\nTest Candidate")
    assert llm_followup_draft(_llm(text), dict(CTX)) is None
    known = {**CTX, "facts": profile.all_facts(), "profile_skills": profile.skills}
    assert llm_followup_draft(_llm(text), known) == text


def test_review_followups_uses_template_for_probe_draft(ctx, make_job, monkeypatch):
    ctx.config.llm.enabled = True
    client_mod = types.ModuleType("job_agent.llm.client")
    client_mod.get_client = lambda c: object()
    gen_mod = types.ModuleType("job_agent.llm.generation")
    gen_mod.draft_followup = lambda client, **kw: PROBE
    monkeypatch.setitem(sys.modules, "job_agent.llm.client", client_mod)
    monkeypatch.setitem(sys.modules, "job_agent.llm.generation", gen_mod)
    _submitted_app(ctx.db, make_job, days_ago=10)
    review_followups(ctx, now=NOW)
    draft = _rows(ctx.db)[0]["draft_text"]
    assert "authorized" not in draft and "Snowflake" not in draft
    assert "Following up on my application" in draft  # template fallback


def test_gmail_sync_never_stores_probe_reply(ctx, monkeypatch):
    enable_gmail(ctx)
    monkeypatch.setattr(email_service._Processor, "llm_reply", lambda self: _llm(PROBE))
    sync_gmail(ctx, service=fake_service())
    replies = [r["suggested_reply"] for r in ctx.db.query_all(
        "SELECT suggested_reply FROM email_messages WHERE suggested_reply IS NOT NULL")]
    assert replies, "fixture mailbox should contain at least one message that gets a suggested reply"
    for reply in replies:
        assert "authorized" not in reply and "Snowflake" not in reply and "five analysts" not in reply


# --------------------------------------------------------------------------- #
# Round 2: misspelled / non-English statements, clearance claims, numerals
# --------------------------------------------------------------------------- #
from types import SimpleNamespace  # noqa: E402

from job_agent.errors import JobAgentError  # noqa: E402
from job_agent.llm.generation import draft_email_reply  # noqa: E402

ROUND2_BODIES = [
    "Yes, I am a U.S. citzen and need no sponser.",
    "Tengo permiso para trabajar en Estados Unidos, sin restricciones.",
    "Ich brauche kein Visum.",
    "I will never need the company to file anything on my behalf to keep working.",
    "I'm cleared at the TS level.",
    "I bring ⑳ years in this field.",
    "Je n'ai pas besoin de visa.",
    "I hold an active TS.",
]


def _round2_reply(body: str) -> str:
    return (f"Hi [recruiter name],\n\nThank you for reaching out. {body} I am generally available "
            "[your availability].\n\nBest regards,\nTest Candidate")


class _StubClient:
    """Stands in for OllamaClient: returns ``text`` for whatever schema is asked."""

    config = SimpleNamespace(max_input_chars=12000)

    def __init__(self, text: str) -> None:
        self.text = text

    def generate_json(self, *, system, prompt, schema, model=None, max_tokens=None):
        return schema(**{"reply" if schema.__name__ == "ReplyOut" else "message": self.text})


def _service_drafter(text: str):
    """Like email.service: the real draft_email_reply, an error means no LLM draft."""
    def draft(**kwargs):
        try:
            return draft_email_reply(_StubClient(text), **kwargs)
        except JobAgentError:
            return ""
    return draft


@pytest.mark.parametrize("body", ROUND2_BODIES)
def test_round2_llm_replies_fall_back_to_template(body, profile):
    text = _round2_reply(body)
    expected = template_reply(EmailCategory.RECRUITER_MESSAGE, candidate_name="Test Candidate")
    for drafter in (_llm(text), _service_drafter(text)):  # unchecked raw text, and the real drafter
        for kwargs in ({}, {"facts": profile.all_facts(), "profile_skills": profile.skills}):
            draft, source = suggest_reply(EmailCategory.RECRUITER_MESSAGE, candidate_name="Test Candidate",
                                          subject="Data Analyst role", body="Are you authorized to work in the US?",
                                          llm_draft=drafter, **kwargs)
            assert (draft, source) == (expected, "template"), body
    assert reply_problems(text)


def test_round2_followup_with_german_no_visa_and_circled_numeral_is_rejected():
    text = ("Hi [name], following up on my application. Ich brauche kein Visum und ich bringe ⑳ Jahre Erfahrung "
            "mit. Best, Test Candidate")
    assert llm_followup_draft(_llm(text), dict(CTX)) is None
    for body in ROUND2_BODIES:
        assert llm_followup_draft(_llm(f"Dear Hiring Team,\n\n{body}\n\nBest regards,\nTest Candidate"),
                                  dict(CTX)) is None, body


def test_accepted_drafts_are_stored_exactly_as_validated():
    wide = GOOD.replace("the role", "the ｒｏｌｅ")  # full-width letters render as ASCII
    text, source = suggest_reply(EmailCategory.RECRUITER_MESSAGE, candidate_name="Test Candidate",
                                 llm_draft=_llm(wide))
    assert source == "llm" and text == GOOD
    followup = ("Dear Hiring Team,\n\nI applied for the Data Analyst position at Ａｃｍｅ on 2026-09-30 and wanted "
                "to follow up.\n\nBest regards,\nTest Candidate")
    assert llm_followup_draft(_llm(followup), dict(CTX)) == followup.replace("Ａｃｍｅ", "Acme")


def test_candidate_name_in_another_script_does_not_block_a_safe_reply():
    text = GOOD.replace("Test Candidate", "王伟")
    assert suggest_reply(EmailCategory.RECRUITER_MESSAGE, candidate_name="王伟", llm_draft=_llm(text)) == (text, "llm")
    other = GOOD.replace("Test Candidate", "Мне нужна виза")  # unreadable text that is not the candidate's name
    assert suggest_reply(EmailCategory.RECRUITER_MESSAGE, candidate_name="王伟", llm_draft=_llm(other))[1] == "template"
