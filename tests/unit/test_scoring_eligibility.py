from __future__ import annotations

import re

import pytest

from job_agent.scoring.eligibility import detect_eligibility_flags
from job_agent.scoring.text import sentences

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


def test_sentence_splitter_keeps_abbreviations_together():
    parts = sentences("Must be a U.S. citizen. Experience with e.g. SQL is required. Apply today!")
    assert parts == ["Must be a U.S. citizen.", "Experience with e.g. SQL is required.", "Apply today!"]
    assert sentences("• Python\n• SQL; reporting") == ["Python", "SQL; reporting"]
