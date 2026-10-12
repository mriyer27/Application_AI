"""Normalization: HTML/text, URLs, keys, salary, qualifications, legal excerpts, adapters."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from job_agent.config import SourcesSection
from job_agent.discovery.http import PoliteHttpClient
from job_agent.discovery.normalize import (
    build_normalized_job,
    canonicalize_url,
    contains_phrase,
    description_similarity,
    detect_employment_type,
    detect_experience_level,
    detect_work_arrangement,
    extract_legal_excerpts,
    extract_qualifications,
    extract_years_experience,
    html_to_text,
    InvalidListing,
    normalize_employer,
    normalize_key,
    to_utc_iso,
)
from job_agent.discovery.salary import salary_from_text
from job_agent.discovery.sources import AshbyAdapter, GreenhouseAdapter, LeverAdapter
from job_agent.models import EmploymentType, SalaryPeriod, WorkArrangement

FX = Path(__file__).resolve().parents[1] / "fixtures" / "discovery"


def load(name: str):
    return json.loads((FX / name).read_text(encoding="utf-8"))


def mock_http(routes: dict[str, object]) -> PoliteHttpClient:
    """PoliteHttpClient over an httpx.MockTransport; routes map URL path -> JSON body or callable."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = routes.get(request.url.path)
        if callable(body):
            return body(request)
        if body is None:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json=body)

    settings = SourcesSection(min_request_interval_seconds=0.2, max_retries=1)
    return PoliteHttpClient(settings, transport=httpx.MockTransport(handler), sleep=lambda s: None)


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #
def test_html_to_text_bullets_entities_and_scripts():
    markup = ("<p><strong>What You&rsquo;ll Need</strong></p><ul><li><p>SQL &amp; Python</p></li>"
              "<li>Excel&nbsp;skills</li></ul><script>alert(1)</script><style>p{}</style>"
              "<p>Second&#8203; paragraph‮</p><p>LI-HYBRID</p>")
    text = html_to_text(markup)
    assert text.splitlines() == ["What You’ll Need", "", "- SQL & Python", "- Excel skills", "", "Second paragraph"]
    assert "alert" not in text and "‮" not in text


def test_normalize_keys():
    assert normalize_key("Sr. Data Analyst, AI/ML (Remote)") == "sr data analyst ai ml remote"
    assert normalize_key("Ph.D. Résumé") == "phd resume"
    assert normalize_employer("The Example Company, Inc.") == "example company"
    assert normalize_employer("Acme Analytics LLC") == normalize_employer("ACME Analytics") == "acme analytics"
    assert contains_phrase(normalize_key("US - New York"), "US")
    assert not contains_phrase(normalize_key("Austin, TX"), "US")


def test_canonicalize_url():
    assert canonicalize_url("HTTPS://Careers.Example.com:443/Jobs/?gh_jid=11&gh_src=x&utm_medium=y#apply") == \
        "https://careers.example.com/Jobs?gh_jid=11"
    assert canonicalize_url("https://jobs.lever.co/acme/abc/?lever-source=LinkedIn&lever-origin=applied") == \
        "https://jobs.lever.co/acme/abc"
    assert canonicalize_url("https://x.example/jobs?id=2&ref=feed&b=1") == "https://x.example/jobs?b=1&id=2"
    assert canonicalize_url("mailto:jobs@example.com") is None
    assert canonicalize_url(None) is None


def test_to_utc_iso():
    assert to_utc_iso("2026-09-29T05:15:51-04:00") == "2026-09-29T09:15:51+00:00"
    assert to_utc_iso(1786469891368) == "2026-08-11T17:38:11+00:00"
    assert to_utc_iso("2026-04-07T17:12:35.753+00:00") == "2026-04-07T17:12:35+00:00"
    assert to_utc_iso("2026-10-31") == "2026-10-31T00:00:00+00:00"
    assert to_utc_iso("not a date") is None and to_utc_iso(None) is None


def test_description_similarity():
    a = "Analyze data with SQL and Python for the retail team every day"
    assert description_similarity(a, a + ".") == 1.0
    assert description_similarity(a, "Completely different text about painting walls") < 0.2
    assert description_similarity("", a) == 0.0


