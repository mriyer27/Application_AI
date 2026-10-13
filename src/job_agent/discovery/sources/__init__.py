"""Source adapters. Real: greenhouse, lever, ashby (public job-board APIs). Mock: fixture."""

from __future__ import annotations

from typing import TYPE_CHECKING

from job_agent.discovery.sources.ashby import AshbyAdapter
from job_agent.discovery.sources.base import FetchResult, SourceAdapter
from job_agent.discovery.sources.fixture import FixtureAdapter
from job_agent.discovery.sources.greenhouse import GreenhouseAdapter
from job_agent.discovery.sources.lever import LeverAdapter
from job_agent.errors import PermanentError

if TYPE_CHECKING:  # pragma: no cover
    from job_agent.config import AppConfig
    from job_agent.discovery.http import PoliteHttpClient

#: Real sources, in the order they are run.
REAL_SOURCES: tuple[str, ...] = ("greenhouse", "lever", "ashby")
MOCK_SOURCES: tuple[str, ...] = ("fixture",)
ALL_SOURCES: tuple[str, ...] = REAL_SOURCES + MOCK_SOURCES


def create_adapter(name: str, *, http: "PoliteHttpClient | None", config: "AppConfig") -> SourceAdapter:
    if name == "fixture":
        return FixtureAdapter(config.home)
    if http is None:
        raise PermanentError(f"source {name!r} needs an HTTP client")
    if name == "greenhouse":
        return GreenhouseAdapter(http)
    if name == "lever":
        return LeverAdapter(http, max_pages=config.sources.max_pages)
    if name == "ashby":
        return AshbyAdapter(http)
    raise PermanentError(f"unknown job source {name!r}; known sources: {', '.join(ALL_SOURCES)}")


def configured_identifiers(config: "AppConfig", name: str) -> list[str]:
    s = config.sources
    if name == "greenhouse":
        values = s.greenhouse.boards
    elif name == "lever":
        values = s.lever.companies
    elif name == "ashby":
        values = s.ashby.boards
    else:
        return []
    out: list[str] = []
    for v in values:
        v = str(v).strip()
        if v and v not in out:
            out.append(v)
    return out


def source_enabled(config: "AppConfig", name: str) -> bool:
    section = getattr(config.sources, name, None)
    return bool(getattr(section, "enabled", False))


__all__ = [
    "ALL_SOURCES", "MOCK_SOURCES", "REAL_SOURCES", "AshbyAdapter", "FetchResult", "FixtureAdapter",
    "GreenhouseAdapter", "LeverAdapter", "SourceAdapter", "configured_identifiers", "create_adapter",
    "source_enabled",
]
