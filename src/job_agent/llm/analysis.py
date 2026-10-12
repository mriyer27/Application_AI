"""Validated job analysis with the local LLM.

The job posting is untrusted: it is cleaned, truncated to
``llm.max_input_chars`` and wrapped with ``security.untrusted.wrap_untrusted``
before it reaches the model. The model's reply is validated by Pydantic and
then post-validated by deterministic code:

* ``required_skills`` / ``preferred_skills`` not literally present in the
  posting (case-insensitive, word boundary) are dropped;
* ``sponsorship_statements`` that are not verbatim excerpts of the posting
  (after whitespace / typographic-quote normalization) are dropped;
* ``years_experience_required`` is kept only if that number appears in the
  posting; relevance scores are clamped to 0..1; text is length-limited and
  stripped of control characters.

Only the candidate's verified skills and fact texts are given to the model --
no contact details and no work-authorization values.
"""

from __future__ import annotations

import math
import re
import sqlite3
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from job_agent.database.connection import Database, from_json
from job_agent.errors import JobAgentError
from job_agent.llm import prompts, repository
from job_agent.llm.client import OllamaClient, get_client, mark_unavailable
from job_agent.llm.errors import LLMUnavailable
from job_agent.logging_config import get_logger
from job_agent.models import CodingIntensity, JobAnalysis
from job_agent.profile import CandidateProfile
from job_agent.security.redaction import redact_text
from job_agent.security.untrusted import clean_untrusted, wrap_untrusted

if TYPE_CHECKING:  # pragma: no cover
    from job_agent.context import AppContext

log = get_logger(__name__)

PROFILE_SUMMARY_MAX_CHARS = 4000
ANALYSIS_MAX_TOKENS = 1500

_SENIORITY_ALIASES = {
    "internship": "intern", "junior": "entry", "entry-level": "entry", "entry level": "entry",
    "new grad": "entry", "new_grad": "entry", "associate": "entry", "early career": "entry",
    "mid-level": "mid", "mid level": "mid", "intermediate": "mid",
    "lead": "senior", "staff": "senior", "principal": "senior", "sr": "senior",
}
_NUMBER_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
                 "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
                 "nineteen", "twenty"]
_ELIGIBILITY_RE = re.compile(
    r"sponsor|visa|h-?1b|authori[sz]|citizen|green card|permanent resident|clearance|export|itar|\bear\b|"
    r"immigration|right to work|eligible to work|legally",
    re.I,
)
_TYPOGRAPHIC = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-",
                              "\u2014": "-", "\u00a0": " "})
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


# --------------------------------------------------------------------------- #
# Schema sent to Ollama (``format``); converted to ``models.JobAnalysis`` after checks
# --------------------------------------------------------------------------- #
class RawJobAnalysis(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: str
    role_family: str
    seniority: Literal["intern", "entry", "mid", "senior", "unknown"]
    years_experience_required: int | None
    required_skills: list[str]
    preferred_skills: list[str]
    responsibilities: list[str]
    coding_intensity: Literal["low", "medium", "high", "unknown"]
    ai_relevance: float
    business_relevance: float
    analytics_relevance: float
    product_relevance: float
    sponsorship_statements: list[str]
    red_flags: list[str]
    fit_explanation: str

    @field_validator("seniority", mode="before")
    @classmethod
    def _seniority(cls, v: Any) -> Any:
        if isinstance(v, str):
            key = v.strip().lower()
            key = _SENIORITY_ALIASES.get(key, key)
            return key if key in {"intern", "entry", "mid", "senior", "unknown"} else "unknown"
        return v

    @field_validator("coding_intensity", mode="before")
    @classmethod
    def _coding(cls, v: Any) -> Any:
        if isinstance(v, str):
            key = v.strip().lower()
            return key if key in {"low", "medium", "high", "unknown"} else "unknown"
        return v


@dataclass(frozen=True)
class PreparedJob:
    """Exactly what the model sees, plus the cache key."""

    visible_text: str  # cleaned + truncated posting (what deterministic checks compare against)
    wrapped: str  # the same text inside an untrusted block
    profile_summary: str
    input_hash: str


@dataclass(frozen=True)
class AnalysisOutcome:
    analysis: JobAnalysis
    cached: bool
    duration_ms: int | None
    input_hash: str


# --------------------------------------------------------------------------- #
# Input preparation
# --------------------------------------------------------------------------- #
def _as_dict(job: Mapping[str, Any] | sqlite3.Row) -> dict[str, Any]:
    if isinstance(job, sqlite3.Row):
        return {k: job[k] for k in job.keys()}
    return dict(job)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parsed = from_json(value, default=None)
        if isinstance(parsed, list):
            value = parsed
        else:
            return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v is not None and str(v).strip()]
    return []


