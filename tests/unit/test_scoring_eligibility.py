from __future__ import annotations

import re

import pytest

from job_agent.models import MatchCategory
from job_agent.scoring.eligibility import detect_eligibility_flags, has_blocker, needs_review
from job_agent.scoring.engine import score_job
from job_agent.scoring.text import sentences
from job_agent.security.textnorm import normalize_text, suspicious_characters

#: A flag message must never decide eligibility for the candidate.
_CLAIMS = re.compile(r"\b(?:eligib\w*|ineligib\w*|qualif\w*|disqualif\w*|you\s+are|you're|you\s+cannot|you\s+can't|"
                     r"you\s+may\s+not|not\s+allowed|approved|denied)\b", re.I)

CASES = [
    ("We will not sponsor visas for this role.", "no_sponsorship", "review"),
    ("The company is unable to sponsor employment visas.", "no_sponsorship", "review"),
    ("There is no visa sponsorship available for this position.", "no_sponsorship", "review"),
    ("Applicants must be able to work without sponsorship.", "no_sponsorship", "review"),
    ("Candidates must be authorized to work in the U.S. without employer sponsorship now or in the future.",
     "no_sponsorship", "review"),
    ("We are not able to provide sponsorship at this time.", "no_sponsorship", "review"),
    ("Must be a U.S. citizen.", "citizenship_required", "blocker"),
    ("US citizenship required for this position.", "citizenship_required", "blocker"),
    ("Applicants must be a citizen of the United States.", "citizenship_required", "blocker"),
    ("Must hold a green card.", "permanent_residency_required", "blocker"),
    ("This role is open to permanent residents only.", "permanent_residency_required", "blocker"),
    ("Requires an active security clearance.", "clearance_required", "blocker"),
    ("Active TS/SCI with polygraph is mandatory.", "clearance_required", "blocker"),
    ("Secret clearance required.", "clearance_required", "blocker"),
    ("This role is subject to ITAR.", "export_control", "blocker"),
    ("You will work with EAR-controlled technology.", "export_control", "blocker"),
    ("Candidates must be a U.S. person under export control regulations.", "export_control", "blocker"),
    ("Visa sponsorship available for this role.", "sponsorship_available", "info"),
    ("We sponsor H-1B visas for qualified candidates.", "sponsorship_available", "info"),
    ("This program is for December 2026 graduates.", "graduation_timing", "review"),
    ("You must have graduated by August 2026.", "graduation_timing", "review"),
    ("Start date in 2026.", "graduation_timing", "review"),
    ("Must have completed a Master's degree.", "graduation_timing", "review"),
]


@pytest.mark.parametrize("sentence,code,severity", CASES)
def test_flag_patterns_quote_the_listing_verbatim(profile, sentence, code, severity):
    description = f"We build analytics products for retailers. {sentence} Apply on our careers page."
    flags = detect_eligibility_flags({"description_text": description}, profile)
    match = [f for f in flags if f.code == code]
    assert match, f"{code} not detected in {sentence!r}: {flags}"
    flag = match[0]
    assert flag.severity == severity
    assert flag.excerpt == sentence
    assert flag.excerpt in description
    assert flag.message.startswith("Review: the listing states")


@pytest.mark.parametrize("sentence,code,severity", CASES)
def test_flags_never_claim_eligibility(profile, sentence, code, severity):
    for flag in detect_eligibility_flags({"description_text": sentence}, profile):
        assert not _CLAIMS.search(flag.message), flag.message
        assert "legal determination" in flag.message or flag.code == "graduation_timing"


@pytest.mark.parametrize(
    "text,absent",
    [
        ("No security clearance required.", "clearance_required"),
        ("No visa sponsorship available.", "sponsorship_available"),
        ("We are unable to sponsor visas.", "sponsorship_available"),
        ("We do not offer relocation assistance.", "no_sponsorship"),
        ("We do not discriminate on the basis of race, citizenship, or national origin.", "citizenship_required"),
        ("U.S. citizenship is not required for this role.", "citizenship_required"),
        ("We offer green card sponsorship after one year.", "permanent_residency_required"),
        ("Open to candidates graduating in May 2027 or later.", "graduation_timing"),
        ("Class of 2027 graduates are welcome.", "graduation_timing"),
        ("Founded in 2015, we hire new graduates every year.", "graduation_timing"),
        ("Must have completed a Bachelor's degree.", "graduation_timing"),
        ("Hear what we have to say: every year we grow.", "export_control"),
    ],
)
def test_negations_and_non_requirements_do_not_flag(profile, text, absent):
    codes = {f.code for f in detect_eligibility_flags({"description_text": text}, profile)}
    assert absent not in codes


def test_no_visa_sponsorship_available_is_not_sponsorship_available(profile):
    codes = [f.code for f in detect_eligibility_flags({"description_text": "No visa sponsorship available."}, profile)]
    assert codes == ["no_sponsorship"]


def test_graduation_timing_compares_against_profile_not_hardcoded(profile):
    job = {"description_text": "This rotational program is for May 2027 graduates."}
    assert not [f for f in detect_eligibility_flags(job, profile) if f.code == "graduation_timing"]

    later = profile.model_copy(deep=True)
    later.education[0].end = "December 2027"
    flags = [f for f in detect_eligibility_flags(job, later) if f.code == "graduation_timing"]
    assert len(flags) == 1
    assert "December 2027" in flags[0].message and "May 2027" in flags[0].message

    graduated = profile.model_copy(deep=True)
    for edu in graduated.education:
        edu.expected = False
    assert not detect_eligibility_flags({"description_text": "Open to December 2026 graduates."}, graduated)


