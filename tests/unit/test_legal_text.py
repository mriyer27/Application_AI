"""security.legal_text: legal-question detection, work-authorization statements, unverified draft claims."""

from __future__ import annotations

import pytest

from job_agent.security.legal_text import (
    claim_sentences,
    draft_claim_problems,
    is_legal_question,
    is_sensitive_question,
    mentions_legal_matter,
    mentions_work_authorization,
    numbers_mentioned,
    tools_mentioned,
)

#: Labels from the field-mapping review that were missed before (each must be a legal question).
REVIEW_LEGAL_LABELS = [
    "Will you now or in the future require us to support an H-1B?",
    "Are you legally able to work in the U.S.?",
    "Do you need a visa?",
    "Are you a U.S. person under ITAR?",
    "Are you able to legally work in the United States?",
    "Are you legally allowed to work in the United States?",
    "Are you lawfully permitted to work in the US?",
    "Can you work in the United States without restrictions?",
    "Do you currently have unrestricted work rights in the U.S.?",
    "Will you now or in the future need an employer to file an employment-based petition on your behalf?",
    "Are you currently on OPT?",
    "Are you an F-1 student?",
    "Do you hold an EAD card?",
    "Do you hold an active Secret or TS/SCI?",
    "Are you a U.S. national?",
    "Can you provide proof of identity and employment eligibility (Form I-9)?",
    "Are you a protected individual as defined in 8 U.S.C. 1324b(a)(3)?",
    "Will you require a change of status or transfer to begin employment?",
]

MORE_LEGAL_LABELS = [
    "Sponsorship",
    "Are you legally authorized to work in the United States?",
    "Are you eligible to work in the US?",
    "Do you have the right to work in the UK?",
    "Will you now or in the future require visa sponsorship?",
    "Do you require sponsorship now or in the future?",
    "What is your current immigration status?",
    "What is your visa status?",
    "Are you currently on STEM OPT?",
    "Are you authorized to work via CPT?",
    "Are you on a J-1 visa?",
    "Are you a TN visa holder?",
    "Do you hold an O-1?",
    "Do you have a green card?",
    "Are you a lawful permanent resident?",
    "Are you a U.S. citizen?",
    "Country of citizenship",
    "Nationality",
    "Do you have a work permit?",
    "Do you hold an active security clearance?",
    "Do you hold a public trust clearance?",
    "Are you willing to take a polygraph?",
    "Are you a U.S. person under export control regulations (ITAR/EAR)?",
    "Would this role require a deemed export license for you?",
    "Do you have any criminal history?",
    "Have you ever been convicted of a felony?",
    "Do you consent to a background check?",
    "Are you subject to a non-compete agreement?",
    "Are you bound by a non-solicitation agreement?",
    "Do you have any relatives employed by this company?",
    "Are you willing to relocate, and will you require visa sponsorship now or in the future?",
]


@pytest.mark.parametrize("label", REVIEW_LEGAL_LABELS + MORE_LEGAL_LABELS)
def test_legal_questions_detected(label):
    assert is_legal_question(label), label


@pytest.mark.parametrize("label", [
    "spon​sorship required?",              # zero-width space inside a word
    "Are you authorized​to work here?",     # zero-width space between words
    "Do you require spоnsorship?",               # Cyrillic "о"
    "Do you require ｓｐｏｎｓｏｒｓｈｉｐ?",       # full-width letters
    "您是否需要签证担保?",                          # a script these patterns cannot read
])
def test_obfuscated_or_unreadable_labels_count_as_legal(label):
    assert is_legal_question(label)


@pytest.mark.parametrize("label", [
    "First name", "Last name", "Legal name", "Legal first name", "Full legal name", "Email", "Phone",
    "Location (City)", "City", "LinkedIn Profile", "Portfolio / website", "Resume/CV", "Cover Letter", "School",
    "Degree", "Discipline", "Expected graduation date", "GPA", "Gender", "Race / Ethnicity", "Veteran status",
    "Disability Status", "Pronouns", "I certify that the information provided is true.",
    "I agree to the privacy policy", "Salary expectations", "When can you start?", "Are you willing to relocate?",
    "What is your notice period?", "How did you hear about us?", "Why do you want to work here?",
    "Additional information", "Current company", "Emergency contact name", "Address line 1",
    "Are you willing to work weekends?", "Opt in to SMS updates", "Résumé", "", None,
])
def test_ordinary_labels_are_not_legal(label):
    assert not is_legal_question(label)


def test_sensitive_questions():
    for label in ("Salary expectations", "Desired annual base", "Gender", "Date of birth", "Are you a veteran?"):
        assert is_sensitive_question(label), label
    assert not is_sensitive_question("How did you hear about us?")