def build_job_text(job: Mapping[str, Any]) -> str:
    """Plain-text rendering of a job row / ``NormalizedJob`` dict (still untrusted)."""
    data = _as_dict(job)
    lines: list[str] = []
    for label, key in (("Title", "title"), ("Employer", "employer"), ("Location", "location"),
                       ("Employment type", "employment_type"), ("Work arrangement", "work_arrangement"),
                       ("Experience level", "experience_level"), ("Salary", "salary_text")):
        value = data.get(key)
        if value is not None and str(value).strip() and str(value) != "unknown":
            lines.append(f"{label}: {value}")
    description = str(data.get("description_text") or "").strip()
    if description:
        lines += ["", description]
    for label, key in (("Required qualifications", "required_qualifications"),
                       ("Preferred qualifications", "preferred_qualifications")):
        items = _as_list(data.get(key))
        if items:
            lines += ["", f"{label}:", *(f"- {item}" for item in items)]
    for label, key in (("Sponsorship information", "sponsorship_info"),
                       ("Work authorization requirements", "work_authorization_requirements")):
        value = data.get(key)
        if value and str(value).strip() and str(value).strip() not in description:
            lines += ["", f"{label}: {value}"]
    return "\n".join(lines).strip()


def profile_summary(profile: CandidateProfile, *, max_chars: int = PROFILE_SUMMARY_MAX_CHARS) -> str:
    """Verified skills + fact texts only (no contact details, no work-authorization values)."""
    lines = ["Verified skills: " + ", ".join(profile.skills), "Verified facts:"]
    for fact in profile.all_facts():
        lines.append(f"- {redact_text(fact.text, pii=True)}")
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[:max_chars].rsplit("\n", 1)[0]


def prepare_job_input(job: Mapping[str, Any], profile: CandidateProfile, *, max_chars: int) -> PreparedJob:
    raw = build_job_text(job)
    visible = clean_untrusted(raw, max_chars=max_chars)
    wrapped = wrap_untrusted(raw, label="JOB_POSTING", max_chars=max_chars)
    summary = profile_summary(profile)
    return PreparedJob(visible_text=visible, wrapped=wrapped, profile_summary=summary,
                       input_hash=repository.input_hash(visible, summary))


# --------------------------------------------------------------------------- #
# Deterministic post-validation
# --------------------------------------------------------------------------- #
def _norm(text: str) -> str:
    return " ".join(text.translate(_TYPOGRAPHIC).split())


def clean_field(text: Any, max_len: int) -> str:
    value = _CONTROL_RE.sub("", str(text or ""))
    value = " ".join(value.split())
    return value if len(value) <= max_len else value[: max_len - 1].rstrip() + "…"


def find_term(term: str, text: str) -> str | None:
    """The occurrence of ``term`` in ``text`` (case-insensitive, word boundary), as written in ``text``."""
    t = _norm(term).lstrip("\"'`([").rstrip("\"'`)].,;:")
    if not t:
        return None
    pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])", re.I)
    match = pattern.search(_norm(text))
    return match.group(0) if match else None


def filter_skills(skills: Iterable[str], text: str, *, limit: int = 40) -> list[str]:
    kept: list[str] = []
    seen: set[str] = set()
    for skill in skills:
        if not isinstance(skill, str) or len(skill) > 80 or len(skill.split()) > 8:
            continue
        found = find_term(_CONTROL_RE.sub("", skill), text)
        if found and found.lower() not in seen:
            seen.add(found.lower())
            kept.append(found)
        if len(kept) >= limit:
            break
    return kept


def filter_verbatim(statements: Iterable[str], text: str, *, limit: int = 10) -> list[str]:
    """Keep eligibility-related statements that are verbatim excerpts of ``text``."""
    haystack = _norm(text)
    lowered = haystack.lower()
    kept: list[str] = []
    for statement in statements:
        if not isinstance(statement, str):
            continue
        needle = _norm(_CONTROL_RE.sub("", statement)).strip("\"'` ")
        if len(needle) < 8 or len(needle) > 500:
            continue
        idx = lowered.find(needle.lower())
        if idx < 0:
            continue
        excerpt = haystack[idx: idx + len(needle)]
        if _ELIGIBILITY_RE.search(excerpt) and excerpt not in kept:
            kept.append(excerpt)
        if len(kept) >= limit:
            break
    return kept


