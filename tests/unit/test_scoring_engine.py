from __future__ import annotations

import json

import pytest

from job_agent.models import CodingIntensity, JobAnalysis, MatchCategory
from job_agent.scoring.engine import SCORING_VERSION, salary_display, score_job, scoring_config_hash

# 410 characters, no eligibility statements, one growth signal ("mentorship"), no extra years / skills.
DESCRIPTION = (
    "Acme Analytics is hiring a Business Analyst to join our insights team in New York. "
    "You will partner with finance and operations leaders to analyze trends, build clear reports, "
    "and recommend improvements. You will receive mentorship from experienced colleagues. "
    "We value curiosity, clear writing, and ownership of outcomes. This is a full-time position "
    "based in our New York office with a collaborative culture."
)
REQUIRED = ["Proficiency in SQL and Python", "Experience with Tableau", "Working knowledge of Java",
            "0-2 years of experience in an analytical role"]


def crafted(**overrides):
    job = {
        "id": 7,
        "job_uid": "J-crafted001",
        "title": "Business Analyst",
        "employer": "Acme Analytics",
        "location": "New York, NY",
        "work_arrangement": "onsite",
        "employment_type": "full_time",
        "description_text": DESCRIPTION,
        "required_qualifications": json.dumps(REQUIRED),
        "preferred_qualifications": "[]",
        "salary_min": 95000.0,
        "salary_max": 120000.0,
        "salary_currency": "USD",
        "salary_period": "year",
        "salary_is_base": 1,
        "content_hash": "c-crafted",
    }
    job.update(overrides)
    return job


def factor(result, name):
    return next(f for f in result.factors if f.name == name)


def with_scoring(config, **changes):
    return config.model_copy(update={"scoring": config.scoring.model_copy(update=changes)})


def with_weights(config, **weights):
    return with_scoring(config, weights=config.scoring.weights.model_copy(update=weights))


def analysis(**overrides) -> JobAnalysis:
    values = dict(summary="Analytics role on an insights team.", role_family="business analysis", seniority="entry",
                  required_skills=["Snowflake"], coding_intensity=CodingIntensity.HIGH, ai_relevance=0.2,
                  business_relevance=0.6, analytics_relevance=1.0, product_relevance=0.0,
                  fit_explanation="Strong analytics overlap with the verified SQL and reporting work.",
                  model="qwen2.5:7b-instruct", prompt_version="test-1")
    values.update(overrides)
    return JobAnalysis(**values)


# --------------------------------------------------------------------------- #
def test_exact_score_for_crafted_job(config, profile):
    result = score_job(crafted(), profile, config)
    assert {f.name: f.score for f in result.factors} == {
        "career_interest": 1.0,  # exact target title
        "skills": 0.75,  # SQL, Python, Tableau matched; Java missing
        "experience_level": 1.0,  # "0-2 years"
        "domain_relevance": 1.0,  # business + analytics in the title
        "location": 1.0,  # "New York" is a configured location
        "compensation": 1.0,  # $95k-$120k >= $90k target
        "employer_growth": 0.65,  # 0.5 + mentorship 0.15
    }
    assert {f.name: f.weight for f in result.factors} == {
        "career_interest": 25.0, "skills": 25.0, "experience_level": 15.0, "domain_relevance": 10.0,
        "location": 10.0, "compensation": 10.0, "employer_growth": 5.0,
    }
    # 25*1 + 25*0.75 + 15*1 + 10*1 + 10*1 + 10*1 + 5*0.65 = 92.0
    assert result.overall_score == 92.0
    assert result.category == MatchCategory.STRONG and result.shortlisted
    assert result.job_id == 7
    assert result.scoring_version == SCORING_VERSION and not result.used_llm and result.llm_model is None
    assert result.recommended_next_action == "Prepare tailored documents"
    assert result.eligibility_flags == [] and result.potential_disqualifiers == []
    assert "Java (required; no evidence in verified profile)" in result.missing_qualifications
    assert [m.split(" (")[0] for m in result.matching_qualifications] == ["SQL", "Python", "Tableau"]
    assert all(f.explanation for f in result.factors)


def test_configurable_weights_change_the_result(config, profile):
    only_skills = with_weights(config, career_interest=0, skills=1, experience_level=0, domain_relevance=0,
                               location=0, compensation=0, employer_growth=0)
    result = score_job(crafted(), profile, only_skills)
    assert result.overall_score == 75.0
    assert factor(result, "skills").weight == 100.0

    no_money = with_weights(config, compensation=0, employer_growth=0)  # remaining weights sum to 85
    result = score_job(crafted(), profile, no_money)
    assert result.overall_score == round((25 + 25 * 0.75 + 15 + 10 + 10) * 100 / 85, 1) == 92.6


