"""Opt-in live checks against the real public job-board APIs: ``pytest -m network``.

Each test makes one or two small read-only requests. Deselected by default.
"""

from __future__ import annotations

import pytest

from job_agent.config import SourcesSection
from job_agent.discovery.http import PoliteHttpClient
from job_agent.discovery.sources import AshbyAdapter, GreenhouseAdapter, LeverAdapter

pytestmark = pytest.mark.network


@pytest.fixture
def http():
    client = PoliteHttpClient(SourcesSection(max_retries=1))
    yield client
    client.close()


def test_lever_tiny_live_fetch(http):
    result = LeverAdapter(http, max_pages=1, page_size=2).fetch("palantir")
    assert result.pages == 1 and len(result) + result.invalid >= 1
    job = result.jobs[0]
    assert job.source_name == "lever" and job.external_id and job.title
    assert job.job_url and job.job_url.startswith("https://jobs.lever.co/palantir/")
    assert not result.complete  # truncated by max_pages=1 -> closures would not be inferred


def test_greenhouse_and_ashby_reachable(http):
    assert "reachable" in GreenhouseAdapter(http).check("capco")
    assert "reachable" in AshbyAdapter(http).check("ramp")