def test_start_date_ignored_for_internships(profile):
    job = {"title": "Data Analyst Intern", "description_text": "Start date in June 2026."}
    assert not detect_eligibility_flags(job, profile)
    job["title"] = "Data Analyst"
    assert [f.code for f in detect_eligibility_flags(job, profile)] == ["graduation_timing"]


def test_sources_are_combined_and_deduplicated(profile):
    job = {
        "sponsorship_info": "We will not sponsor visas.",
        "work_authorization_requirements": "Must be a U.S. citizen.",
        "description_text": "Great team. We will not sponsor visas. Must be a U.S. citizen. Apply now.",
    }
    flags = detect_eligibility_flags(job, profile)
    assert [(f.code, f.excerpt) for f in flags] == [
        ("citizenship_required", "Must be a U.S. citizen."),
        ("no_sponsorship", "We will not sponsor visas."),
    ]


def test_long_sentence_excerpt_is_a_verbatim_window(profile):
    filler = "We are a fast growing company with many teams and products and customers " * 8
    text = f"{filler}and unfortunately we will not sponsor work visas for any role in {filler.strip()}."
    flags = detect_eligibility_flags({"description_text": text}, profile)
    assert flags and flags[0].code == "no_sponsorship"
    assert flags[0].excerpt in text and "will not sponsor" in flags[0].excerpt
    assert len(flags[0].excerpt) <= 400


# --------------------------------------------------------------------------- #
# Regression: statements the earlier rules missed (review-truth/probe_elig.py, 19 of 23 missed)
# --------------------------------------------------------------------------- #
SEVERITY = {
    "no_sponsorship": "review",
    "citizenship_required": "blocker",
    "permanent_residency_required": "blocker",
    "clearance_required": "blocker",
    "export_control": "blocker",
    "work_authorization_required": "info",
    "sponsorship_available": "info",
}
ALL_CODES = set(SEVERITY) | {"graduation_timing", "eligibility_statement"}

PROBE_CASES = [
    ("Candidates requiring H-1B now or in the future will not be considered.", {"no_sponsorship"}),
    ("This position does not qualify for sponsorship.", {"no_sponsorship"}),
    ("Sponsorship will not be provided for this role.", {"no_sponsorship"}),
    ("We are unable to support visa applications for this role.", {"no_sponsorship"}),
    ("Applicants must be currently authorized to work in the United States on a full-time basis.",
     {"work_authorization_required"}),
    ("Must be authorized to work in the US for any employer.", {"no_sponsorship"}),
    ("OPT/CPT candidates will not be considered.", {"no_sponsorship"}),
    ("F-1 students are not eligible for this position.", {"no_sponsorship"}),
    ("This role is not open to candidates on student visas.", {"no_sponsorship"}),
    ("Applicants must be U.S. citizens; an active clearance is not required but you must be able to obtain one.",
     {"citizenship_required", "clearance_required"}),
    ("U.S. citizenship is required, but prior experience is not required.", {"citizenship_required"}),
    ("Due to federal contract requirements, U.S. citizenship is required and all qualified applicants will receive "
     "consideration without regard to race, color or national origin.", {"citizenship_required"}),
    ("Must hold an active TS/SCI; candidates with no active clearance will not be considered.", {"clearance_required"}),
    ("Must hold an active Secret.", {"clearance_required"}),
    ("Ability to obtain a Public Trust is required.", {"clearance_required"}),
    ("Must be a U.S. national.", {"citizenship_required"}),
    ("Only US nationals may apply.", {"citizenship_required"}),
    ("Must be a US Person as defined by 22 CFR 120.62.", {"export_control"}),
    ("This role requires access to export-controlled technology.", {"export_control"}),
    ("Must be eligible for a DoD clearance.", {"clearance_required"}),
    ("Green card holders or citizens only.", {"permanent_residency_required", "citizenship_required"}),
    ("Applicants must be able to pass a federal background investigation and obtain clearance.",
     {"clearance_required"}),
    ("Must be clearable.", {"clearance_required"}),
]

#: Further phrasings from the C6 pattern families (sponsorship, visa types, citizenship, clearance, export control).
MORE_CASES = [
    ("We do not provide relocation or visa support.", {"no_sponsorship"}),
    ("We cannot consider applicants who require sponsorship now or in the future.", {"no_sponsorship"}),
    ("Applicants must not require employer sponsorship now or in the future.", {"no_sponsorship"}),
    ("Candidates must be able to work without the need for an H-1B visa.", {"no_sponsorship"}),
    ("Sponsorship is unavailable for this position.", {"no_sponsorship"}),
    ("This position is ineligible for H-1B sponsorship.", {"no_sponsorship"}),
    ("Employment-based immigration sponsorship is not available.", {"no_sponsorship"}),
    ("Candidates on J-1 visas are not eligible.", {"no_sponsorship"}),
    ("Unrestricted work authorization required.", {"no_sponsorship"}),
    ("Must be eligible to work in the United States.", {"work_authorization_required"}),
    ("Must be a U.S. citizen or national.", {"citizenship_required"}),
    ("Only American citizens may apply.", {"citizenship_required"}),
    ("Applicants must be U.S. citizens or lawful permanent residents (LPRs).",
     {"citizenship_required", "permanent_residency_required"}),
    ("Requires an active Top Secret clearance with full scope poly.", {"clearance_required"}),
    ("Must possess an active Public Trust.", {"clearance_required"}),
    ("Polygraph required.", {"clearance_required"}),
    ("Clearance-eligible candidates preferred.", {"clearance_required"}),
    ("Access to export-restricted data requires U.S. person status.", {"export_control"}),
    ("This role requires compliance with export regulations under 15 CFR 730.", {"export_control"}),
]


