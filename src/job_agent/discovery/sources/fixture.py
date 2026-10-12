"""MOCK source: reads listings from a local JSON file. For tests and demos only.

This is not a job board. Rows it creates are tagged ``source_name='fixture'``
and the ``job_sources`` row is marked ``is_mock=1`` so reports can label them.
It can only be selected explicitly (``--source fixture:<path>``); it is never
part of the configured sources.

File format::

    {"employer": "Example Co (mock)",
     "jobs": [{"id": "1", "title": "Data Analyst", "location": "New York, NY",
               "description_html": "<p>...</p>"  (or "description": "plain text"),
               "workplace_type": "hybrid", "employment_type": "Full-time",
               "department": "...", "requisition_id": "...", "posted_at": "2026-01-01T00:00:00Z",
               "closing_at": null, "url": "https://...", "apply_url": "https://...",
               "salary": {"min": 1, "max": 2, "currency": "USD", "interval": "per-year", "text": "..."},
               "employer": "optional per-job override"}]}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from job_agent.discovery.normalize import build_normalized_job, clean_inline
from job_agent.discovery.salary import structured_salary
from job_agent.discovery.sources.base import FetchResult
from job_agent.errors import PermanentError
from job_agent.models import NormalizedJob
from job_agent.security.sanitize import ensure_within

MAX_FIXTURE_BYTES = 20_000_000


class FixtureAdapter:
    name = "fixture"
    is_mock = True

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)

    def _path(self, identifier: str) -> Path:
        path = ensure_within(self.base_dir, Path(identifier))
        if path.suffix.lower() != ".json":
            raise PermanentError(f"fixture source must be a .json file: {identifier}")
        if not path.is_file():
            raise PermanentError(f"fixture file not found: {identifier}")
        if path.stat().st_size > MAX_FIXTURE_BYTES:
            raise PermanentError(f"fixture file too large: {identifier}")
        return path

    def _load(self, identifier: str) -> dict[str, Any]:
        try:
            data = json.loads(self._path(identifier).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PermanentError(f"fixture file is not valid JSON: {identifier}: {exc}") from None
        if not isinstance(data, dict):
            raise PermanentError(f"fixture file must contain a JSON object: {identifier}")
        return data

    def check(self, identifier: str) -> str:
        data = self._load(identifier)
        jobs = data.get("jobs")
        return f"mock fixture file; {len(jobs) if isinstance(jobs, list) else 0} job(s)"

    def fetch(self, identifier: str) -> FetchResult:
        data = self._load(identifier)
        result = FetchResult(self.name, identifier, pages=1)
        employer = clean_inline(data.get("employer")) or "Mock Employer"
        result.employer = employer
        result.complete = data.get("complete", True) is not False
        result.normalize_each(data.get("jobs"), lambda raw: self._convert(raw, identifier, employer))
        return result

    @staticmethod
    def _convert(raw: dict[str, Any], identifier: str, employer: str) -> NormalizedJob:
        salary_raw = raw.get("salary") if isinstance(raw.get("salary"), dict) else None
        salary = None
        if salary_raw:
            salary = structured_salary(
                low=salary_raw.get("min"), high=salary_raw.get("max"), currency=salary_raw.get("currency"),
                interval=salary_raw.get("interval"), text=salary_raw.get("text"),
            )
        location = raw.get("location")
        locations = location if isinstance(location, list) else [location]
        return build_normalized_job(
            source_name="fixture",
            source_identifier=identifier,
            external_id=raw.get("id"),
            employer=raw.get("employer") or employer,
            title=raw.get("title"),
            location_parts=locations,
            description_html=raw.get("description_html"),
            description_plain=raw.get("description"),
            workplace_type=raw.get("workplace_type"),
            is_remote=raw.get("is_remote") if isinstance(raw.get("is_remote"), bool) else None,
            employment_hints=[raw.get("employment_type")],
            department=raw.get("department"),
            requisition_id=raw.get("requisition_id"),
            posted_at=raw.get("posted_at"),
            posted_at_source="posted_at" if raw.get("posted_at") else None,
            closing_at=raw.get("closing_at"),
            job_url=raw.get("url"),
            application_url=raw.get("apply_url"),
            structured_salary=salary,
            raw_metadata={"mock": True},
        )