def test_shortlist_and_category_thresholds(config, profile):
    assert not score_job(crafted(), profile, with_scoring(config, shortlist_threshold=92.1)).shortlisted
    assert score_job(crafted(), profile, with_scoring(config, shortlist_threshold=92.0)).shortlisted
    possible = score_job(crafted(), profile, with_scoring(config, strong_threshold=95, possible_threshold=60))
    assert possible.category == MatchCategory.POSSIBLE
    weak = score_job(crafted(), profile, with_scoring(config, strong_threshold=99, possible_threshold=95))
    assert weak.category == MatchCategory.WEAK


def test_missing_information_is_neutral_and_needs_review(config, profile):
    job = {"id": 3, "title": "Business Analyst", "employer": "Unknown Co", "description_text": "Join us.",
           "required_qualifications": "[]", "preferred_qualifications": "[]"}
    result = score_job(job, profile, config)
    skills = factor(result, "skills")
    assert skills.score == 0.5 and "Insufficient information" in skills.explanation
    assert factor(result, "location").score == 0.5 and "not listed" in factor(result, "location").explanation
    comp = factor(result, "compensation")
    assert comp.score == 0.5 and "Salary not listed" in comp.explanation
    assert factor(result, "experience_level").score == 0.6
    assert "no explicit experience requirement" in factor(result, "experience_level").explanation.lower()
    assert factor(result, "employer_growth").score == 0.5
    assert result.category == MatchCategory.NEEDS_REVIEW
    assert any("Short description" in r for r in result.reasons_not_to_apply)
    assert result.recommended_next_action.startswith("Open the listing")


def test_scoring_is_repeatable(config, profile, db, make_job):
    first = score_job(crafted(), profile, config)
    second = score_job(crafted(), profile, config)
    assert first.model_dump_json() == second.model_dump_json()
    job_id = make_job(description_text=DESCRIPTION, required_qualifications=json.dumps(REQUIRED))
    row = db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    assert score_job(row, profile, config).model_dump_json() == score_job(dict(row), profile, config).model_dump_json()
    assert scoring_config_hash(config) == scoring_config_hash(config)


@pytest.mark.parametrize(
    "phrase,expected",
    [("0-2 years of experience", 1.0), ("1+ year of experience", 0.95), ("2+ years of experience", 0.8),
     ("3+ years of relevant experience", 0.55), ("4 years of professional experience", 0.3),
     ("5+ years of experience", 0.1), ("Minimum of 7 years in analytics", 0.1),
     ("two (2) years of experience with SQL", 0.8)],
)
def test_experience_years_mapping(config, profile, phrase, expected):
    result = score_job(crafted(required_qualifications=json.dumps(["SQL", phrase])), profile, config)
    assert factor(result, "experience_level").score == expected


def test_five_plus_years_is_a_potential_disqualifier(config, profile):
    result = score_job(crafted(required_qualifications=json.dumps(["SQL", "5+ years of experience"])), profile,
                       config)
    assert "Requires 5+ years of experience" in result.potential_disqualifiers
    assert any(m.startswith("5+ years of professional experience") for m in result.missing_qualifications)
    assert result.recommended_next_action == "Skip - requires 5+ years"


def test_alternative_years_in_one_sentence_take_the_lower(config, profile):
    quals = ["4 years of experience, or 2 years of experience with a Master's degree"]
    assert factor(score_job(crafted(required_qualifications=json.dumps(quals)), profile, config),
                  "experience_level").score == 0.8  # 2 years (the lower alternative)


def test_title_seniority_and_entry_signals(config, profile):
    no_years = json.dumps(["SQL", "Python"])
    senior = score_job(crafted(title="Senior Business Analyst", required_qualifications=no_years), profile, config)
    assert factor(senior, "experience_level").score == 0.15
    assert factor(senior, "career_interest").score == 1.0  # seniority prefixes do not hide the target title
    assert senior.recommended_next_action == "Skip - senior-level role"
    entry = score_job(crafted(title="Business Analyst (New Grad)", required_qualifications=no_years), profile, config)
    assert factor(entry, "experience_level").score == 1.0
    assoc = score_job(crafted(title="Associate Data Analyst", required_qualifications=no_years), profile, config)
    assert factor(assoc, "experience_level").score == 0.9
    unknown = score_job(crafted(required_qualifications=no_years), profile, config)
    assert factor(unknown, "experience_level").score == 0.6