def _codes(text: str, profile) -> set[str]:
    return {f.code for f in detect_eligibility_flags({"description_text": text, "title": "Data Analyst"}, profile)}


@pytest.mark.parametrize("sentence,codes", PROBE_CASES + MORE_CASES)
def test_eligibility_statements_are_flagged_verbatim(profile, sentence, codes):
    description = f"We build analytics products for retailers. {sentence} Apply on our careers page."
    flags = detect_eligibility_flags({"description_text": description, "title": "Data Analyst"}, profile)
    by_code = {f.code: f for f in flags}
    assert codes <= set(by_code), f"{codes - set(by_code)} not detected in {sentence!r}: {flags}"
    for code in codes:
        flag = by_code[code]
        assert flag.severity == SEVERITY[code]
        assert flag.excerpt == sentence
        assert flag.message.startswith("Review: the listing states")
        assert "legal determination" in flag.message
        assert not _CLAIMS.search(flag.message), flag.message


@pytest.mark.parametrize("sentence,codes", PROBE_CASES + MORE_CASES)
def test_new_flag_messages_never_claim_eligibility(profile, sentence, codes):
    for flag in detect_eligibility_flags({"description_text": sentence}, profile):
        assert not _CLAIMS.search(flag.message), flag.message


@pytest.mark.parametrize(
    "sentence,code",
    [
        # a negation / EEO wording in *another clause* of the sentence must not hide the requirement
        ("U.S. citizenship is required, but prior experience is not required.", "citizenship_required"),
        ("Prior experience is not required and U.S. citizenship is required.", "citizenship_required"),
        ("Clearance not required and U.S. citizenship required.", "citizenship_required"),
        ("U.S. citizenship is required; we do not discriminate on the basis of race or religion.",
         "citizenship_required"),
        ("We do not discriminate on the basis of age, but U.S. citizenship is required for this role.",
         "citizenship_required"),
        ("Without regard to race, color or religion, U.S. citizenship is required for this position.",
         "citizenship_required"),
        ("Must be a permanent resident, but relocation is not required.", "permanent_residency_required"),
        ("We sponsor H-1B visas, but no relocation assistance is offered.", "sponsorship_available"),
        # the negation is itself the exclusion
        ("Non-U.S. citizens need not apply.", "citizenship_required"),
        ("Candidates with no active clearance will not be considered.", "clearance_required"),
        ("No clearance required, but you must be able to obtain one.", "clearance_required"),
        ("An active clearance is not required, though candidates must be eligible for a Secret clearance.",
         "clearance_required"),
    ],
)
def test_exceptions_apply_only_to_their_own_clause(profile, sentence, code):
    flags = [f for f in detect_eligibility_flags({"description_text": sentence}, profile) if f.code == code]
    assert flags, f"{code} hidden in {sentence!r}"
    assert flags[0].excerpt == sentence


@pytest.mark.parametrize(
    "text,absent",
    [
        # equal-opportunity statements that list citizenship / immigration status as protected categories
        ("We do not discriminate on the basis of race, color, religion, national origin, U.S. citizenship status, "
         "or disability.", {"citizenship_required"}),
        ("All qualified applicants will receive consideration without regard to race, color, religion, sex, national "
         "origin, disability, protected veteran status, and U.S. citizenship or immigration status.",
         {"citizenship_required", "no_sponsorship", "permanent_residency_required"}),
        ("Employment decisions are made regardless of U.S. citizenship, permanent residency or immigration status.",
         {"citizenship_required", "permanent_residency_required", "no_sponsorship"}),
        ("We will not consider your immigration status in hiring decisions.", {"no_sponsorship"}),
        ("Visa status will not be considered in hiring decisions.", {"no_sponsorship"}),
        # negations of the requirement itself
        ("U.S. citizenship preferred but not required.", {"citizenship_required"}),
        ("U.S. citizenship and security clearance are not required.", {"citizenship_required", "clearance_required"}),
        ("Security clearance: not required.", {"clearance_required"}),
        ("Security clearance is not required, but candidates must be eligible to work in the US.",
         {"clearance_required"}),
        # look-alike wording that is not an eligibility condition
        ("Applicants who opt out of the assessment will not be considered.", {"no_sponsorship"}),
        ("Incomplete applications will not be considered.", ALL_CODES),
        ("Work with business sponsors to scope analysis.", ALL_CODES),
        ("Executive sponsors will not be available during onboarding.", ALL_CODES),
        ("Our clients include Visa and Mastercard.", ALL_CODES),
        ("Must be able to work in a fast-paced environment.", {"work_authorization_required"}),
        ("We serve U.S. national accounts and U.S. national laboratories.", {"citizenship_required"}),
        ("Support our U.S. national security customers.", {"citizenship_required"}),
        ("Join a team of citizen developers.", {"citizenship_required"}),
        ("Obtain customs clearance for international shipments.", {"clearance_required"}),
        ("Help maintain public trust in our institutions.", {"clearance_required"}),
        ("In compliance with federal law, all persons hired will be required to verify identity and eligibility to "
         "work in the United States and to complete the required employment eligibility verification form upon hire.",
         {"work_authorization_required", "no_sponsorship"}),
    ],
)
def test_true_negatives_still_do_not_flag(profile, text, absent):
    assert not (_codes(text, profile) & absent), detect_eligibility_flags({"description_text": text}, profile)