def _unit(value: float) -> float:
    if value is None or not math.isfinite(value):
        return 0.0
    if 1 < value <= 10:
        value /= 10  # "8 out of 10"
    elif 10 < value <= 100:
        value /= 100  # percentage
    return round(min(1.0, max(0.0, value)), 3)


def _years_in_text(years: int, text: str) -> bool:
    lowered = text.lower()
    if re.search(rf"(?<![\d.]){years}(?!\d)", lowered):
        return True
    return years < len(_NUMBER_WORDS) and re.search(rf"\b{_NUMBER_WORDS[years]}\b", lowered) is not None


def post_validate(raw: RawJobAnalysis, job_text: str, *, model: str) -> JobAnalysis:
    years = raw.years_experience_required
    if years is not None:
        years = max(0, min(40, years))
        if not _years_in_text(years, job_text):
            years = None
    responsibilities = [clean_field(r, 300) for r in raw.responsibilities if isinstance(r, str) and r.strip()][:20]
    red_flags = [clean_field(r, 200) for r in raw.red_flags if isinstance(r, str) and r.strip()][:10]
    return JobAnalysis(
        summary=clean_field(raw.summary, 800),
        role_family=clean_field(raw.role_family, 80).lower() or "unknown",
        seniority=clean_field(raw.seniority, 40) or "unknown",
        years_experience_required=years,
        required_skills=filter_skills(raw.required_skills, job_text),
        preferred_skills=filter_skills(raw.preferred_skills, job_text),
        responsibilities=responsibilities,
        coding_intensity=CodingIntensity(raw.coding_intensity),
        ai_relevance=_unit(raw.ai_relevance),
        business_relevance=_unit(raw.business_relevance),
        analytics_relevance=_unit(raw.analytics_relevance),
        product_relevance=_unit(raw.product_relevance),
        sponsorship_statements=filter_verbatim(raw.sponsorship_statements, job_text),
        red_flags=red_flags,
        fit_explanation=clean_field(raw.fit_explanation, 1500),
        model=model,
        prompt_version=prompts.PROMPT_VERSION,
    )


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def _analyze_prepared(client: OllamaClient, prepared: PreparedJob, *, model: str) -> JobAnalysis:
    raw = client.generate_json(
        system=prompts.JOB_ANALYSIS_SYSTEM,
        prompt=prompts.job_analysis_prompt(prepared.wrapped, prepared.profile_summary),
        schema=RawJobAnalysis,
        model=model,
        max_tokens=ANALYSIS_MAX_TOKENS,
    )
    return post_validate(raw, prepared.visible_text, model=model)


def analyze_job(client: OllamaClient, job: Mapping[str, Any], profile: CandidateProfile, *,
                model: str | None = None) -> JobAnalysis:
    """Analyze one job with the local LLM (no caching). Raises ``LLMUnavailable`` / ``JobAgentError``."""
    model_name = client.resolve_model(model)
    prepared = prepare_job_input(job, profile, max_chars=client.config.max_input_chars)
    return _analyze_prepared(client, prepared, model=model_name)


def _job_id(job: Mapping[str, Any]) -> int | None:
    value = _as_dict(job).get("id")
    return value if isinstance(value, int) else None


def analyze_job_cached(db: Database, client: OllamaClient, job: Mapping[str, Any], profile: CandidateProfile, *,
                       use_cache: bool = True, model: str | None = None) -> AnalysisOutcome:
    """``analyze_job`` with the ``llm_analyses`` cache (keyed by model, prompt version and input hash)."""
    model_name = client.resolve_model(model)
    prepared = prepare_job_input(job, profile, max_chars=client.config.max_input_chars)
    if use_cache:
        cached = repository.get_cached_analysis(db, model=model_name, prompt_version=prompts.PROMPT_VERSION,
                                                input_hash=prepared.input_hash)
        if cached is not None:
            return AnalysisOutcome(cached, True, None, prepared.input_hash)
    started = time.monotonic()
    analysis = _analyze_prepared(client, prepared, model=model_name)
    duration_ms = int((time.monotonic() - started) * 1000)
    repository.save(db, purpose=repository.PURPOSE_JOB_ANALYSIS, model=model_name,
                    prompt_version=prompts.PROMPT_VERSION, input_hash=prepared.input_hash, output=analysis,
                    job_id=_job_id(job), duration_ms=duration_ms)
    log.info("job analysis model=%s ms=%d job_id=%s", model_name, duration_ms, _job_id(job))
    return AnalysisOutcome(analysis, False, duration_ms, prepared.input_hash)


