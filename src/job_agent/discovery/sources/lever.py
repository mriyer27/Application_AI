"""Lever Postings API (public, read-only, meant for career sites).

Docs: https://github.com/lever/postings-api
  GET https://api.lever.co/v0/postings/{company}?mode=json&skip=N&limit=M  -> [posting, ...]
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from job_agent.discovery.http import PoliteHttpClient
from job_agent.discovery.normalize import build_normalized_job, clean_inline
from job_agent.discovery.salary import structured_salary
from job_agent.discovery.sources.base import FetchResult
from job_agent.errors import PermanentError
from job_agent.models import NormalizedJob

API_BASE = "https://api.lever.co/v0/postings"
DEFAULT_PAGE_SIZE = 100


class LeverAdapter:
    name = "lever"
    is_mock = False

    def __init__(self, http: PoliteHttpClient, *, max_pages: int, page_size: int | None = None) -> None:
        self.http = http
        self.max_pages = max_pages
        self.page_size = page_size or DEFAULT_PAGE_SIZE

    def _url(self, company: str) -> str:
        return f"{API_BASE}/{quote(company, safe='')}"

    def check(self, identifier: str) -> str:
        data = self.http.get_json(self._url(identifier), params={"mode": "json", "skip": 0, "limit": 1})
        if not isinstance(data, list):
            raise PermanentError(f"lever:{identifier}: unexpected response shape")
        return "reachable; has open postings" if data else "reachable; no open postings"

    def fetch(self, identifier: str) -> FetchResult:
        result = FetchResult(self.name, identifier)
        employer = identifier.replace("-", " ").title()
        result.employer = employer
        skip = 0
        while True:
            page = self.http.get_json(self._url(identifier),
                                      params={"mode": "json", "skip": skip, "limit": self.page_size})
            result.pages += 1
            if not isinstance(page, list):
                raise PermanentError(f"lever:{identifier}: unexpected response shape (page {result.pages})")
            result.normalize_each(page, lambda raw: self._convert(raw, identifier, employer))
            if len(page) < self.page_size:
                break
            if result.pages >= self.max_pages:
                result.complete = False
                result.warnings.append(f"stopped after sources.max_pages={self.max_pages} pages")
                break
            skip += self.page_size
        return result

    @staticmethod
    def _convert(raw: dict[str, Any], identifier: str, employer: str) -> NormalizedJob:
        categories = raw.get("categories") if isinstance(raw.get("categories"), dict) else {}
        all_locations = [loc for loc in categories.get("allLocations") or [] if isinstance(loc, str)]
        locations = all_locations or [categories.get("location")]
        parts = [raw.get("description") or ""]
        for item in raw.get("lists") or []:
            if isinstance(item, dict):
                heading = clean_inline(item.get("text"))
                parts.append(f"<h3>{_escape(heading)}</h3><ul>{item.get('content') or ''}</ul>")
        parts.append(raw.get("additional") or "")
        description_html = "\n".join(p for p in parts if p)
        description_plain = None
        if not description_html.strip():
            description_plain = "\n\n".join(
                str(raw.get(k) or "") for k in ("descriptionPlain", "additionalPlain") if raw.get(k)
            )
            description_html = None
        salary_range = raw.get("salaryRange") if isinstance(raw.get("salaryRange"), dict) else None
        salary = None
        if salary_range:
            salary_text = clean_inline(raw.get("salaryDescriptionPlain")) or None
            salary = structured_salary(
                low=salary_range.get("min"), high=salary_range.get("max"), currency=salary_range.get("currency"),
                interval=salary_range.get("interval"),
                text=salary_text or _describe_range(salary_range),
                extra_text=salary_text,
            )
        return build_normalized_job(
            source_name="lever",
            source_identifier=identifier,
            external_id=raw.get("id"),
            employer=employer,
            title=raw.get("text"),
            location_parts=locations,
            description_html=description_html,
            description_plain=description_plain,
            workplace_type=raw.get("workplaceType"),
            employment_hints=[categories.get("commitment")],
            department=categories.get("department") or categories.get("team"),
            posted_at=raw.get("createdAt"),
            posted_at_source="createdAt" if raw.get("createdAt") else None,
            job_url=raw.get("hostedUrl"),
            application_url=raw.get("applyUrl"),
            structured_salary=salary,
            raw_metadata={
                "commitment": categories.get("commitment"),
                "team": categories.get("team"),
                "department": categories.get("department"),
                "country": raw.get("country"),
                "workplace_type": raw.get("workplaceType"),
                "all_locations": all_locations,
            },
        )


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _describe_range(rng: dict[str, Any]) -> str:
    """A plain description of the structured range (labelled as such, not presented as quoted text)."""
    low, high = rng.get("min"), rng.get("max")
    amount = f"{low:,}" if isinstance(low, (int, float)) else "?"
    if isinstance(high, (int, float)) and high != low:
        amount += f"-{high:,}"
    return f"{rng.get('currency') or ''} {amount} ({rng.get('interval') or 'interval not stated'})".strip()
