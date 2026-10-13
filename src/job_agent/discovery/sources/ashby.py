"""Ashby Posting API (public, read-only, meant for career sites).

Docs: https://developers.ashbyhq.com/docs/public-job-posting-api
  GET https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true
      -> {"apiVersion": "1", "jobs": [...]}   (no pagination)
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from job_agent.discovery.http import PoliteHttpClient
from job_agent.discovery.normalize import build_normalized_job, clean_inline
from job_agent.discovery.salary import SalaryInfo, structured_salary
from job_agent.discovery.sources.base import FetchResult
from job_agent.errors import PermanentError
from job_agent.models import NormalizedJob

API_BASE = "https://api.ashbyhq.com/posting-api/job-board"


class AshbyAdapter:
    name = "ashby"
    is_mock = False

    def __init__(self, http: PoliteHttpClient) -> None:
        self.http = http

    def _url(self, board: str) -> str:
        return f"{API_BASE}/{quote(board, safe='')}"

    def check(self, identifier: str) -> str:
        data = self.http.get_json(self._url(identifier), params={"includeCompensation": "false"})
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            raise PermanentError(f"ashby:{identifier}: unexpected response shape")
        listed = [j for j in jobs if not (isinstance(j, dict) and j.get("isListed") is False)]
        return f"reachable; {len(listed)} listed job(s)"

    def fetch(self, identifier: str) -> FetchResult:
        result = FetchResult(self.name, identifier)
        data = self.http.get_json(self._url(identifier), params={"includeCompensation": "true"})
        result.pages = 1
        if not isinstance(data, dict):
            raise PermanentError(f"ashby:{identifier}: unexpected response shape")
        employer = clean_inline(data.get("organizationName") or data.get("name")) or identifier.replace("-", " ").title()
        result.employer = employer
        jobs = data.get("jobs")
        if isinstance(jobs, list):
            listed = [j for j in jobs if not (isinstance(j, dict) and j.get("isListed") is False)]
            skipped = len(jobs) - len(listed)
            if skipped:
                result.warnings.append(f"{skipped} unlisted job(s) ignored")
            jobs = listed
        result.normalize_each(jobs, lambda raw: self._convert(raw, identifier, employer))
        return result

    @staticmethod
    def _convert(raw: dict[str, Any], identifier: str, employer: str) -> NormalizedJob:
        secondary = [
            s.get("location") for s in raw.get("secondaryLocations") or [] if isinstance(s, dict)
        ]
        return build_normalized_job(
            source_name="ashby",
            source_identifier=identifier,
            external_id=raw.get("id"),
            employer=employer,
            title=raw.get("title"),
            location_parts=[raw.get("location"), *secondary],
            description_html=raw.get("descriptionHtml") or None,
            description_plain=raw.get("descriptionPlain"),
            workplace_type=raw.get("workplaceType"),
            is_remote=raw.get("isRemote") if isinstance(raw.get("isRemote"), bool) else None,
            employment_hints=[raw.get("employmentType")],
            department=raw.get("department") or raw.get("team"),
            posted_at=raw.get("publishedAt"),
            posted_at_source="publishedAt" if raw.get("publishedAt") else None,
            job_url=raw.get("jobUrl"),
            application_url=raw.get("applyUrl"),
            structured_salary=_salary(raw),
            raw_metadata={
                "team": raw.get("team"),
                "department": raw.get("department"),
                "employment_type": raw.get("employmentType"),
                "workplace_type": raw.get("workplaceType"),
                "is_remote": raw.get("isRemote"),
                "secondary_locations": [s for s in secondary if s],
            },
        )


def _salary(raw: dict[str, Any]) -> SalaryInfo | None:
    """The structured "Salary" component only (equity / commission / bonus are not salary)."""
    if raw.get("shouldDisplayCompensationOnJobPostings") is False:
        return None
    comp = raw.get("compensation")
    if not isinstance(comp, dict):
        return None
    components = [c for c in comp.get("summaryComponents") or [] if isinstance(c, dict)]
    salary = next((c for c in components if c.get("compensationType") == "Salary"), None)
    if salary is None:
        return None
    summary = clean_inline(comp.get("compensationTierSummary")) or None
    return structured_salary(
        low=salary.get("minValue"), high=salary.get("maxValue"), currency=salary.get("currencyCode"),
        interval=salary.get("interval"), text=summary, extra_text=summary,
    )