def test_work_authorization_statement_is_info_and_redundant_with_no_sponsorship(profile):
    generic = detect_eligibility_flags(
        {"description_text": "Applicants must be currently authorized to work in the United States."}, profile)
    assert [(f.code, f.severity) for f in generic] == [("work_authorization_required", "info")]
    assert not needs_review(generic) and not has_blocker(generic)

    any_employer = detect_eligibility_flags(
        {"description_text": "Must be authorized to work in the US for any employer."}, profile)
    assert [f.code for f in any_employer] == ["no_sponsorship"]
    assert needs_review(any_employer)


def test_new_requirement_flags_block_the_shortlist(profile):
    for text in ("Must hold an active Secret.", "Must be a U.S. national.", "Must be clearable.",
                 "U.S. citizenship is required, but prior experience is not required."):
        assert has_blocker(detect_eligibility_flags({"description_text": text}, profile)), text


def test_llm_statements_go_through_the_broadened_rules(profile):
    flags = detect_eligibility_flags({}, profile, extra_texts=["OPT/CPT candidates will not be considered."])
    assert [(f.code, f.excerpt) for f in flags] == [("no_sponsorship", "OPT/CPT candidates will not be considered.")]


def test_one_flag_per_code_for_a_sentence_with_several_matches(profile):
    sentence = "Must hold an active TS/SCI; candidates with no active clearance will not be considered."
    flags = detect_eligibility_flags({"description_text": sentence}, profile)
    assert [(f.code, f.excerpt) for f in flags] == [("clearance_required", sentence)]


def test_sentence_splitter_keeps_abbreviations_together():
    parts = sentences("Must be a U.S. citizen. Experience with e.g. SQL is required. Apply today!")
    assert parts == ["Must be a U.S. citizen.", "Experience with e.g. SQL is required.", "Apply today!"]
    assert sentences("• Python\n• SQL; reporting") == ["Python", "SQL; reporting"]


# --------------------------------------------------------------------------- #
# Round 2: paraphrases the broadened rules missed (final-reattack/elig/probe_elig_variants.py, 20 of 42 missed)
# --------------------------------------------------------------------------- #
FALLBACK = "eligibility_statement"

R2_PROBE_CASES = [
    # sponsorship paraphrases
    ("We are not in a position to sponsor employment visas.", {"no_sponsorship"}),
    ("Applicants who will need an employer to file a petition on their behalf cannot be considered.",
     {"no_sponsorship"}),
    ("Candidates must not now or in the future need assistance with work authorization.", {"no_sponsorship"}),
    ("The company will not file H1B paperwork for this opening.", {"no_sponsorship"}),
    ("No visa support available.", {"no_sponsorship"}),
    ("We cannot transfer H-1Bs.", {"no_sponsorship"}),
    ("Candidates requiring sponser ship will not be considered.", {"no_sponsorship"}),
    ("This role is open only to those who can work without our support for immigration paperwork.",
     {"no_sponsorship"}),
    ("Must have permanent work authorization that does not expire.", {"no_sponsorship"}),
    ("Must have unrestricted work authorization.", {"no_sponsorship"}),
    ("Must be able to work for any employer without restriction.", {"no_sponsorship"}),
    ("Must not require the company's assistance to remain employable in the US.", {"no_sponsorship"}),
    ("Candidates on time-limited work permits are not eligible.", {"no_sponsorship"}),
    ("Only candidates with lifetime right to work in the US will be considered.", {"no_sponsorship"}),
    # citizenship paraphrases
    ("This position is restricted to American citizens.", {"citizenship_required"}),
    ("Applicants must possess U.S. citizenship.", {"citizenship_required"}),
    ("Open to US nationals and lawful permanent residents only.",
     {"citizenship_required", "permanent_residency_required"}),
    ("Must be born in the United States.", {"citizenship_required"}),
    ("Must hold a valid U.S. passport.", {"citizenship_required"}),
    ("Only citizens may apply.", {"citizenship_required"}),
    ("Candidates must be citizens of the USA.", {"citizenship_required"}),
    ("Due to government contract requirements, only U.S. persons may be hired.", {"export_control"}),
    ("Applicants must be a USC.", {"citizenship_required"}),
    ("Must be a U.S. Citizen (no dual citizens).", {"citizenship_required"}),
    ("Applicants must be US citizen or green card holder.", {"citizenship_required", "permanent_residency_required"}),
    # clearance paraphrases
    ("Must currently possess a DoD Top Secret.", {"clearance_required"}),
    ("Active TS required.", {"clearance_required"}),
    ("Candidates must be able to obtain and maintain a federal government security investigation.",
     {"clearance_required"}),
    ("Must be able to pass a Tier 3 investigation.", {"clearance_required"}),
    ("Must be able to pass a full-scope poly.", {"clearance_required"}),
    ("Requires an active Q clearance.", {"clearance_required"}),
    ("Must hold DOE L access authorization.", {"clearance_required"}),
    ("Must be suitable for a position of public trust.", {"clearance_required"}),
    ("Must be able to pass a government background investigation for access to classified information.",
     {"clearance_required"}),
    ("Access to classified information is required.", {"clearance_required"}),
    ("This role requires an SF-86.", {"clearance_required"}),
    # export control paraphrases
    ("This position involves access to technology subject to the International Traffic in Arms Regulations.",
     {"export_control"}),
    ("Applicants must qualify as a U.S. person under 22 CFR 120.15.", {"export_control"}),
    ("You must be able to access EAR-controlled data.", {"export_control"}),
    ("Due to OFAC requirements, candidates must not be nationals of embargoed countries.", {"export_control"}),
    # Spanish
    ("Se requiere ciudadanía estadounidense.", {"citizenship_required"}),
    ("No ofrecemos patrocinio de visa.", {"no_sponsorship"}),
    # further phrasings of the same families
    ("Applicants must be nationals of the U.S. or allied nations.", {"citizenship_required"}),
    ("Cleared candidates only.", {"clearance_required"}),
    ("Must complete the e-QIP process.", {"clearance_required"}),
    ("USC/GC only.", {"citizenship_required", "permanent_residency_required"}),
    ("Applicants on OPT are excluded.", {"no_sponsorship"}),
    ("Must have a lifetime right to work in the US.", {"no_sponsorship"}),
]


