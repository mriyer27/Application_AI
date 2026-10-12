"""Source adapter protocol and the fetch result container."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from job_agent.discovery.normalize import InvalidListing, clean_inline
from job_agent.errors import PermanentError
from job_agent.logging_config import get_logger
from job_agent.models import NormalizedJob

log = get_logger(__name__)

#: How many invalid-listing reasons to keep per fetch (for reports / debugging).
MAX_INVALID_REASONS = 10


@dataclass
class FetchResult:
    """All listings returned for one source identifier.

    Iterating yields the valid ``NormalizedJob`` objects. ``complete`` is False
    when pagination was cut by ``sources.max_pages`` (or the API reported more
    jobs than it returned); closures are only inferred from complete fetches.
    """

    source_name: str
    identifier: str
    jobs: list[NormalizedJob] = field(default_factory=list)
    #: Every external id the source returned, including ids of invalid listings.
    seen_external_ids: set[str] = field(default_factory=set)
    invalid: int = 0
    invalid_reasons: list[str] = field(default_factory=list)
    complete: bool = True
    pages: int = 0
    warnings: list[str] = field(default_factory=list)
    employer: str | None = None

    def __iter__(self) -> Iterator[NormalizedJob]:
        return iter(self.jobs)

    def __len__(self) -> int:
        return len(self.jobs)

    def add(self, job: NormalizedJob) -> None:
        self.jobs.append(job)
        self.seen_external_ids.add(job.external_id)

    def add_invalid(self, reason: str, external_id: object = None) -> None:
        self.invalid += 1
        ext = clean_inline(external_id)
        if ext:
            self.seen_external_ids.add(ext)
        if len(self.invalid_reasons) < MAX_INVALID_REASONS:
            self.invalid_reasons.append(f"{ext or '?'}: {reason}"[:200])

    def normalize_each(self, raw_items: object,
                       convert: Callable[[dict[str, Any]], NormalizedJob | None]) -> None:
        """Apply ``convert(raw) -> NormalizedJob | None`` to each item, counting invalid ones."""
        if not isinstance(raw_items, list):
            raise PermanentError(f"{self.source_name}:{self.identifier}: unexpected response shape "
                                 "(expected a list of jobs)")
        for raw in raw_items:
            if not isinstance(raw, dict):
                self.add_invalid("listing is not an object")
                continue
            try:
                job = convert(raw)
            except (InvalidListing, ValidationError, ValueError, TypeError, KeyError, AttributeError) as exc:
                reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
                log.info("skipping invalid %s listing %s: %s", self.source_name, raw.get("id"), reason)
                self.add_invalid(reason, raw.get("id"))
                continue
            if job is not None:
                self.add(job)


@runtime_checkable
class SourceAdapter(Protocol):
    """A public job source. ``fetch`` returns all current listings for one identifier."""

    name: str
    is_mock: bool

    def fetch(self, identifier: str) -> FetchResult: ...

    def check(self, identifier: str) -> str:
        """One lightweight request; returns a short human-readable detail or raises."""
        ...