# --------------------------------------------------------------------------- #
# Arrangement / employment / experience
# --------------------------------------------------------------------------- #
def test_work_arrangement_only_from_explicit_signals():
    assert detect_work_arrangement(workplace_type="OnSite") == WorkArrangement.ONSITE
    assert detect_work_arrangement(workplace_type="hybrid", is_remote=True) == WorkArrangement.HYBRID
    assert detect_work_arrangement(is_remote=True) == WorkArrangement.REMOTE
    assert detect_work_arrangement(locations=["Remote - US"]) == WorkArrangement.REMOTE
    assert detect_work_arrangement(locations=["New York (Hybrid)"]) == WorkArrangement.HYBRID
    assert detect_work_arrangement(workplace_type="unspecified", locations=["New York, NY"]) == WorkArrangement.UNKNOWN
    # A description saying "remote" is not a signal.
    job = build_normalized_job(source_name="fixture", source_identifier="t", external_id="1", employer="E",
                               title="Data Analyst", location_parts=["Boston, MA"],
                               description_plain="Some remote work possible.")
    assert job.work_arrangement == WorkArrangement.UNKNOWN


def test_employment_type():
    assert detect_employment_type(structured=["Full-time"], title="Data Analyst") == EmploymentType.FULL_TIME
    assert detect_employment_type(structured=["FullTime"], title="Analyst") == EmploymentType.FULL_TIME
    assert detect_employment_type(structured=["Fixed-Term"], title="Analyst") == EmploymentType.TEMPORARY
    assert detect_employment_type(structured=["Full-time"], title="Data Analyst Intern") == EmploymentType.INTERNSHIP
    assert detect_employment_type(title="Summer Internship Program") == EmploymentType.INTERNSHIP
    assert detect_employment_type(title="Internal Audit Analyst") == EmploymentType.UNKNOWN


def test_experience_years_and_level():
    years, snippet = extract_years_experience(["10+ years of experience in architecture", "2-4 years of SQL experience"])
    assert years == 2 and "2-4 years" in snippet
    assert extract_years_experience(["At least three years of previous support experience"])[0] == 3
    assert extract_years_experience(["Founded 25 years ago", "We have 5 offices"])[0] is None
    assert detect_experience_level("Senior Associate", None) == "senior"
    assert detect_experience_level("Associate Product Manager", None) == "associate"
    assert detect_experience_level("Data Analyst, New Grad 2027", None) == "new_grad"
    assert detect_experience_level("Junior Analyst", None) == "entry"
    assert detect_experience_level("Data Analyst", 1) == "entry"
    assert detect_experience_level("Data Analyst", None) is None


# --------------------------------------------------------------------------- #
# Salary
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,expected", [
    ("The base salary range for this role is $90,000 - $120,000.",
     (90000, 120000, "USD", SalaryPeriod.YEAR, True)),
    ("OTE: $110,000 to $150,000 per year, including commission.",
     (110000, 150000, "USD", SalaryPeriod.YEAR, False)),
    ("This role pays $45/hour.", (45, 45, "USD", SalaryPeriod.HOUR, None)),
    ("The pay for this role is $45.50 - $55 per hour.", (45.5, 55, "USD", SalaryPeriod.HOUR, None)),
    ("Canada Pay Transparency\n$92,000—$118,000 CAD", (92000, 118000, "CAD", SalaryPeriod.YEAR, None)),
    ("The minimum gross annual base salary for this position is from €29.000.",
     (29000, None, "EUR", SalaryPeriod.YEAR, True)),
    ("Compensation: £39K – £43K per annum", (39000, 43000, "GBP", SalaryPeriod.YEAR, None)),
])
def test_salary_from_text(text, expected):
    info = salary_from_text(text)
    assert info is not None
    assert (info.min, info.max, info.currency, info.period, info.is_base) == expected
    assert info.text and info.source == "text"