@pytest.mark.parametrize("sentence,codes", R2_PROBE_CASES)
def test_round2_paraphrases_raise_review_or_blocker(profile, sentence, codes):
    description = f"We build analytics products for retailers. {sentence} Apply on our careers page."
    flags = detect_eligibility_flags({"description_text": description, "title": "Data Analyst"}, profile)
    assert needs_review(flags), f"{sentence!r} is not sent to review: {flags}"
    by_code = {f.code: f for f in flags}
    assert codes <= set(by_code), f"{codes - set(by_code)} not detected in {sentence!r}: {flags}"
    for code in codes:
        flag = by_code[code]
        assert flag.severity == SEVERITY[code]
        assert flag.excerpt == sentence
        assert flag.message.startswith("Review: the listing states") and "legal determination" in flag.message
        assert not _CLAIMS.search(flag.message), flag.message


# Wording no specific rule recognises: the conservative fallback still sends it to a human.
FALLBACK_CASES = [
    ("Must be authorized to work in the US with a permanent visa.", "visas"),
    ("Must be authorized to work in the United States and must not hold a temporary visa.", "visas"),
    ("Candidates with a temporary visa status will not be considered.", "visas"),
    ("Must hold a valid passport from a NATO country.", "a passport"),
    ("Foreign nationals will not be considered.", "nationality"),
    ("Applicants must not hold dual nationality.", "nationality"),
    ("Only nationals of Five Eyes countries may apply.", "nationality"),
    ("Candidates must not be dual citizens.", "citizenship"),
    ("Candidates must have a clearance.", "a security clearance"),
    ("Must be able to get a clearance.", "a security clearance"),
    ("Applicants must be able to remain in the country without our immigration help.", "immigration"),
    ("Must possess an unexpired employment authorization document.", "work authorization"),
    ("Must hold the right to live and work in Canada.", "work authorization"),
    ("Must be able to work on an H1B transfer.", "visas"),
    ("We sponsor visas only for candidates with a U.S. passport.", "a passport"),
    # wording that tries to hide behind an exemption (EEO list, "we sponsor", "not required", EEO status wording)
    ("Applicants of any nationality other than embargoed countries may apply.", "sanctions / embargoes"),
    ("We welcome applicants who are not on student visas.", "visas"),
    ("Regardless of experience, candidates must hold a passport issued by the State Department.", "a passport"),
    ("No clearance required: you must already hold one.", "a security clearance"),
    ("U.S. citizenship is not required, but U.S. nationality is.", "nationality"),
    ("We sponsor visas, but not for this role.", "visas"),
    ("We sponsor visas for candidates except those on OPT.", "visas"),
    ("Sponsorship: not required.", "sponsorship"),  # ambiguous: the candidate may be the one who must not require it
    ("Se requiere nacionalidad española.", "nationality"),
]


@pytest.mark.parametrize("sentence,topic", FALLBACK_CASES)
def test_fallback_flags_unrecognised_eligibility_wording(profile, sentence, topic):
    description = f"We build analytics products for retailers. {sentence} Apply on our careers page."
    flags = detect_eligibility_flags({"description_text": description, "title": "Data Analyst"}, profile)
    assert needs_review(flags), f"{sentence!r} is not sent to review: {flags}"
    fallback = [f for f in flags if f.code == FALLBACK]
    assert len(fallback) == 1, flags
    flag = fallback[0]
    assert flag.severity == "review"
    assert flag.excerpt == sentence
    assert flag.message.startswith("Review: the listing mentions ")
    assert topic in flag.message and "read the exact wording" in flag.message
    assert "legal determination" in flag.message
    assert not _CLAIMS.search(flag.message), flag.message


