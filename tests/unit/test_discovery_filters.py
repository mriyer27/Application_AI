"""Search filters: title prefilter, word-boundary exclusions, locations ("US" vs "Austin"), etc."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from job_agent.config import SearchSection
from job_agent.discovery.filters import evaluate_filters, excluded_title_keyword, title_is_relevant
from job_agent.models import EmploymentType, NormalizedJob, WorkArrangement

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.fixture
def search(config) -> SearchSection:
    return config.search  # from config/config.example.yaml


def job(**kw) -> NormalizedJob:
    base = dict(source_name="fixture", source_identifier="t", external_id="1", employer="Acme", title="Data Analyst",
                location="New York, NY", employment_type=EmploymentType.FULL_TIME)
    base.update(kw)
    return NormalizedJob(**base)


@pytest.mark.parametrize("title,relevant", [
    ("Business Analyst", True),
    ("Associate Business Analyst", True),
    ("Analyst, Business Intelligence", True),  # target words in any order
    ("Junior Data Analysts", True),  # keyword plural
    ("Manager, Product Marketing", True),  # "product" keyword
    ("Strategy & Operations Associate", True),
    ("Software Engineer", False),
    ("Account Executive", False),
    ("Production Supervisor", False),  # "product" must not match "production"
    ("", False),
])
def test_title_prefilter(search, title, relevant):
    assert title_is_relevant(title, search) is relevant


@pytest.mark.parametrize("title,excluded", [
    ("Senior Data Analyst", "senior"),
    ("Sr. Business Analyst", "sr"),
    ("Data Analyst (SR)", "sr"),
    ("Team Lead, Analytics", "lead"),
    ("Lead Product Analyst", "lead"),
    ("Head of Strategy", "head of"),
    ("VP, Strategy", "vp"),
    ("Leadership Development Analyst", None),  # "lead" does not match "leadership"
    ("Srinagar Operations Analyst", None),  # "sr" does not match inside a word
    ("Staffing Analyst", None),  # "staff" does not match "staffing"
    ("Business Analyst", None),
])
def test_exclusions_use_word_boundaries(search, title, excluded):
    assert excluded_title_keyword(title, search) == excluded


@pytest.mark.parametrize("location,passes", [
    ("US - New York", True),
    ("Austin, TX", True),  # a US city/state (recognized as US, not via the "us" substring)
    ("Houston, TX, USA", True),
    ("Remote (US)", True),
    ("Albany, NY", True),
    ("London, United Kingdom", False),
    ("Sunnyvale, CA", True),  # US city/state; "NY" substring is irrelevant
    ("Bengaluru, India", False),
    ("Toronto, ON, Canada", False),
    (None, True),  # unknown location is not a reason to drop
])
def test_location_filter(search, location, passes):
    decision = evaluate_filters(job(location=location), search, now=NOW)
    assert decision.passed is passes, decision.reason
    if not passes:
        assert decision.codes == ["location"]


def test_remote_arrangement_matches_remote_location_entry(search):
    remote = job(location="Anywhere", work_arrangement=WorkArrangement.REMOTE)
    assert evaluate_filters(remote, search, now=NOW).passed


def test_remote_preference(search):
    remote_only = search.model_copy(update={"remote_preference": "remote_only"})
    hybrid_ok = search.model_copy(update={"remote_preference": "remote_or_hybrid"})
    onsite = job(work_arrangement=WorkArrangement.ONSITE)
    hybrid = job(work_arrangement=WorkArrangement.HYBRID)
    remote = job(location="Remote (US)", work_arrangement=WorkArrangement.REMOTE)
    unknown = job()
    assert not evaluate_filters(onsite, remote_only, now=NOW).passed
    assert not evaluate_filters(unknown, remote_only, now=NOW).passed
    assert evaluate_filters(remote, remote_only, now=NOW).passed
    assert evaluate_filters(hybrid, hybrid_ok, now=NOW).passed
    assert not evaluate_filters(onsite, hybrid_ok, now=NOW).passed
    assert evaluate_filters(onsite, search, now=NOW).passed  # "any"


def test_employment_type_filter(search):
    decision = evaluate_filters(job(employment_type=EmploymentType.INTERNSHIP), search, now=NOW)
    assert decision.codes == ["employment_type"]
    assert evaluate_filters(job(employment_type=EmploymentType.UNKNOWN), search, now=NOW).passed
    with_interns = search.model_copy(update={"employment_types": [*search.employment_types,
                                                                  EmploymentType.INTERNSHIP]})
    assert evaluate_filters(job(employment_type=EmploymentType.INTERNSHIP), with_interns, now=NOW).passed


def test_years_only_when_explicit(search):
    assert evaluate_filters(job(raw_metadata={"years_experience_min": 5}), search, now=NOW).codes == \
        ["experience_years"]
    assert evaluate_filters(job(raw_metadata={"years_experience_min": 3}), search, now=NOW).passed
    assert evaluate_filters(job(), search, now=NOW).passed  # not stated -> not filtered


def test_posting_age_and_closing_date(search):
    old = job(posted_at="2026-08-01T00:00:00+00:00")
    assert evaluate_filters(old, search, now=NOW).codes == ["posting_age"]
    fresh = job(posted_at="2026-09-25T00:00:00+00:00")
    assert evaluate_filters(fresh, search, now=NOW).passed
    closed = job(closing_at="2026-09-30T00:00:00+00:00")
    assert evaluate_filters(closed, search, now=NOW).codes == ["closing_date_passed"]
    assert evaluate_filters(job(closing_at="2026-12-31T00:00:00+00:00"), search, now=NOW).passed


def test_multiple_reasons_are_all_reported(search):
    bad = job(title="Senior Data Analyst", location="London, United Kingdom", employment_type=EmploymentType.CONTRACT)
    decision = evaluate_filters(bad, search, now=NOW)
    assert decision.codes == ["excluded_title_keyword", "location", "employment_type"]
    assert "senior" in decision.reason and "London" in decision.reason


def test_location_phrases_use_word_boundaries():
    from job_agent.discovery.normalize import contains_phrase, normalize_key

    assert not contains_phrase(normalize_key("Austin"), "US")  # CRITICAL: no substring matches
    assert not contains_phrase(normalize_key("Sunnyvale"), "NY")
    assert contains_phrase(normalize_key("US - New York"), "US")


# --- US recognition for city/state-only locations (integration finding) ---- #
@pytest.mark.parametrize("location, expected", [
    ("Bellevue, Washington; Mountain View, California; San Francisco", True),
    ("San Francisco, CA", True),
    ("New York, NY 10001", True),
    ("Austin, TX", True),
    ("Washington, DC", True),
    ("Chicago, Illinois, United States", True),
    ("London, UK; New York, NY", True),
    ("Wilmington, DE 19801", True),
    ("Indianapolis, Indiana", True),
    ("UK - London", False),
    ("Poland - Warsaw", False),
    ("Berlin, DE", False),
    ("Chennai, IN", False),
    ("Bengaluru, IN, India", False),
    ("Toronto, ON, Canada", False),
    ("Remote (Canada)", False),
    ("Dublin, Ireland", False),
    ("Hong Kong", False),
    ("Paris", False),
])
def test_is_us_location(location, expected):
    from job_agent.discovery.filters import is_us_location

    assert is_us_location(location) is expected


def test_city_state_location_matches_when_targeting_us(tmp_path):
    from job_agent.config import SearchSection
    from job_agent.discovery.filters import evaluate_filters
    from job_agent.models import NormalizedJob

    search = SearchSection(target_titles=["Data Analyst"], locations=["United States"])
    job = NormalizedJob(source_name="greenhouse", source_identifier="x", external_id="1", employer="X",
                        title="Data Analyst", location="Bellevue, Washington; Mountain View, California")
    assert evaluate_filters(job, search).passed
    other = SearchSection(target_titles=["Data Analyst"], locations=["New York"])
    assert not evaluate_filters(job, other).passed  # US recognition only when targeting the US


@pytest.mark.parametrize("location, arrangement, passes", [
    ("Remote - USA", WorkArrangement.REMOTE, True),
    ("United States - Remote", WorkArrangement.REMOTE, True),
    ("Remote", WorkArrangement.REMOTE, True),
    ("Anywhere", WorkArrangement.REMOTE, True),
    ("Remote - UK", WorkArrangement.REMOTE, False),
    ("Remote Canada", WorkArrangement.REMOTE, False),
    ("Canada - Remote (ON, AB, BC, or NS Only)", WorkArrangement.REMOTE, False),
    ("Remote - US or Canada", WorkArrangement.REMOTE, True),
])
def test_remote_roles_restricted_to_other_countries_do_not_match_us_search(location, arrangement, passes):
    from job_agent.config import SearchSection
    from job_agent.models import NormalizedJob

    search = SearchSection(target_titles=["Data Analyst"], locations=["United States", "Remote", "New York"])
    j = NormalizedJob(source_name="greenhouse", source_identifier="x", external_id="1", employer="X",
                      title="Data Analyst", location=location, work_arrangement=arrangement)
    assert evaluate_filters(j, search).passed is passes


def test_explicitly_listed_foreign_location_still_matches():
    from job_agent.config import SearchSection
    from job_agent.models import NormalizedJob

    search = SearchSection(target_titles=["Data Analyst"], locations=["United States", "Remote", "Canada"])
    j = NormalizedJob(source_name="greenhouse", source_identifier="x", external_id="1", employer="X",
                      title="Data Analyst", location="Remote Canada", work_arrangement=WorkArrangement.REMOTE)
    assert evaluate_filters(j, search).passed
