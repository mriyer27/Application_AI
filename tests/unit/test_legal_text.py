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


# --------------------------------------------------------------------------- #
# Round 2: typos, paraphrases, other languages, normalized text (final review)
# --------------------------------------------------------------------------- #
from job_agent.security.legal_text import is_never_drafted, is_yes_no_answer  # noqa: E402

#: Every variant from the reviewer's probe_legal_variants.py (25 of these 34 were missed before).
TYPO_PARAPHRASE_FOREIGN_LABELS = [
    # typos
    "Will you now or in the future require sponsership?",
    "Do you require sponser ship for employment?",
    "Will you need us to sponcer you?",
    "Will you now or in the future need visa sponsorhip?",
    "Are you authorised to wrok in the US?",
    "Are you a US citzen?",
    "Do you hold a securty clearence?",
    # paraphrases
    "Will you need us to file any paperwork with the government for you to keep working here?",
    "Is your right to remain in this country time-limited?",
    "What is your current status in the U.S.?",
    "Are you currently in the U.S. on a student status?",
    "Do you need a new employer to take over your H1 petition?",
    "Will your employment depend on the company filing on your behalf?",
    "Have you ever committed a crime?",
    "Have you ever been incarcerated or placed on probation?",
    "Do you have any pending charges?",
    "Have you ever been fined or imprisoned?",
    "Are you subject to any agreement with a former employer that limits where you can work?",
    "Is anyone you are related to employed here?",
    "Do you have a valid passport for the country where this job is located?",
    # other languages
    "¿Requiere patrocinio de visa?",
    "¿Está usted legalmente habilitado para trabajar en EE. UU.?",
    "¿Tiene permiso para trabajar en Estados Unidos?",
    "¿Necesitará una visa de trabajo?",
    "¿Es residente permanente?",
    "Você tem autorização para trabalhar nos EUA?",
    "Você precisa de visto?",
    "Benötigen Sie ein Visum?",
    "Benötigen Sie eine Arbeitsgenehmigung?",
    "Haben Sie die deutsche Staatsbürgerschaft?",
    "Avez-vous le droit de travailler au Canada?",
    "Avez-vous besoin d'un visa?",
    "Hai bisogno di un visto di lavoro?",
    "Heeft u een werkvergunning nodig?",
    # more near-misses and split words
    "Are you athorized to work in the United States?",
    "Do you need visa sponsorhsip?",
    "spon sorship needed?",
    "Are you a citi zen of the US?",
    "Are you elegible to work here?",
    "Have you ever been convicted of a felonny?",
    "Are you a permanent residant?",
    "Do you hold a TS/SCI cleraance?",
    "Have you ever been incarserated?",
    "What is your imigration status?",
    "Avez-vous une autorisation de travail?",
    "Ha bisogno di un permesso di lavoro?",
    "¿Es usted ciudadano de los Estados Unidos?",
    "Você é cidadão americano?",
    "Sind Sie im Besitz einer Arbeitserlaubnis?",
]


@pytest.mark.parametrize("label", TYPO_PARAPHRASE_FOREIGN_LABELS)
def test_typo_paraphrase_and_foreign_legal_questions_detected(label):
    assert is_legal_question(label), label
    assert is_never_drafted(label), label


@pytest.mark.parametrize("label", [
    "您是否需要签证担保?",           # Chinese
    "Нужна ли вам виза?",            # Russian (partly look-alike letters)
    "هل تحتاج إلى تأشيرة؟",          # Arabic
    "ビザのスポンサーが必要ですか？",    # Japanese
])
def test_non_latin_script_still_counts_as_legal(label):
    assert is_legal_question(label)


ROUND2_ORDINARY_LABELS = [
    "Which cities would you consider?", "Describe your experience with sensor data.",
    "Describe your visual design skills.", "Do you have signing authority for purchases?",
    "Are you willing to relocate?", "Are you willing to relocate for this role?", "If hired, when can you start?",
    "How did you hear about this job?", "Who referred you?", "Have you worked with dbt?",
    "Are you comfortable working on-site three days a week?", "What is your greatest strength?",
    "Have you previously worked for this company?", "Are you open to contract roles?", "Preferred work arrangement",
    "Tell us about a project you are proud of.", "Describe a time you worked on a team.",
    "Are you able to work overtime when needed?", "Can you commute to our office?",
    "¿Por qué quiere trabajar aquí?", "Nombre", "Apellido", "Correo electrónico", "Wie haben Sie von uns erfahren?",
    "Vorname", "Nachname", "Pourquoi voulez-vous travailler chez nous ?", "Prénom", "Straße und Hausnummer",
]


@pytest.mark.parametrize("label", ROUND2_ORDINARY_LABELS)
def test_round2_ordinary_labels_are_not_legal(label):
    assert not is_legal_question(label), label