class JobAnalyzer:
    """Callable ``job -> JobAnalysis | None`` handed to the scoring module.

    * cache hits are always served;
    * at most ``budget`` fresh LLM calls per instance (``scoring.max_llm_jobs_per_run``);
    * failures are recorded with ``repo.record_error(component="llm")`` and return ``None``;
    * after ``LLMUnavailable`` the analyzer stops calling the model for the rest of its life
      (circuit breaker), so a dead server costs one timeout per run, not one per job.
    """

    def __init__(self, ctx: "AppContext", client: OllamaClient, profile: CandidateProfile, *, budget: int) -> None:
        self.ctx = ctx
        self.client = client
        self.profile = profile
        self.model = client.resolve_model()
        self.budget = budget
        self.llm_calls = 0
        self.cache_hits = 0
        self.failures = 0
        self.disabled_reason: str | None = None

    def __call__(self, job: Mapping[str, Any]) -> JobAnalysis | None:
        if self.disabled_reason is not None:
            return None
        job_id = None
        try:
            job_id = _job_id(job)
            prepared = prepare_job_input(job, self.profile, max_chars=self.client.config.max_input_chars)
            cached = repository.get_cached_analysis(self.ctx.db, model=self.model,
                                                    prompt_version=prompts.PROMPT_VERSION,
                                                    input_hash=prepared.input_hash)
            if cached is not None:
                self.cache_hits += 1
                return cached
            if self.llm_calls >= self.budget:
                return None
            self.llm_calls += 1
            started = time.monotonic()
            analysis = _analyze_prepared(self.client, prepared, model=self.model)
            duration_ms = int((time.monotonic() - started) * 1000)
            repository.save(self.ctx.db, purpose=repository.PURPOSE_JOB_ANALYSIS, model=self.model,
                            prompt_version=prompts.PROMPT_VERSION, input_hash=prepared.input_hash,
                            output=analysis, job_id=job_id, duration_ms=duration_ms)
            log.info("job analysis model=%s ms=%d job_id=%s", self.model, duration_ms, job_id)
            return analysis
        except LLMUnavailable as exc:
            self.failures += 1
            self.disabled_reason = str(exc)
            mark_unavailable(self.client.config)
            self._record(exc, job_id)
            return None
        except Exception as exc:  # recorded (not swallowed): one bad job must not break scoring
            self.failures += 1
            self._record(exc, job_id)
            return None

    def _record(self, exc: BaseException, job_id: int | None) -> None:
        from job_agent.database import repo

        try:
            repo.record_error(self.ctx.db, component="llm", error=exc, job_id=job_id,
                              details={"model": self.model, "purpose": repository.PURPOSE_JOB_ANALYSIS})
        except (JobAgentError, sqlite3.Error) as record_exc:
            log.warning("could not record llm error (%s); original error type %s", type(record_exc).__name__,
                        type(exc).__name__)

    def stats(self) -> dict[str, Any]:
        return {"model": self.model, "llm_calls": self.llm_calls, "cache_hits": self.cache_hits,
                "failures": self.failures, "disabled_reason": self.disabled_reason}

    def close(self) -> None:
        self.client.close()


def make_analyzer(ctx: "AppContext", *, client: OllamaClient | None = None) -> JobAnalyzer | None:
    """Analyzer for the scoring module, or ``None`` if disabled or unavailable (one health probe)."""
    cfg = ctx.config
    if not cfg.llm.enabled or not cfg.scoring.use_llm:
        return None
    owned = client is None
    if client is None:
        client = get_client(ctx)
        if client is None:
            return None
    else:
        try:
            client.resolve_model()
        except LLMUnavailable as exc:
            log.info("local LLM unavailable: %s", exc)
            return None
    try:
        profile = ctx.profile
    except JobAgentError as exc:
        from job_agent.database import repo

        repo.record_error(ctx.db, component="llm", error=exc)
        if owned:
            client.close()
        return None
    return JobAnalyzer(ctx, client, profile, budget=cfg.scoring.max_llm_jobs_per_run)