@pytest.mark.parametrize("text", [
    "Fertility HRA (up to $10,000 per year)",
    "We automate how over $200B in annualized spend flows through 70,000+ companies.",
    "401(k) match up to $5,000",
    "3-5 years of experience",
    "Learning stipend of $1,500 per year.",
    "We raised $50M from investors.",
    "",
])
def test_salary_never_invented(text):
    assert salary_from_text(text) is None


def test_salary_without_stated_period_is_marked_inferred():
    info = salary_from_text("US Pay Range\n$146,000—$183,000 USD")
    assert info.period == SalaryPeriod.YEAR and info.period_inferred and info.is_base is None
    assert info.text == "US Pay Range $146,000—$183,000 USD"


# --------------------------------------------------------------------------- #
# Qualifications / legal excerpts
# --------------------------------------------------------------------------- #
def test_extract_qualifications():
    text = ("About Us\n\nWe do things.\n\nWHAT YOU NEED\n\n- SQL\n- 2+ years of experience\nTechnical skills:\n"
            "- Python\n\nNice to Haves\n\n- Tableau\n\nBenefits\n\n- Free lunch\n\nPreferred Qualifications:\n- dbt")
    required, preferred = extract_qualifications(text)
    assert required == ["SQL", "2+ years of experience", "Python"]
    assert preferred == ["Tableau", "dbt"]
    assert extract_qualifications("Just a paragraph.\n- a bullet without heading") == ([], [])


def test_legal_excerpts_are_verbatim_and_not_inferred():
    text = ("Work with executive sponsors to scope analysis.\n- Must be authorized to work in the United States. "
            "We are unable to sponsor visas for this role.\nComplete the citizen development SDLC.\n"
            "U.S. citizenship required (program operates in classified environments).\n"
            "This role requires an active security clearance.")
    sponsorship, work_auth = extract_legal_excerpts(text)
    assert sponsorship == "We are unable to sponsor visas for this role."
    assert work_auth.splitlines() == [
        "Must be authorized to work in the United States.",
        "U.S. citizenship required (program operates in classified environments).",
        "This role requires an active security clearance.",
    ]
    for line in work_auth.splitlines() + [sponsorship]:
        assert line in text
    assert extract_legal_excerpts("We value diverse teams.") == (None, None)


def test_build_requires_id_and_title():
    with pytest.raises(InvalidListing):
        build_normalized_job(source_name="x", source_identifier="y", external_id=None, employer="E", title="T")
    with pytest.raises(InvalidListing):
        build_normalized_job(source_name="x", source_identifier="y", external_id="1", employer="E", title="  ")


# --------------------------------------------------------------------------- #
# Adapters (fixtures shaped like real responses)
# --------------------------------------------------------------------------- #
def test_greenhouse_adapter_normalization():
    http = mock_http({"/v1/boards/example": load("greenhouse_board.json"),
                      "/v1/boards/example/jobs": load("greenhouse_jobs.json")})
    result = GreenhouseAdapter(http).fetch("example")
    assert result.complete and result.employer == "Example Consulting"
    assert result.invalid == 2 and len(result) == 10
    assert any("missing title" in r for r in result.invalid_reasons)
    assert "1005" in result.seen_external_ids  # invalid listing's id still counts as "returned"
    job = next(j for j in result if j.external_id == "1001")
    assert job.employer == "Example Consulting" and job.requisition_id == "R-100"
    assert job.location == "US - New York" and job.department == "Business Consulting"
    assert job.posted_at == "2026-09-20T14:00:00+00:00"
    assert job.required_qualifications[:2] == ["Bachelor’s degree in Business, Finance or Information Systems",
                                               "2+ years of experience in business analysis"]
    assert job.preferred_qualifications == ["Experience with Power BI", "Energy industry exposure"]
    assert job.sponsorship_info == "We are unable to sponsor visas for this role."
    assert job.work_authorization_requirements == "Must be authorized to work in the United States."
    assert (job.salary_min, job.salary_max, job.salary_currency) == (85000, 105000, "USD")
    assert job.salary_is_base is None and job.raw_metadata["salary_period_inferred"] is True
    assert job.raw_metadata["years_experience_min"] == 2
    assert "<" not in job.description_text and "alert" not in job.description_text
    assert job.work_arrangement == WorkArrangement.UNKNOWN  # hidden "LI-HYBRID" tag is not a signal
    custom = next(j for j in result if j.external_id == "1011")
    assert custom.closing_at == "2026-09-01T00:00:00+00:00"
    assert canonicalize_url(custom.job_url) == "https://careers.example.com/open-roles?gh_jid=1011"