def test_degree_requirements(config, profile):
    phd = score_job(crafted(required_qualifications=json.dumps(["PhD in Statistics", "SQL"])), profile, config)
    assert "PhD / doctorate (not in verified profile)" in phd.missing_qualifications
    assert "Requires a PhD / doctorate" in phd.potential_disqualifiers
    assert factor(phd, "experience_level").score <= 0.2

    bachelor = score_job(crafted(required_qualifications=json.dumps(["Bachelor's degree in Computer Science"])),
                         profile, config)
    assert any(m.startswith("Bachelor's degree (profile:") for m in bachelor.matching_qualifications)
    assert {"requirement": "Bachelor's degree", "fact_id": "edu_bs"}.items() <= next(
        e for e in bachelor.evidence if e["requirement"] == "Bachelor's degree").items()

    either = score_job(crafted(required_qualifications=json.dumps(["BS/MS in Statistics or a related field"])),
                       profile, config)
    assert any(m.startswith("Bachelor's degree") for m in either.matching_qualifications)


def test_preferred_skills_count_less(config, profile):
    result = score_job(crafted(preferred_qualifications=json.dumps(["Snowflake", "dbt"])), profile, config)
    skills = factor(result, "skills")
    assert skills.score == round(3 / (4 + 2 * 0.4), 3)
    assert "Snowflake (preferred; no evidence in verified profile)" in result.missing_qualifications


def test_skills_fall_back_to_description(config, profile):
    job = crafted(required_qualifications="[]",
                  description_text=DESCRIPTION + " Daily work uses Excel, Power BI and Snowflake.")
    skills = factor(score_job(job, profile, config), "skills")
    assert "the description" in skills.explanation
    assert skills.score == round(2 / 3, 3)


def test_r_false_positives_do_not_count(config, profile):
    job = crafted(required_qualifications=json.dumps(["Experience with Rust and React", "An R&D mindset"]))
    result = score_job(job, profile, config)
    assert not any(m.startswith("R ") for m in result.matching_qualifications)
    assert factor(result, "skills").score == 0.0
    assert {m.split(" (")[0] for m in result.missing_qualifications} >= {"Rust", "React"}
    with_r = score_job(crafted(required_qualifications=json.dumps(["Statistics in R/Python"])), profile, config)
    assert {m.split(" (")[0] for m in with_r.matching_qualifications} >= {"R", "Python", "Statistics"}


def test_evidence_comes_only_from_the_profile(config, profile):
    quals = ["SQL, Python, SAS, statistics", "AWS and Kubernetes", "Machine learning"]
    result = score_job(crafted(required_qualifications=json.dumps(quals)), profile, config)
    facts = {f.id: f.text for f in profile.all_facts()}
    assert result.evidence
    for item in result.evidence:
        assert set(item) == {"requirement", "fact_id", "fact_text"}
        assert facts[item["fact_id"]] == item["fact_text"]
    matched = {m.split(" (")[0] for m in result.matching_qualifications}
    assert matched == {"SQL", "Python", "SAS", "Statistics"}
    assert "SAS (required; via profile skill SAS Enterprise Guide; evidence: agency_regression, " \
           "agency_python_migration)" in result.matching_qualifications
    assert {m.split(" (")[0] for m in result.missing_qualifications} == {"AWS", "Kubernetes", "Machine learning"}


def test_location_word_boundaries_and_remote(config, profile):
    assert factor(score_job(crafted(location="Austin, TX"), profile, config), "location").score == 0.2
    remote_us = score_job(crafted(location="Remote - US", work_arrangement="remote"), profile, config)
    assert factor(remote_us, "location").score == 1.0
    remote_ca = score_job(crafted(location="Remote (Canada)", work_arrangement="remote"), profile, config)
    assert factor(remote_ca, "location").score == 0.5
    unknown = score_job(crafted(location=None, work_arrangement="unknown"), profile, config)
    assert factor(unknown, "location").score == 0.5
    remote_only = config.model_copy(update={"search": config.search.model_copy(
        update={"remote_preference": "remote_only"})})
    assert factor(score_job(crafted(), profile, remote_only), "location").score == 0.15