#: Reviewer's probe_reply_variants.py / probe_reply_e2e.py statements (several were accepted before).
ROUND2_DRAFT_STATEMENTS = [
    "Thanks! For what it's worth, I'm a green-card holder, so no paperwork would be needed.",
    "Thanks! I will never need the company to file anything on my behalf to keep working.",
    "Thanks! I'm cleared at the TS level.",
    "Thanks! I have ⑤ years of analytics experience.",
    "Thanks! I bring ⑳ years in this field.",
    "Thanks! I have five years of Snowflake experience.",
    "Thanks! The team I directed had twelve analysts.",
    "Thanks! This candidate managed a team of ten analysts.",
    "Thanks! I ｍａｎａｇｅｄ a team of analysts.",
    "Thanks! I am a U.S. citzen and need no sponser.",
    "Thanks! Soy ciudadano estadounidense y no necesito patrocinio.",
    "Thanks! Ich brauche kein Visum.",
    "Thanks! Ich habe eine unbefristete Arbeitserlaubnis.",
    "Thanks! Tengo permiso para trabajar en Estados Unidos.",
    "Thanks! Tengo permiso para trabajar en Estados Unidos, sin restricciones.",
    "Thanks! I don't need any immigration support from you.",
    "Thanks! No visa needed on my end.",
    "Thanks! I'm a dual national (US/Canada).",
    "Thanks! I have no criminal record.",
    "Thanks! I've never been arrested.",
    "Thanks! I have never committed a crime.",
    "Thanks! I hold an active TS.",
    "Thanks! Je n'ai pas besoin de visa.",
    "Thanks! Ho un permesso di soggiorno.",
]


@pytest.mark.parametrize("text", ROUND2_DRAFT_STATEMENTS)
def test_round2_draft_statements_are_rejected(text):
    facts = FACTS + ["Led a team of 3 student volunteers to organize a campus analytics workshop."]
    assert draft_claim_problems(text, facts=facts, skills=SKILLS), text
    assert draft_claim_problems(text), text


@pytest.mark.parametrize("text", [
    "Thanks! I am a U.S. citzen and need no sponser.", "Thanks! Ich brauche kein Visum.",
    "Thanks! Tengo permiso para trabajar en Estados Unidos.", "Thanks! I'm cleared at the TS level.",
    "Thanks! I will never need the company to file anything on my behalf to keep working.",
])
def test_round2_work_authorization_statements_detected(text):
    assert mentions_legal_matter(text)
    assert "work-authorization" in " ".join(draft_claim_problems(text))


def test_non_ascii_numerals_are_read_and_rejected():
    assert numbers_mentioned("I bring ⑳ years in this field") == {"20"}
    assert numbers_mentioned("I have ５ years") == {"5"}
    problems = " | ".join(draft_claim_problems("I bring ⑳ years in this field.", facts=FACTS, skills=SKILLS))
    assert "not plain digits" in problems and "20" in problems
    # even a numeral that matches a verified fact is rejected: it would be rendered differently than checked
    assert draft_claim_problems("I converted ⑨ legacy SAS programs to Python.", facts=FACTS, skills=SKILLS)


def test_unreadable_script_in_drafts_is_rejected_but_trusted_context_is_not():
    assert "script" in " | ".join(draft_claim_problems("Thanks! Мне не нужна виза."))
    reply = "Hi [name],\n\nThank you for reaching out. I would be glad to learn more.\n\nBest regards,\n王伟"
    assert draft_claim_problems(reply)  # an unknown name in a script the checks cannot read
    assert draft_claim_problems(reply, context=("王伟",)) == []  # ... but the candidate's own name is fine
    polish = "Hi [name],\n\nThank you for reaching out.\n\nBest regards,\nŁukasz Żółć"
    assert draft_claim_problems(polish) == []  # Latin letters with accents/strokes are readable


@pytest.mark.parametrize("text", [
    "Thank you for reaching out. I would be glad to learn more about the role.",
    "I am happy to visit the office.", "I'm convinced this role is a great fit.",
    "I wanted to follow up on my application for the Data Analyst position at Acme.",
    "Please let me know which option works for the team.", "I look forward to hearing from you.",
    "Which cities are on the team's travel schedule?",
])
def test_round2_neutral_sentences_are_not_legal(text):
    assert not mentions_legal_matter(text), text


@pytest.mark.parametrize("answer, expected", [
    ("No, not now or in the future.", True), ("Yes.", True), ("yes", True), ("Nope", True), ("Yes I can", True),
    ("Sí, tengo permiso.", True), ("Não.", True), ("Ja – kein Problem", True),
    ("Non-profit analytics work with two agencies.", False), ("No problem was too small for our team.", False),
    ("I enjoy analytics work.", False),
])
def test_is_yes_no_answer(answer, expected):
    assert is_yes_no_answer(answer) is expected