@pytest.mark.parametrize(
    "sentence",
    [
        "Without regard to race, color, or religion, applicants on H-1B are excluded.",
        "Visa status will not be considered in hiring decisions, but OPT holders need not apply.",
        "We will not consider your immigration status unless you require a visa.",
        "All qualified applicants will receive consideration without regard to race or citizenship; however, "
        "applicants must be US persons.",
        "Security clearance is not required, but you must be able to get one.",
        "Visas are not sponsored.",
        "Requires a DOE Q.",
        "Must be a natural-born American.",
        "Candidates must be persons lawfully admitted for permanent residence.",
        "Must pass a Tier 5 reinvestigation.",
        "Applicants must not be citizens of embargoed countries.",
        "Applicants requiring an EAD extension cannot be considered.",
        "This job requires the candidate to not need employer support for a work visa.",
        "Applicants requiring a Visa will not be considered.",
        "VISA SPONSORSHIP NOT AVAILABLE.",
    ],
)
def test_exemption_wording_cannot_hide_a_requirement(profile, sentence):
    flags = detect_eligibility_flags({"description_text": sentence, "title": "Data Analyst"}, profile)
    assert needs_review(flags), f"{sentence!r} is not sent to review: {flags}"
    assert all(f.excerpt == sentence for f in flags)


def test_fallback_is_not_added_when_a_specific_rule_already_quotes_the_sentence(profile):
    for sentence in ("Must hold a valid U.S. passport.", "No visa support available.",
                     "Access to classified information is required."):
        flags = detect_eligibility_flags({"description_text": sentence}, profile)
        assert FALLBACK not in {f.code for f in flags}, flags
        assert len(flags) == 1


@pytest.mark.parametrize(
    "sentence,code",
    [
        ("We sponsor H-1B visas for qualified candidates.", "sponsorship_available"),
        ("We offer green card sponsorship after one year.", "sponsorship_available"),
        ("Visa sponsorship available for this role.", "sponsorship_available"),
        ("H-1B sponsorship is offered for this role.", "sponsorship_available"),
        ("Applicants must be currently authorized to work in the United States on a full-time basis.",
         "work_authorization_required"),
        ("Must be eligible to work in the United States.", "work_authorization_required"),
        ("Only candidates authorized to work in the US will be considered.", "work_authorization_required"),
    ],
)
def test_pure_info_statements_stay_info(profile, sentence, code):
    flags = detect_eligibility_flags({"description_text": sentence}, profile)
    assert [(f.code, f.severity) for f in flags] == [(code, "info")]
    assert not needs_review(flags)


@pytest.mark.parametrize(
    "text",
    [
        # equal-opportunity boilerplate listing protected categories
        "All qualified applicants will receive consideration for employment without regard to race, color, religion, "
        "sex, sexual orientation, gender identity, national origin, disability, protected veteran status, age, genetic "
        "information, pregnancy, marital status, citizenship status, or any other characteristic protected by law.",
        "We are an equal opportunity employer and do not discriminate based on citizenship status or national origin.",
        "We do not discriminate on the basis of race, color, religion, national origin, U.S. citizenship status, or "
        "disability.",
        "Employment decisions are made regardless of U.S. citizenship, permanent residency or immigration status.",
        "We will not consider your immigration status in hiring decisions.",
        "Visa status will not be considered in hiring decisions.",
        "We welcome applicants of every nationality and background.",
        # Visa the card brand / company
        "Our clients include Visa and Mastercard.",
        "Partner with Visa, Mastercard and American Express on payment analytics.",
        "Join the Visa analytics team in San Francisco.",
        "Experience with Visa or Mastercard settlement data is a plus.",
        # clearance in a customs / retail sense
        "Obtain customs clearance for international shipments.",
        "Partner with merchants on markdown and clearance strategy.",
        "Analyze clearance performance and sell-through by store.",
        # national / citizen / sponsor / public trust / export in a business sense
        "We serve U.S. national accounts and U.S. national laboratories.",
        "Support our U.S. national security customers.",
        "Work with business sponsors to scope analysis.",
        "Secure executive sponsorship for analytics initiatives.",
        "Manage sponsorship deals for our sports clients.",
        "Executive sponsors will not be available during onboarding.",
        "Join a team of citizen developers.",
        "Improve citizen services for the city.",
        "Corporate citizenship and sustainability reporting.",
        "Help maintain public trust in our institutions.",
        "Export data to Excel and build dashboards.",
        "Perform OFAC sanctions screening of transactions.",
        "This position is classified as exempt under the FLSA.",
        "Must be able to work in a fast-paced environment.",
        "Employees are eligible to work remotely two days a week.",
        "Must pass a background check.",
        "Tier 1 support experience with ticketing tools.",
        "Build F1 score dashboards for model evaluation.",
        # Form I-9 / E-Verify onboarding wording
        "In compliance with federal law, all persons hired will be required to verify identity and eligibility to work "
        "in the United States and to complete the required employment eligibility verification form upon hire.",
        "Company participates in E-Verify and will provide the federal government with your Form I-9 information to "
        "confirm that you are authorized to work in the U.S.",
        # a whole sentence that only says the requirement does not apply
        "No security clearance required.",
        "U.S. citizenship is not required for this role.",
        "U.S. citizenship and security clearance are not required.",
        "Security clearance: not required.",
        "No polygraph required.",
        "U.S. citizenship preferred but not required.",
    ],
)
def test_round2_true_negatives_raise_no_flags(profile, text):
    flags = detect_eligibility_flags({"description_text": text, "title": "Data Analyst"}, profile)
    assert flags == [], flags