def test_lever_adapter_paginates_with_skip_and_limit():
    pages = {0: load("lever_page1.json"), 2: load("lever_page2.json")}
    calls = []

    def postings(request: httpx.Request) -> httpx.Response:
        skip, limit = int(request.url.params["skip"]), int(request.url.params["limit"])
        calls.append((skip, limit, request.url.params["mode"]))
        return httpx.Response(200, json=pages.get(skip, []))

    adapter = LeverAdapter(mock_http({"/v0/postings/acme": postings}), max_pages=5, page_size=2)
    result = adapter.fetch("acme")
    assert calls == [(0, 2, "json"), (2, 2, "json")]
    assert result.complete and result.pages == 2 and len(result) == 3
    first = result.jobs[0]
    assert first.employer == "Acme" and first.work_arrangement == WorkArrangement.HYBRID
    assert first.employment_type == EmploymentType.FULL_TIME and first.department == "Analytics"
    assert (first.salary_min, first.salary_max, first.salary_period, first.salary_is_base) == \
        (90000, 120000, SalaryPeriod.YEAR, True)
    assert first.required_qualifications == ["SQL and Python", "1+ years of experience with dashboards"]
    assert first.preferred_qualifications == ["Tableau"]
    assert first.sponsorship_info == "We are unable to sponsor H-1B visas for this role."
    assert first.application_url.endswith("/apply") and first.posted_at == "2026-09-21T14:13:20+00:00"
    second = result.jobs[1]
    assert second.salary_text.endswith("$70,000 - $95,000/year.") and second.salary_is_base is None
    assert second.work_authorization_requirements == "U.S. citizenship required."
    third = result.jobs[2]
    assert third.salary_period == SalaryPeriod.HOUR and third.work_arrangement == WorkArrangement.REMOTE


def test_lever_pagination_cap_marks_fetch_incomplete():
    full_page = load("lever_page1.json")
    adapter = LeverAdapter(mock_http({"/v0/postings/acme": full_page}), max_pages=1, page_size=2)
    result = adapter.fetch("acme")
    assert not result.complete and result.pages == 1 and "max_pages" in result.warnings[0]


def test_ashby_adapter_normalization():
    result = AshbyAdapter(mock_http({"/posting-api/job-board/examplefin": load("ashby_board.json")})).fetch(
        "examplefin")
    assert result.employer == "Examplefin" and len(result) == 3  # unlisted job ignored
    ai = next(j for j in result if j.title == "AI Analyst")
    assert ai.location == "New York, NY (HQ); Remote (US)"
    assert ai.work_arrangement == WorkArrangement.HYBRID  # workplaceType wins over isRemote
    assert ai.employment_type == EmploymentType.FULL_TIME
    assert (ai.salary_min, ai.salary_max, ai.salary_currency, ai.salary_period) == (100000, 130000, "USD",
                                                                                    SalaryPeriod.YEAR)
    assert ai.salary_is_base is None and ai.salary_text == "$100K – $130K • Offers Equity"
    assert ai.required_qualifications == ["Minimum 1 year of experience in analytics", "Python and SQL"]
    assert ai.preferred_qualifications == ["LLM tooling"]
    assert ai.posted_at == "2026-09-25T17:12:35+00:00"
    ops = next(j for j in result if j.title == "Business Operations Analyst")
    assert ops.salary_is_base is False and ops.work_arrangement == WorkArrangement.REMOTE


def test_adapter_rejects_unexpected_shape():
    from job_agent.errors import PermanentError

    with pytest.raises(PermanentError, match="unexpected response shape"):
        AshbyAdapter(mock_http({"/posting-api/job-board/x": ["not", "an", "object"]})).fetch("x")
