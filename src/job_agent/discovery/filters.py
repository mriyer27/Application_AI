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
matches "US - New York" but never "Austin". When ``search.locations`` targets
the US ("United States"/"USA"/"US"), listings named only by city and state
("San Francisco, CA", "Bellevue, Washington") also match.
"""

from __future__ import annotations

import re
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


_US_ALIASES = {"united states", "united states of america", "usa", "us", "u s"}
_US_STATES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california",
    "CO": "colorado", "CT": "connecticut", "DE": "delaware", "FL": "florida", "GA": "georgia",
    "HI": "hawaii", "ID": "idaho", "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas",
    "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland", "MA": "massachusetts",
    "MI": "michigan", "MN": "minnesota", "MS": "mississippi", "MO": "missouri", "MT": "montana",
    "NE": "nebraska", "NV": "nevada", "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico",
    "NY": "new york", "NC": "north carolina", "ND": "north dakota", "OH": "ohio", "OK": "oklahoma",
    "OR": "oregon", "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina",
    "SD": "south dakota", "TN": "tennessee", "TX": "texas", "UT": "utah", "VT": "vermont",
    "VA": "virginia", "WA": "washington", "WV": "west virginia", "WI": "wisconsin", "WY": "wyoming",
    "DC": "district of columbia",
}
#: Country / region names that mark a location part as non-US even when it
#: contains a state-like token ("Berlin, DE", "Bengaluru, IN", "Toronto, ON, Canada").
_NON_US_MARKERS = (
    "canada", "mexico", "brazil", "argentina", "colombia", "chile", "united kingdom", "uk", "england",
    "scotland", "ireland", "germany", "deutschland", "france", "spain", "portugal", "italy", "netherlands",
    "belgium", "switzerland", "austria", "poland", "czech", "sweden", "norway", "denmark", "finland",
    "india", "china", "hong kong", "japan", "korea", "singapore", "malaysia", "philippines", "vietnam",
    "indonesia", "australia", "new zealand", "israel", "uae", "dubai", "saudi", "qatar", "doha",
    "south africa", "nigeria", "kenya", "egypt", "turkey", "romania", "hungary", "greece", "ukraine",
    "emea", "apac", "latam", "europe",
)
_LOCATION_SPLIT = re.compile(r"\s*(?:;|\||/|\bor\b|\n)\s*", re.I)
_AMBIGUOUS_CODES = {"DE", "IN"}
_ZIP = re.compile(r"\b\d{5}(?:-\d{4})?\b")
_STATE_CODE = re.compile(r"(?:^|,|\s-\s|\()\s*([A-Z]{2})\s*(?:$|,|\)|\s\d{5})")


def _wants_us(search: SearchSection) -> bool:
    return any(normalize_key(w) in _US_ALIASES for w in search.locations)


def is_us_location(location: str) -> bool:
    """True if any part of a (possibly multi-location) string is clearly in the US.

    Recognizes US aliases, full state names and ", XX" state codes per part, and
    ignores parts that name another country or region.
    """
    for part in _LOCATION_SPLIT.split(location or ""):
        if not part.strip():
            continue
        key = normalize_key(part)
        if any(contains_phrase(key, marker) for marker in _NON_US_MARKERS):
            continue
        if any(contains_phrase(key, alias) for alias in _US_ALIASES - {"u s"}):
            return True
        if any(contains_phrase(key, name) for name in _US_STATES.values()):
            return True
        for m in _STATE_CODE.finditer(part.strip()):
            code = m.group(1)
            # DE/IN double as ISO country codes (Germany, India): need a ZIP to count.
            if code in _US_STATES and (code not in _AMBIGUOUS_CODES or _ZIP.search(part)):
                return True
    return False


def names_only_foreign_places(location: str) -> bool:
    """True if every named part of the location is outside the US ("Remote - UK", "Remote Canada")."""
    parts = [p for p in _LOCATION_SPLIT.split(location or "") if p.strip()]
    foreign = [p for p in parts if any(contains_phrase(normalize_key(p), m) for m in _NON_US_MARKERS)]
    return bool(foreign) and not is_us_location(location)


def _location_matches(job: NormalizedJob, search: SearchSection) -> bool:
    if not job.location:
        return True  # unknown location is not a reason to drop a listing
    key = normalize_key(job.location)
    wants_us = _wants_us(search)
    remote_wanted = False
    for wanted in search.locations:
        wanted_key = normalize_key(wanted)
        if wanted_key == "remote":
            remote_wanted = True
            continue
        if wants_us and wanted_key in _US_ALIASES:
            continue  # handled by is_us_location below (per location part)
        if contains_phrase(key, wanted):
            return True  # an explicitly listed place, e.g. "New York" or "Canada"
    if wants_us and is_us_location(job.location):
        return True
    if remote_wanted and (job.work_arrangement == WorkArrangement.REMOTE or contains_phrase(key, "remote")):
        # A remote role restricted to another country does not fit a US search.
        return not (wants_us and names_only_foreign_places(job.location))
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