@pytest.mark.parametrize(
    "sentence",
    [
        "The candidate must be a U​.S​. citi​zen.",  # zero-width spaces
        "Must be a U.S. сitizen.",  # Cyrillic "с"
        "Ｍｕｓｔ ｂｅ ａ Ｕ．Ｓ． ｃｉｔｉｚｅｎ．",
        "No spon­sorship available.",  # soft hyphen
        "We will not spon​sor visas.",
    ],
)
def test_unicode_tricks_are_normalized_and_the_excerpt_is_the_checked_text(profile, sentence):
    description = f"We build analytics products for retailers. {sentence} Apply on our careers page."
    flags = detect_eligibility_flags({"description_text": description}, profile)
    assert needs_review(flags), f"{sentence!r} is not sent to review: {flags}"
    for flag in flags:
        assert flag.excerpt == normalize_text(sentence)  # stored text is exactly the validated (normalized) text
        assert flag.excerpt in normalize_text(description)
        assert suspicious_characters(flag.excerpt) == [], flag.excerpt


def test_fallback_flag_moves_the_job_to_needs_review(config, profile):
    description = (
        "Acme Analytics is hiring a Business Analyst to join our insights team in New York. "
        "You will partner with finance and operations leaders to analyze trends, build clear reports, "
        "and recommend improvements. You will receive mentorship from experienced colleagues. "
        "We value curiosity, clear writing, and ownership of outcomes. This is a full-time position "
        "based in our New York office with a collaborative culture."
    )
    job = {
        "id": 7, "job_uid": "J-crafted001", "title": "Business Analyst", "employer": "Acme Analytics",
        "location": "New York, NY", "work_arrangement": "onsite", "employment_type": "full_time",
        "description_text": description,
        "required_qualifications": '["Proficiency in SQL and Python", "Experience with Tableau"]',
        "preferred_qualifications": "[]", "salary_min": 95000.0, "salary_max": 120000.0, "salary_currency": "USD",
        "salary_period": "year", "salary_is_base": 1, "content_hash": "c-crafted",
    }
    clean = score_job(job, profile, config)
    assert clean.category != MatchCategory.NEEDS_REVIEW and not clean.eligibility_flags

    flagged = score_job({**job, "description_text": description + " Foreign nationals will not be considered."},
                        profile, config)
    assert [f.code for f in flagged.eligibility_flags] == [FALLBACK]
    assert flagged.category == MatchCategory.NEEDS_REVIEW
    assert flagged.recommended_next_action == "Review eligibility statement before investing time"
    assert any(r.startswith("Eligibility-related wording") for r in flagged.reasons_not_to_apply)


# --------------------------------------------------------------------------- #
# Round 3: employer boilerplate that put whole employers into needs_review (E1), and requirement wording that
# got no flag at all (E2-E4). Corpus scan: round3/elig (1,080 real listings).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        # anti-scam notices (Duolingo, every listing)
        "Duolingo and our employees will never ask for your Social Security number, bank details, or passport info, "
        "and we’ll never ask you to deposit a check, purchase equipment, or exchange money during the interview "
        "process.",
        "We will never ask for your bank details, Social Security number, or passport during the interview process.",
        # Form I-9 wording phrased as "verification of your identity and eligibility to work"
        "Employment is contingent upon verification of your identity and eligibility to work in the United States.",
        # "clearance" of a drug screen / background check
        "Offers are contingent on a background check and drug screen clearance.",
        # business senses seen in live listings
        "- Manage Integrated Solutions approvals and vetting across The Ringer podcast portfolio, triaging incoming "
        "sponsorship requests, coordinating with the right partners, and tracking requests through resolution.",
        "- You understand change management, including sponsorship models, adoption curves, resistance patterns, and "
        "how to build internal champions.",
        "- Formulate pricing and deal economics for sponsorships, tentpole packages, and multi-product bundles, "
        "balancing revenue optimization with competitive positioning and advertiser value",
        "- Collaborate with business stakeholders to ensure alignment and secure sponsorship for AI initiatives",
        "Exposure to trademark matters, such as clearance, enforcement, or prosecution support, or to hardware or "
        "consumer product compliance, is a plus.",
        "Experience recruiting in Ireland or Portugal, including local compensation norms, notice periods, and "
        "relocation and work authorization requirements.",
        "Experience coordinating immigration, work authorization, relocation, or external counsel/vendor workflows.",
        "- Coordinate active immigration, work-authorization, relocation, and business-travel cases with employees, "
        "external counsel, vendors, and internal partners.",
        "- 3+ years in People Operations, immigration operations, global mobility, employee lifecycle operations, or "
        "another high-detail service environment.",
        "Our team is building an investment account for every child born in America, in partnership with the U.S. "
        "Treasury — one that grows with them into adulthood.",
        "Benefits include relocation assistance and immigration support for eligible employees.",
        "We offer visa and relocation support for international hires.",
        "Our team represents more than 40 nationalities.",
        "Right To Work Poster (English)",
        "Must have a valid passport and be able to travel internationally.",
        # physical clearances
        "Ground clearance of at least 8 inches is required for the field vehicle.",
        "Minimum clearance of 36 inches is required in front of the electrical panel.",
        "Ensure a 36-inch clearance in front of all panels.",
    ],
)
def test_round3_boilerplate_raises_no_flags(profile, text):
    flags = detect_eligibility_flags({"description_text": text, "title": "Data Analyst"}, profile)
    assert flags == [], flags


