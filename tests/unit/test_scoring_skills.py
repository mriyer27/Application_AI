from __future__ import annotations

import pytest

from job_agent.scoring.skills import TAXONOMY, build_profile_index, extract_skills, skill_evidence


@pytest.mark.parametrize(
    "text",
    ["Python, R, SQL", "R programming", "R/Python", "Python/R", "experience with R.", "(R, Python)",
     "statistics in R and SAS", "RStudio"],
)
def test_r_language_standalone_tokens(text):
    assert "R" in extract_skills(text)


@pytest.mark.parametrize(
    "text",
    ["Rust and React", "R&D team", "our R & D group", "HR business partner", "Microsoft Excel(R)", "Excel (R)",
     "R2 storage", "R-squared", "R.N. license", "Ruby on Rails", "Architecture review"],
)
def test_r_false_positives(text):
    assert "R" not in extract_skills(text)


def test_rust_and_react_are_their_own_skills():
    assert extract_skills("Rust and React") == ["Rust", "React"]
    assert extract_skills("We react to customer feedback") == []


@pytest.mark.parametrize("text", ["Power BI", "PowerBI", "Power-BI", "Microsoft Power BI dashboards"])
def test_power_bi_variants(text):
    assert "Power BI" in extract_skills(text)


def test_longest_mention_wins_and_boundaries():
    assert extract_skills("SAS Enterprise Guide") == ["SAS Enterprise Guide"]
    assert extract_skills("SQL Server, MySQL, PostgreSQL and NoSQL") == ["SQL Server", "MySQL", "PostgreSQL"]
    assert extract_skills("Java but not JavaScript") == ["Java", "JavaScript"]
    assert extract_skills("C++ and C#") == ["C++", "C#"]
    assert extract_skills("excel in a fast-paced environment") == []
    assert extract_skills("Advanced Excel and pivot tables") == ["Excel"]
    assert extract_skills("A/B testing and Google Analytics") == ["A/B testing", "Google Analytics"]
    assert extract_skills("HTML") == ["HTML/CSS"]  # "ML" inside HTML is not machine learning


def test_common_requirements_are_covered():
    names = {s.name for s in TAXONOMY}
    for expected in ["Java", "C++", "JavaScript", "React", "AWS", "Azure", "GCP", "Spark", "Snowflake", "dbt",
                     "Looker", "Salesforce", "SAP", "Kubernetes", "Airflow", "Machine learning", "Deep learning",
                     "PyTorch", "TensorFlow", "A/B testing", "Statistics", "Product management", "Agile/Scrum",
                     "Stakeholder management", "Figma", "Alteryx", "Google Analytics"]:
        assert expected in names


def test_every_profile_skill_maps_to_taxonomy(profile):
    index = build_profile_index(profile)
    assert set(index.label_skills) == set(profile.skills)
    assert all(names for names in index.label_skills.values())
    assert not any(s.adhoc for s in index.taxonomy)  # every test-profile skill is in the static taxonomy
    assert index.label_skills["Data analytics and business reporting"] == ("Data analytics", "Business reporting")


def test_profile_skill_evidence_comes_from_facts(profile):
    evidence = skill_evidence(profile)
    assert evidence["Python"] == ["agency_python_migration", "smallco_flask"]
    assert evidence["SAS Enterprise Guide"] == ["agency_regression", "agency_python_migration"]
    assert evidence["IBM Cognos"] == ["agency_dashboards"]
    assert evidence["SQL"] == []  # listed skill without a supporting fact
    fact_ids = {f.id for f in profile.all_facts()}
    assert all(set(ids) <= fact_ids for ids in evidence.values())


def test_index_has_only_profile_backed_skills(profile):
    index = build_profile_index(profile)
    assert index.has("Python") and index.has("SQL") and index.has("R")
    assert index.has("SQL") and index.evidence("SQL").describe() == "profile skill"
    # implication: SAS Enterprise Guide is a kind of SAS
    sas = index.evidence("SAS")
    assert sas.supported and sas.via_labels == ("SAS Enterprise Guide",)
    # keyword-tagged fact evidence (not a listed skill)
    assert index.evidence("Statistics").fact_ids == ("agency_regression",)
    for missing in ("Java", "AWS", "Snowflake", "Kubernetes", "React", "Rust"):
        assert not index.has(missing)
        assert index.evidence(missing).describe() == "no evidence in verified profile"


def test_unknown_profile_skill_becomes_adhoc_entry(profile):
    custom = profile.model_copy(update={"skills": [*profile.skills, "Qlik Sense"]})
    index = build_profile_index(custom)
    assert index.label_skills["Qlik Sense"] == ("Qlik Sense",)
    assert index.extract("Experience with Qlik Sense dashboards") == ["Qlik Sense", "Data visualization"]
    assert index.has("Qlik Sense")
