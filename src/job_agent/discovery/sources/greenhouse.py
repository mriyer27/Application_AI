"""Greenhouse Job Board API (public, read-only, meant for career sites).

Docs: https://developers.greenhouse.io/job-board.html
  GET https://boards-api.greenhouse.io/v1/boards/{token}             -> {"name": ...}
  GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
      -> {"jobs": [...], "meta": {"total": N}}   (no pagination; "content" is HTML-escaped HTML)
"""

from __future__ import annotations

import html
from typing import Any
from urllib.parse import quote

from job_agent.discovery.http import PoliteHttpClient
from job_agent.discovery.normalize import build_normalized_job, clean_inline
from job_agent.discovery.sources.base import FetchResult
from job_agent.errors import PermanentError
from job_agent.models import NormalizedJob

API_BASE = "https://boards-api.greenhouse.io/v1/boards"
_EMPLOYMENT_METADATA_NAMES = ("employment type", "job type", "commitment", "time type", "employment", "worker type")


class GreenhouseAdapter:
    name = "greenhouse"
    is_mock = False

    def __init__(self, http: PoliteHttpClient) -> None:
        self.http = http

    def _board_url(self, token: str) -> str:
        return f"{API_BASE}/{quote(token, safe='')}"

    def check(self, identifier: str) -> str:
        board = self.http.get_json(self._board_url(identifier))
        name = clean_inline(board.get("name")) if isinstance(board, dict) else ""
        return f"reachable; board name {name!r}" if name else "reachable"

    def fetch(self, identifier: str) -> FetchResult:
        result = FetchResult(self.name, identifier)
        board = self.http.get_json(self._board_url(identifier))
        employer = clean_inline(board.get("name")) if isinstance(board, dict) else ""
        data = self.http.get_json(f"{self._board_url(identifier)}/jobs", params={"content": "true"})
        result.pages = 1
        if not isinstance(data, dict):
            raise PermanentError(f"greenhouse:{identifier}: unexpected response shape")
        jobs = data.get("jobs")
        if not employer and isinstance(jobs, list) and jobs and isinstance(jobs[0], dict):
            employer = clean_inline(jobs[0].get("company_name"))
        employer = employer or identifier.title()
        result.employer = employer
        result.normalize_each(jobs, lambda raw: self._convert(raw, identifier, employer))
        total = (data.get("meta") or {}).get("total") if isinstance(data.get("meta"), dict) else None
        if isinstance(total, int) and total > len(jobs or []):
            result.complete = False
            result.warnings.append(f"API reported {total} jobs but returned {len(jobs or [])}")
        return result

    @staticmethod
    def _convert(raw: dict[str, Any], identifier: str, employer: str) -> NormalizedJob:
        content = raw.get("content")
        description_html = html.unescape(content) if isinstance(content, str) else None
        location = raw.get("location") if isinstance(raw.get("location"), dict) else {}
        departments = [clean_inline(d.get("name")) for d in raw.get("departments") or [] if isinstance(d, dict)]
        offices = [clean_inline(o.get("name")) for o in raw.get("offices") or [] if isinstance(o, dict)]
        metadata = [m for m in raw.get("metadata") or [] if isinstance(m, dict)]
        employment_hints = [
            str(m.get("value")) for m in metadata
            if isinstance(m.get("value"), str)
            and any(n in clean_inline(m.get("name")).lower() for n in _EMPLOYMENT_METADATA_NAMES)
        ]
        meta_values = {
            clean_inline(m.get("name"))[:80]: m.get("value")
            for m in metadata
            if m.get("value") not in (None, "", []) and clean_inline(m.get("name"))
        }
        posted = raw.get("first_published")
        posted_source = "first_published" if posted else ("updated_at" if raw.get("updated_at") else None)
        return build_normalized_job(
            source_name="greenhouse",
            source_identifier=identifier,
            external_id=raw.get("id"),
            employer=employer,
            title=raw.get("title"),
            location_parts=[location.get("name")],
            description_html=description_html,
            employment_hints=employment_hints,
            department=departments[0] if departments else None,
            requisition_id=raw.get("requisition_id"),
            posted_at=posted or raw.get("updated_at"),
            posted_at_source=posted_source,
            closing_at=raw.get("application_deadline"),
            job_url=raw.get("absolute_url"),
            application_url=None,
            raw_metadata={
                "departments": [d for d in departments if d],
                "offices": [o for o in offices if o],
                "metadata": meta_values,
                "updated_at": raw.get("updated_at"),
                "internal_job_id": raw.get("internal_job_id"),
            },
        )