@pytest.mark.parametrize(
    "text",
    [
        # Samsara (every listing): a plain right-to-work condition, like "must be authorized to work"
        "All offers of employment are contingent upon an individual’s ability to secure and maintain the legal right "
        "to work at the company and in the specified work location, if applicable.",
        "Candidates must have the right to work in the UK.",
        "Eligibility to Work in Singapore",
        "ELIGIBILITY TO WORK IN SG",
        "You must have the legal right to work in Canada.",
    ],
)
def test_round3_plain_right_to_work_is_info(profile, text):
    flags = detect_eligibility_flags({"description_text": text, "title": "Data Analyst"}, profile)
    assert [(f.code, f.severity) for f in flags] == [("work_authorization_required", "info")], flags
    assert flags[0].excerpt == text and flags[0].message.startswith("Review: the listing states")
    assert not needs_review(flags)


@pytest.mark.parametrize(
    "sentence,codes",
    [
        # E2: "minimum" no longer hides a security clearance
        ("Minimum Clearance Required: Secret", {"clearance_required"}),
        ("Candidates must hold a minimum clearance of Secret.", {"clearance_required"}),
        ("Clearance Level: Secret", {"clearance_required"}),
        ("Clearance: Secret required.", {"clearance_required"}),
        # E3: words between "sponsorship" and "is unavailable"
        ("Sponsorship for this program is unavailable.", {"no_sponsorship"}),
        ("Sponsorship for this team is unavailable.", {"no_sponsorship"}),
        # E4
        ("Candidates must be able to obtain and maintain a DoD Secret.", {"clearance_required"}),
        ("This role requires Secret eligibility.", {"clearance_required"}),
        ("Applicants must be U.S.-born.", {"citizenship_required"}),
        ("Without regard to race, color, or sex we hire U.S. citizens exclusively.", {"citizenship_required"}),
        ("This position is only open to people who will not need us to file paperwork with USCIS.", {FALLBACK}),
        ("Candidates must be able to work for us without us filing any petitions.", {FALLBACK}),
        ("Only candidates who are local and US work eligible.", {FALLBACK}),
        ("Foreign-born applicants will not be considered.", {FALLBACK}),
    ],
)
def test_round3_requirements_are_flagged(profile, sentence, codes):
    # one line each: some of these are headings without a full stop
    description = f"We build analytics products for retailers.\n{sentence}\nApply on our careers page."
    flags = detect_eligibility_flags({"description_text": description, "title": "Data Analyst"}, profile)
    assert needs_review(flags), f"{sentence!r} is not sent to review: {flags}"
    by_code = {f.code: f for f in flags}
    assert codes <= set(by_code), f"{codes - set(by_code)} not detected in {sentence!r}: {flags}"
    for code in codes:
        flag = by_code[code]
        assert flag.severity == SEVERITY.get(code, "review")
        assert flag.excerpt == sentence
        assert "legal determination" in flag.message
        assert not _CLAIMS.search(flag.message), flag.message


@pytest.mark.parametrize(
    "sentence",
    [
        # the boilerplate exemptions above must not hide a requirement next to them
        "We will never ask for your passport, but you must hold a passport from a NATO country.",
        "Must have a valid passport issued by a NATO country and be able to travel.",
        "Candidates must have the right to work in the UK without sponsorship.",
        "Candidates must have the full right to work in the UK.",
        "Candidates must have the existing right to work in the UK.",
        "You must have the right to work in the UK; we are unable to sponsor visas.",
        "Employment is contingent upon verification of your identity and eligibility to work, and you must not "
        "require a visa.",
        "Offers are contingent on a background check and drug screen clearance and an active Secret clearance.",
        "Drug screen clearance and a Top Secret clearance are required.",
        "Exposure to trademark matters is a plus; candidates must hold a security clearance.",
        "Experience recruiting is a plus; candidates must hold work authorization that is not tied to a visa.",
        "Experience coordinating immigration cases; candidates must not need a visa.",
        "We offer visa and relocation support only for senior roles.",
        "Benefits include immigration support, except for candidates on OPT.",
        "Our team represents more than 40 nationalities; applicants must be EU nationals.",
        "Sponsorship requests will not be considered.",
        "We cannot accommodate sponsorship requests.",
        "Executive sponsorship for this program is unavailable, and so is visa sponsorship.",
        "Minimum Clearance Required: Top Secret with polygraph.",
        "Applicants must be able to obtain clearance; ground clearance is irrelevant.",
        "Must hold an active security clearance and be able to climb 20 ft ladders.",
        "Patent attorneys with clearance preferred.",
        "Must hold a valid passport that allows travel to Israel.",
        "Own work authorization.",
        "Our team has 40 nationalities, but applicants must be US citizens.",
        "Every child born in the U.S. is a citizen, and candidates must be U.S. citizens.",
    ],
)
def test_round3_exemptions_cannot_hide_a_requirement(profile, sentence):
    flags = detect_eligibility_flags({"description_text": sentence, "title": "Data Analyst"}, profile)
    assert needs_review(flags), f"{sentence!r} is not sent to review: {flags}"
