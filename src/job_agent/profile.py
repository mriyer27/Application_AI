"""Verified candidate profile.

The profile is the *only* source of truth for claims made in résumés, cover
letters and application answers. Every experience bullet is a ``Fact`` with a
stable ``id`` so generated documents can trace each claim back to a verified
source fact.

Work-authorization fields are deliberately tri-state (``None`` = unknown). The
system must never guess them; any question that depends on an unknown value is
routed to the human for review.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

from job_agent.errors import ProfileError

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{1,63}$")
_PLACEHOLDER_RE = re.compile(r"<[^<>]+>")


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_id(v: str) -> str:
    if not _ID_RE.match(v):
        raise ValueError(f"id {v!r} must be lowercase letters/digits/_/- (2-64 chars)")
    return v


Identifier = Annotated[str, AfterValidator(_check_id)]


class Personal(_M):
    full_name: str
    email: str
    phone: str | None = None
    location: str | None = None
    linkedin_url: str | None = None
    portfolio_url: str | None = None
    github_url: str | None = None


class Fact(_M):
    """A single verified statement about the candidate."""

    id: Identifier
    text: str = Field(min_length=10)
    skills: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    #: Quantities that appear in ``text`` (e.g. "40%", "11"). Generated text may
    #: only contain numbers that appear in some verified fact.
    metrics: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _metrics_in_text(self) -> "Fact":
        for m in self.metrics:
            if m not in self.text:
                raise ValueError(f"fact {self.id}: metric {m!r} does not appear in its text")
        return self


class Education(_M):
    id: Identifier
    institution: str
    degree: str
    field: str | None = None
    location: str | None = None
    start: str | None = None  # free text as verified, e.g. "Fall 2025"
    end: str | None = None  # e.g. "May 2027"
    expected: bool = False
    gpa: str | None = None
    facts: list[Fact] = Field(default_factory=list)


class Experience(_M):
    id: Identifier
    employer: str
    title: str
    team: str | None = None
    location: str | None = None
    dates: str  # verbatim, e.g. "Summer 2026" -- never invent months
    employment_type: Literal["full_time", "part_time", "internship", "contract", "other"] = "other"
    current: bool = False
    facts: list[Fact] = Field(min_length=1)


class Project(_M):
    id: Identifier
    name: str
    dates: str | None = None
    facts: list[Fact] = Field(min_length=1)


class Certification(_M):
    id: Identifier
    name: str
    issuer: str | None = None
    date: str | None = None


class WorkAuthorization(_M):
    """Tri-state: ``None`` means unknown -> requires human review. Never guessed."""

    #: Stated immigration status, e.g. "F-1". Informational only; never used
    #: to auto-answer a legal question.
    current_status: str | None = None
    authorized_to_work_in_us_now: bool | None = None
    authorized_for_internship_now: bool | None = None  # e.g. via CPT
    authorized_for_full_time_after_graduation: bool | None = None  # e.g. via OPT/STEM OPT
    requires_sponsorship_now: bool | None = None
    requires_sponsorship_future: bool | None = None
    us_citizen: bool | None = None
    us_person_for_export_control: bool | None = None
    security_clearance: str | None = None
    #: Free-text notes for the human reviewer. Never copied into applications.
    notes: str | None = None
    #: Set to the date you last verified these answers (e.g. "2026-10-12").
    last_verified: str | None = None


class VerifiedAnswer(_M):
    """A user-confirmed answer to a recurring application question."""

    id: Identifier
    question_patterns: list[str] = Field(min_length=1)  # case-insensitive substrings
    answer: str
    is_legal_declaration: bool = False


class Preferences(_M):
    target_roles: list[str] = Field(default_factory=list)
    avoid_focus: list[str] = Field(default_factory=list)
    preferred_themes: list[str] = Field(default_factory=list)
    salary_min_base: float | None = None
    salary_currency: str = "USD"
    relocation: bool | None = None
    preferred_locations: list[str] = Field(default_factory=list)


class CandidateProfile(_M):
    schema_version: Literal[1] = 1
    personal: Personal
    education: list[Education] = Field(min_length=1)
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    skills: list[str] = Field(min_length=1)
    preferences: Preferences = Field(default_factory=Preferences)
    work_authorization: WorkAuthorization = Field(default_factory=WorkAuthorization)
    verified_answers: list[VerifiedAnswer] = Field(default_factory=list)
    summary: str | None = None

    @model_validator(mode="after")
    def _unique_ids(self) -> "CandidateProfile":
        seen: set[str] = set()
        for fact_id in [f.id for f in self.all_facts()] + [e.id for e in self.education] + [
            x.id for x in self.experience
        ] + [p.id for p in self.projects]:
            if fact_id in seen:
                raise ValueError(f"duplicate id {fact_id!r} in profile")
            seen.add(fact_id)
        lowered = [s.lower() for s in self.skills]
        if len(set(lowered)) != len(lowered):
            raise ValueError("duplicate skills in profile")
        return self

    # -- helpers ----------------------------------------------------------- #
    def all_facts(self) -> list[Fact]:
        facts: list[Fact] = []
        for e in self.education:
            facts.extend(e.facts)
        for x in self.experience:
            facts.extend(x.facts)
        for p in self.projects:
            facts.extend(p.facts)
        return facts

    def fact_by_id(self, fact_id: str) -> Fact | None:
        return next((f for f in self.all_facts() if f.id == fact_id), None)

    def has_skill(self, skill: str) -> bool:
        return skill.strip().lower() in {s.lower() for s in self.skills}

    def placeholders(self) -> list[str]:
        """Return dotted paths of values still containing ``<placeholder>`` text."""
        found: list[str] = []

        def walk(obj, path: str) -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    walk(v, f"{path}.{k}" if path else k)
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    walk(v, f"{path}[{i}]")
            elif isinstance(obj, str) and _PLACEHOLDER_RE.search(obj):
                found.append(path)

        walk(self.model_dump(), "")
        return found

    def unknown_work_authorization_fields(self) -> list[str]:
        wa = self.work_authorization.model_dump()
        return [k for k, v in wa.items() if v is None and k not in {"notes", "security_clearance"}]

    def profile_hash(self) -> str:
        blob = json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:16]


def load_profile(path: Path) -> CandidateProfile:
    if not path.is_file():
        raise ProfileError(
            f"candidate profile not found at {path}. Copy profiles/candidate_profile.example.yaml "
            "to that path and fill in your verified details."
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ProfileError(f"profile {path} is not valid YAML: {exc}") from None
    try:
        return CandidateProfile.model_validate(data)
    except ValidationError as exc:
        lines = [f"  - {'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise ProfileError("invalid candidate profile:\n" + "\n".join(lines)) from None
