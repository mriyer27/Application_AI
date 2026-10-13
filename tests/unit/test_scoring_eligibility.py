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