@pytest.mark.parametrize("text", [
    "I am eligible to work for any employer.",
    "I will not need any employer support.",
    "I am authorized to work in the United States.",
    "I can start without sponsorship.",
    "I am a U.S. citizen.",
    "I don't need a visa.",
    "I am on OPT until 2027.",
    "I can legally work here.",
])
def test_work_authorization_statements(text):
    assert mentions_work_authorization(text)
    assert mentions_legal_matter(text)


@pytest.mark.parametrize("text", [
    "Thank you for reaching out. I would be glad to learn more about the role.",
    "I am generally available [your availability] if a short call would be helpful.",
    "Could you share more details about the team and the next steps?",
])
def test_neutral_sentences_are_not_legal_statements(text):
    assert not mentions_legal_matter(text)


def test_numbers_mentioned_handles_words_digits_and_percentages():
    assert numbers_mentioned("three years, twenty-five people, 1,200 rows, 30% and a 3.70 GPA") == {
        "3", "25", "1200", "30", "3.7"}
    assert numbers_mentioned("one year") == {"1"}
    assert numbers_mentioned("one of the teams, someone else") == set()


def test_tools_mentioned():
    assert tools_mentioned("I have three years of Snowflake and dbt experience") == ["dbt", "snowflake"]
    assert tools_mentioned("I used SAS Enterprise Guide") == ["sas enterprise guide"]
    assert "zendesk" in tools_mentioned("I have experience with Zendesk.")
    assert tools_mentioned("Thank you for your time.") == []


PROBE = ("Hi [recruiter name],\n\nThank you for reaching out. Yes, I am authorized to work in the United States for "
         "any employer and am eligible to work without restriction. I have three years of Snowflake and dbt "
         "experience and led a team of five analysts. I am generally available [your availability].\n\n"
         "Best regards,\nTest Candidate")
FACTS = ["Converted 9 legacy SAS programs to Python with AI-assisted development, improving processing "
         "efficiency by approximately 25%.",
         "Supported IBM Cognos dashboards and reporting using DB2."]
SKILLS = ["Python", "SQL", "SAS Enterprise Guide", "IBM Cognos", "DB2"]


def test_probe_reply_is_rejected_for_every_reason():
    problems = " | ".join(draft_claim_problems(PROBE))
    assert "work-authorization" in problems
    assert "numbers" in problems and "tools/skills" in problems and "leadership" in problems


def test_claims_without_legal_wording_are_rejected_when_facts_are_unknown():
    text = "I have three years of Snowflake and dbt experience and led a team of five analysts."
    problems = " | ".join(draft_claim_problems(text))
    assert "work-authorization" not in problems
    assert "numbers" in problems and "tools/skills" in problems and "leadership" in problems
    assert draft_claim_problems("Led a team of analysts.")  # verb-first fragment is still a claim


def test_claims_checked_against_known_facts():
    bad = draft_claim_problems("I have three years of Snowflake and dbt experience and led a team of five analysts.",
                               facts=FACTS, skills=SKILLS)
    text = " | ".join(bad)
    assert "quantities" in text and "dbt" in text and "snowflake" in text and "leadership" in text
    # verbatim and faithfully reworded facts are fine
    assert draft_claim_problems("I converted 9 legacy SAS programs to Python.", facts=FACTS, skills=SKILLS) == []
    assert draft_claim_problems("I converted nine legacy SAS programs to Python, improving efficiency by about 25%.",
                                facts=FACTS, skills=SKILLS) == []
    assert draft_claim_problems("I supported IBM Cognos dashboards using DB2.", facts=FACTS, skills=SKILLS) == []
    assert draft_claim_problems("I converted 12 SAS programs.", facts=FACTS, skills=SKILLS)  # changed number
    assert draft_claim_problems("I have experience with Zendesk.", facts=FACTS, skills=SKILLS)
    assert draft_claim_problems("I managed the Cognos dashboards.", facts=FACTS, skills=SKILLS)  # no such fact


def test_leadership_supported_by_a_fact_is_allowed():
    facts = ["Led a team of 3 student volunteers to organize a campus analytics workshop."]
    assert draft_claim_problems("I led a team of 3 student volunteers.", facts=facts, skills=[]) == []
    assert draft_claim_problems("I led a team of 5 student volunteers.", facts=facts, skills=[])


def test_neutral_reply_has_no_problems():
    text = ("Hi [name],\n\nThank you for reaching out. I would be glad to learn more about the role. "
            "I am generally available [your availability].\n\nBest regards,\nTest Candidate")
    assert draft_claim_problems(text) == []
    assert claim_sentences(text) == []


def test_context_numbers_allowed_outside_claims_only():
    context = ("Applied on 2026-09-30; no response for 12 days.", "Acme", "Engineering Manager")
    text = ("I applied for the Engineering Manager position at Acme on 2026-09-30 and wanted to follow up. "
            "I remain interested in the role.")
    assert draft_claim_problems(text, context=context) == []
    assert draft_claim_problems("I managed 12 engineers.", context=context)
    assert draft_claim_problems(text)  # without the context the date is an unverified number