def test_compensation_rules(config, profile):
    hourly = factor(score_job(crafted(salary_min=45.0, salary_max=45.0, salary_period="hour"), profile, config),
                    "compensation")
    assert hourly.score == 1.0 and "estimate" in hourly.explanation and "$93,600" in hourly.explanation
    ote = factor(score_job(crafted(salary_is_base=0), profile, config), "compensation")
    assert ote.score == 0.7 and "total compensation" in ote.explanation and "not base" in ote.explanation
    low = factor(score_job(crafted(salary_min=60000.0, salary_max=72000.0), profile, config), "compensation")
    assert low.score == 0.48  # 72k is 80% of 90k -> 0.8 * (0.8 - 0.5) / 0.5
    reach = factor(score_job(crafted(salary_min=80000.0, salary_max=100000.0), profile, config), "compensation")
    assert reach.score == 0.85
    unspecified = factor(score_job(crafted(salary_is_base=None), profile, config), "compensation")
    assert unspecified.score == 1.0 and "does not say whether this is base" in unspecified.explanation
    euro = factor(score_job(crafted(salary_currency="EUR"), profile, config), "compensation")
    assert euro.score == 0.5
    assert salary_display(crafted(salary_text=None)) == "$95,000-$120,000/year"
    assert salary_display(crafted(salary_text="$95k - $120k base")) == "$95k - $120k base"
    assert salary_display(crafted(salary_min=None, salary_max=None)) == ""


def test_career_interest_penalties_never_zero(config, profile):
    swe = score_job(crafted(title="Software Engineer"), profile, config)
    ci = factor(swe, "career_interest")
    assert 0 < ci.score <= 0.1 and "software-engineering" in ci.explanation
    leetcode = crafted(description_text=DESCRIPTION + " Interviews include LeetCode-style coding challenges.")
    assert factor(score_job(leetcode, profile, config), "career_interest").score == 0.8
    coding = crafted(description_text=DESCRIPTION + " You will write Python and SQL code for analyses.")
    assert factor(score_job(coding, profile, config), "career_interest").score == 1.0


def test_flags_drive_category_and_shortlist(config, profile):
    blocked = score_job(crafted(description_text=DESCRIPTION + " Must be a U.S. citizen."), profile, config)
    assert blocked.overall_score >= 70
    assert blocked.category == MatchCategory.NEEDS_REVIEW and not blocked.shortlisted
    assert blocked.potential_disqualifiers[0] == 'Citizenship requirement stated: "Must be a U.S. citizen."'
    assert blocked.recommended_next_action == "Review eligibility statement before investing time"
    review = score_job(crafted(sponsorship_info="We will not sponsor visas."), profile, config)
    assert review.category == MatchCategory.NEEDS_REVIEW and review.shortlisted  # review flags do not block
    assert review.eligibility_flags[0].excerpt == "We will not sponsor visas."
    info = score_job(crafted(sponsorship_info="Visa sponsorship available."), profile, config)
    assert info.category == MatchCategory.STRONG
    assert any("sponsorship" in r for r in info.reasons_to_apply)


def test_llm_analysis_blending(config, profile):
    job = crafted(description_text=DESCRIPTION + " Our data lives in Snowflake.")
    rules = score_job(job, profile, config)
    result = score_job(job, profile, config, analysis())
    assert result.used_llm and result.llm_model == "qwen2.5:7b-instruct"
    assert result.scoring_version == SCORING_VERSION + "+llm"
    assert factor(result, "career_interest").score == 0.9  # high coding intensity: -0.1
    domain = factor(result, "domain_relevance")
    assert domain.score == 0.9  # 0.5 * rules 1.0 + 0.5 * mean(1.0, 0.6)
    assert "Local AI opinion (unverified; may be wrong): Strong analytics overlap" in domain.explanation
    assert factor(result, "skills").score == 0.6  # Snowflake added as a requirement; still checked vs profile
    assert "Snowflake (required, from local AI analysis; no evidence in verified profile)" in \
        result.missing_qualifications
    assert result.overall_score < rules.overall_score


def test_llm_cannot_claim_a_skill_for_the_candidate(config, profile):
    result = score_job(crafted(), profile, config, analysis(required_skills=["Kubernetes", "Python"]))
    assert any(m.startswith("Kubernetes (required, from local AI analysis; no evidence") for m in
               result.missing_qualifications)
    assert not any(e["requirement"] == "Kubernetes" for e in result.evidence)


def test_llm_seniority_used_only_when_rules_found_nothing(config, profile):
    no_years = json.dumps(["SQL", "Python"])
    senior = analysis(seniority="senior", years_experience_required=None)
    assert factor(score_job(crafted(required_qualifications=no_years), profile, config, senior),
                  "experience_level").score == 0.15
    many = analysis(years_experience_required=8)
    assert factor(score_job(crafted(), profile, config, many), "experience_level").score == 1.0  # rules: 0-2 years


def test_llm_sponsorship_statements_are_checked_by_rules(config, profile):
    result = score_job(crafted(), profile, config, analysis(sponsorship_statements=["We are unable to sponsor visas."]))
    assert [f.code for f in result.eligibility_flags] == ["no_sponsorship"]
