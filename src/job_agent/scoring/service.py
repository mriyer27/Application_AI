"""Scoring service: score new/changed jobs, optionally enriched by the local LLM.

Flow of one run:
  1. pick active, non-filtered jobs whose current score is missing or stale
     (content / profile / scoring-config hash or scoring version changed);
     ``rescore=True`` forces all of them;
  2. score every one with the rules engine and save it (fast, durable);
  3. if an ``analyzer`` is given, re-score only the top ``max_llm_jobs_per_run``
     of them (by rules score) with the validated ``JobAnalysis``. Analyzer
     failures are recorded and the rules score stays current.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Mapping

from pydantic import BaseModel, Field

from job_agent.database import repo
from job_agent.errors import JobAgentError, PermanentError
from job_agent.logging_config import get_logger
from job_agent.models import JobAnalysis, ScoreResult
from job_agent.scoring import repository as score_repo
from job_agent.scoring.engine import SCORING_VERSION, base_version, score_job, scoring_config_hash

if TYPE_CHECKING:  # pragma: no cover
    from job_agent.context import AppContext
    from job_agent.tasks import RunInfo

log = get_logger(__name__)

Analyzer = Callable[[Mapping[str, Any]], "JobAnalysis | None"]


class ScoringSummary(BaseModel):
    scored: int = 0  # jobs (re)scored in this run
    skipped_unchanged: int = 0  # jobs whose current score is still valid
    shortlisted: int = 0  # of the jobs scored in this run
    by_category: dict[str, int] = Field(default_factory=dict)  # of the jobs scored in this run
    errors: list[str] = Field(default_factory=list)
    llm_scored: int = 0  # jobs whose current score now includes local-LLM analysis
    llm_failed: int = 0  # analyzer raised or returned invalid data (rules score kept)
    llm_unavailable: int = 0  # analyzer returned None (no analysis; rules score kept)
    deferred: int = 0  # stale jobs left for a later run because of ``limit``


def _is_stale(current: Any, job: Mapping[str, Any], profile_hash: str, config_hash: str) -> bool:
    if current is None:
        return True
    return (
        base_version(current["scoring_version"]) != SCORING_VERSION
        or current["profile_hash"] != profile_hash
        or current["config_hash"] != config_hash
        or current["content_hash"] != job["content_hash"]
    )


def _candidates(ctx: "AppContext", job_ids: list[int] | None) -> list[dict[str, Any]]:
    db = ctx.db
    if job_ids:
        ids = sorted({int(i) for i in job_ids})
        marks = ",".join("?" for _ in ids)
        rows = db.query_all(f"SELECT * FROM jobs WHERE id IN ({marks}) ORDER BY discovered_at DESC, id DESC", ids)
        missing = set(ids) - {int(r["id"]) for r in rows}
        if missing:
            raise PermanentError(f"unknown job id(s): {', '.join(map(str, sorted(missing)))}")
    else:
        rows = db.query_all(
            "SELECT * FROM jobs WHERE listing_status = 'active' AND pipeline_state NOT IN ('filtered_out', 'closed') "
            "AND duplicate_of_job_id IS NULL ORDER BY discovered_at DESC, id DESC"
        )
    return [dict(r) for r in rows]


def score_jobs(ctx: "AppContext", *, rescore: bool = False, limit: int | None = None,
               job_ids: list[int] | None = None, analyzer: Analyzer | None = None,
               run_id: int | None = None) -> ScoringSummary:
    """Score jobs that need it. ``job_ids`` explicitly requests those jobs (even filtered-out ones)."""
    db = ctx.db
    config = ctx.config
    profile = ctx.profile  # ProfileError propagates: nothing can be scored without a verified profile
    profile_hash = profile.profile_hash()
    config_hash = scoring_config_hash(config)
    explicit = bool(job_ids)
    summary = ScoringSummary()

    current = score_repo.current_score_keys(db)
    todo: list[dict[str, Any]] = []
    for job in _candidates(ctx, job_ids):
        if not rescore and not _is_stale(current.get(int(job["id"])), job, profile_hash, config_hash):
            summary.skipped_unchanged += 1
            continue
        if limit is not None and len(todo) >= limit:
            summary.deferred += 1
            continue
        todo.append(job)

    final: dict[int, ScoreResult] = {}
    for job in todo:
        result = _score_and_save(ctx, job, None, profile_hash, config_hash, explicit, summary, run_id)
        if result is not None:
            final[int(job["id"])] = result

    budget = config.scoring.max_llm_jobs_per_run
    if analyzer is not None and budget > 0 and final:
        jobs_by_id = {int(j["id"]): j for j in todo}
        ranked = sorted(final.values(), key=lambda r: (-r.overall_score, r.job_id))[:budget]
        for rules_result in ranked:
            job = jobs_by_id[rules_result.job_id]
            analysis = _analyze(ctx, analyzer, job, summary, run_id)
            if analysis is None:
                continue
            result = _score_and_save(ctx, job, analysis, profile_hash, config_hash, explicit, summary, run_id)
            if result is not None:
                final[rules_result.job_id] = result
                summary.llm_scored += 1

    summary.scored = len(final)
    for result in final.values():
        summary.by_category[result.category.value] = summary.by_category.get(result.category.value, 0) + 1
        summary.shortlisted += int(result.shortlisted)
    summary.by_category = dict(sorted(summary.by_category.items()))
    if summary.scored or summary.errors:
        repo.audit(db, actor="system", action="jobs_scored", outcome="partial" if summary.errors else "success",
                   entity_type="job_scores", run_id=run_id,
                   details={k: v for k, v in summary.model_dump(mode="json").items() if k != "errors"}
                   | {"error_count": len(summary.errors), "rescore": rescore})
    log.info("scoring: %d scored, %d unchanged, %d shortlisted, %d errors", summary.scored,
             summary.skipped_unchanged, summary.shortlisted, len(summary.errors))
    return summary


def _score_and_save(ctx: "AppContext", job: dict[str, Any], analysis: JobAnalysis | None, profile_hash: str,
                    config_hash: str, explicit: bool, summary: ScoringSummary,
                    run_id: int | None) -> ScoreResult | None:
    try:
        result = score_job(job, ctx.profile, ctx.config, analysis)
        score_repo.save_score(ctx.db, job, result, profile_hash, config_hash, allow_filtered=explicit)
        return result
    except Exception as exc:  # one bad job must not stop the run; recorded, not swallowed
        repo.record_error(ctx.db, component="scoring", error=exc, task_name="score", run_id=run_id,
                          job_id=int(job["id"]))
        summary.errors.append(f"{job.get('job_uid')}: {type(exc).__name__}: {exc}")
        log.warning("scoring failed for %s: %s", job.get("job_uid"), exc)
        return None


def _analyze(ctx: "AppContext", analyzer: Analyzer, job: dict[str, Any], summary: ScoringSummary,
             run_id: int | None) -> JobAnalysis | None:
    try:
        raw = analyzer(dict(job))
        if raw is None:
            summary.llm_unavailable += 1
            return None
        return raw if isinstance(raw, JobAnalysis) else JobAnalysis.model_validate(raw)
    except Exception as exc:  # incl. pydantic ValidationError; recorded, the rules score stays current
        repo.record_error(ctx.db, component="scoring:llm", error=exc, task_name="score", run_id=run_id,
                          job_id=int(job["id"]))
        summary.llm_failed += 1
        summary.errors.append(f"{job.get('job_uid')}: local AI analysis failed ({type(exc).__name__}); "
                              "rules-based score kept")
        return None


def default_analyzer(ctx: "AppContext", *, run_id: int | None = None) -> Analyzer | None:
    """The local-LLM analyzer if enabled and available, else None (rules-only scoring)."""
    if not (ctx.config.scoring.use_llm and ctx.config.llm.enabled):
        return None
    try:
        from job_agent.llm.analysis import make_analyzer
    except ImportError as exc:
        log.info("local LLM analysis not installed (%s); rules-only scoring", exc)
        return None
    try:
        return make_analyzer(ctx)
    except JobAgentError as exc:
        repo.record_error(ctx.db, component="scoring:llm", error=exc, task_name="score", run_id=run_id)
        return None


def score_task(ctx: "AppContext", run: "RunInfo") -> dict[str, Any]:
    """``tasks.TASKS["score"]`` entry point."""
    analyzer = default_analyzer(ctx, run_id=run.run_id)
    summary = score_jobs(ctx, analyzer=analyzer, run_id=run.run_id)
    out: dict[str, Any] = summary.model_dump(mode="json")
    out["llm"] = "enabled" if analyzer is not None else "unavailable or disabled (rules-only scoring)"
    if summary.errors:
        out["partial"] = True
    return out
