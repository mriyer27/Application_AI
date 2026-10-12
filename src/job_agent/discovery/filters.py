"""Deterministic search filters driven by ``config.search``.

Two stages:

1. ``title_is_relevant`` -- the *prefilter*. A title is relevant when it
   contains every word of some ``target_titles`` entry (any order, so
   "Analyst, Business Intelligence" matches "Business Intelligence Analyst"
   and seniority prefixes do not matter) or any ``title_keywords`` phrase.
   Irrelevant listings are counted but never stored.
2. ``evaluate_filters`` -- everything else. Listings that fail are stored with
   ``pipeline_state='filtered_out'`` and a readable ``filter_reason``.

All keyword matching is case-insensitive and on word boundaries over
normalized text: ``"sr"`` matches "Sr. Analyst" but not "Srinagar", ``"lead"``
matches "Team Lead" but not "Leadership Program", and the location ``"US"``
matches "US - New York" but never "Austin".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from job_agent.config import SearchSection
from job_agent.discovery.normalize import contains_phrase, normalize_key
from job_agent.models import NormalizedJob, WorkArrangement
from job_agent.utils.time import parse_iso, utcnow


def _tokens(text: str) -> set[str]:
    out: set[str] = set()
    for tok in normalize_key(text).split():
        out.add(tok)
        if len(tok) > 3 and tok.endswith("s"):
            out.add(tok[:-1])  # "analysts" -> "analyst"
    return out


def title_is_relevant(title: str, search: SearchSection) -> bool:
    key = normalize_key(title)
    if not key:
        return False
    tokens = _tokens(title)
    for target in search.target_titles:
        wanted = set(normalize_key(target).split())
        if wanted and wanted <= tokens:
            return True
    for phrase in search.title_keywords:
        if contains_phrase(key, phrase) or contains_phrase(key, f"{phrase}s"):
            return True
    return False


@dataclass
class FilterDecision:
    passed: bool = True
    codes: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    def fail(self, code: str, message: str) -> None:
        self.passed = False
        self.codes.append(code)
        self.messages.append(message)

    @property
    def reason(self) -> str | None:
        return "; ".join(self.messages) if self.messages else None


def excluded_title_keyword(title: str, search: SearchSection) -> str | None:
    key = normalize_key(title)
    for phrase in search.exclude_title_keywords:
        if contains_phrase(key, phrase):
            return phrase
    return None


def _location_matches(job: NormalizedJob, search: SearchSection) -> bool:
    if not job.location:
        return True  # unknown location is not a reason to drop a listing
    key = normalize_key(job.location)
    for wanted in search.locations:
        if normalize_key(wanted) == "remote" and job.work_arrangement == WorkArrangement.REMOTE:
            return True
        if contains_phrase(key, wanted):
            return True
    return False


def _mentions(job: NormalizedJob, word: str) -> bool:
    return bool(job.location) and contains_phrase(normalize_key(job.location), word)


def evaluate_filters(job: NormalizedJob, search: SearchSection, *, now: datetime | None = None,
                     title_relevant: bool = True) -> FilterDecision:
    """Apply every non-title filter and return all reasons a listing fails."""
    now = now or utcnow()
    decision = FilterDecision()
    if not title_relevant:
        decision.fail("title_not_relevant", "title no longer matches target titles / keywords")

    excluded = excluded_title_keyword(job.title, search)
    if excluded:
        decision.fail("excluded_title_keyword", f"title contains excluded keyword {excluded!r}")

    if not _location_matches(job, search):
        decision.fail("location", f"location {job.location!r} does not match search.locations")

    pref = search.remote_preference
    arrangement = job.work_arrangement
    if pref == "remote_only" and not (arrangement == WorkArrangement.REMOTE or _mentions(job, "remote")):
        decision.fail("remote_preference", f"work arrangement is {arrangement.value} (remote_only)")
    elif pref == "remote_or_hybrid" and not (
        arrangement in {WorkArrangement.REMOTE, WorkArrangement.HYBRID}
        or _mentions(job, "remote") or _mentions(job, "hybrid")
    ):
        decision.fail("remote_preference", f"work arrangement is {arrangement.value} (remote_or_hybrid)")

    if job.employment_type not in search.employment_types:
        decision.fail("employment_type", f"employment type {job.employment_type.value} is not in "
                                         "search.employment_types")

    years = job.raw_metadata.get("years_experience_min")
    if isinstance(years, int) and years > search.max_years_experience:
        decision.fail("experience_years", f"requires {years}+ years of experience "
                                          f"(max {search.max_years_experience})")

    posted = parse_iso(job.posted_at) if job.posted_at else None
    if posted is not None and posted < now - timedelta(days=search.max_posting_age_days):
        age = (now - posted).days
        decision.fail("posting_age", f"posted {age} days ago (max {search.max_posting_age_days})")

    closing = parse_iso(job.closing_at) if job.closing_at else None
    if closing is not None and closing < now:
        decision.fail("closing_date_passed", f"closing date {job.closing_at} has passed")
    return decision
