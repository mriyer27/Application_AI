"""The recurring discovery cycle: discover -> (local AI) -> score -> draft documents.

Each step is isolated: a failing step is recorded and the cycle continues with
what it can do (e.g. scoring existing jobs when every source is down, or
rules-only scoring when Ollama is unavailable). Nothing here submits anything.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from job_agent.database import repo
from job_agent.errors import JobAgentError, RecoverableError
from job_agent.logging_config import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from job_agent.context import AppContext
    from job_agent.tasks import RunInfo

log = get_logger(__name__)


def _record(ctx: "AppContext", step: str, exc: BaseException, run: "RunInfo | None") -> str:
    repo.record_error(ctx.db, component=f"pipeline:{step}", error=exc, task_name="discover_and_score",
                      run_id=run.run_id if run else None)
    return f"{step}: {type(exc).__name__}: {exc}"


def run_cycle(ctx: "AppContext", run: "RunInfo | None" = None, *, discover: bool = True, use_llm: bool | None = None,
              auto_prepare: bool = True) -> dict[str, Any]:
    summary: dict[str, Any] = {"steps": {}, "errors": []}

    # 1. Discovery -------------------------------------------------------
    if discover:
        try:
            from job_agent.discovery.service import discover_jobs

            result = discover_jobs(ctx)
            summary["steps"]["discover"] = result.model_dump(mode="json")
            if result.partial:
                summary["partial"] = True
            attempted = [s for s in result.sources if not s.skipped]
            if attempted and all(s.error for s in attempted):
                summary["discovery_failed"] = "every job source failed"
        except Exception as exc:  # recorded; scoring of existing jobs still runs
            summary["errors"].append(_record(ctx, "discover", exc, run))
            summary["partial"] = True
            summary["discovery_failed"] = f"{type(exc).__name__}: {exc}"

    # 2. Local AI analyzer (optional) ------------------------------------
    analyzer = None
    want_llm = ctx.config.scoring.use_llm if use_llm is None else use_llm
    if want_llm and ctx.config.llm.enabled:
        try:
            from job_agent.llm.analysis import make_analyzer

            analyzer = make_analyzer(ctx)
        except Exception as exc:
            summary["errors"].append(_record(ctx, "llm", exc, run))
    summary["steps"]["llm"] = "enabled" if analyzer else "unavailable or disabled (rules-only scoring)"

    # 3. Scoring ---------------------------------------------------------
    try:
        from job_agent.scoring.service import score_jobs

        scored = score_jobs(ctx, analyzer=analyzer)
        summary["steps"]["score"] = scored.model_dump(mode="json")
    except Exception as exc:
        summary["errors"].append(_record(ctx, "score", exc, run))
        summary["partial"] = True
        return summary
    finally:
        if analyzer is not None:
            summary["steps"]["llm_stats"] = analyzer.stats()
            analyzer.close()

    # 4. Draft documents for top new shortlisted jobs (rules-only, no LLM) --
    top_n = ctx.config.documents.auto_prepare_top_n if auto_prepare else 0
    if top_n:
        summary["steps"]["documents"] = _auto_prepare(ctx, top_n, run, summary)
    return summary


def _auto_prepare(ctx: "AppContext", top_n: int, run: "RunInfo | None", summary: dict[str, Any]) -> dict[str, Any]:
    from job_agent.models import Permission
    from job_agent.security.permissions import is_allowed

    if not is_allowed(ctx.config, Permission.GENERATE_DOCUMENTS):
        return {"skipped": "permissions.generate_documents is false"}
    rows = ctx.db.query_all(
        "SELECT j.id, j.job_uid FROM jobs j JOIN job_scores s ON s.job_id = j.id AND s.is_current = 1 "
        "LEFT JOIN applications a ON a.job_id = j.id "
        "WHERE j.pipeline_state = 'shortlisted' AND j.listing_status = 'active' AND a.id IS NULL "
        "AND (j.user_relevance IS NULL OR j.user_relevance != 'irrelevant') "
        "ORDER BY s.overall_score DESC, j.discovered_at DESC LIMIT ?",
        (top_n,),
    )
    prepared, failed = [], []
    try:
        from job_agent.resume.service import prepare_documents
    except ImportError as exc:
        return {"skipped": f"document generation unavailable: {exc}"}
    for row in rows:
        try:
            prepare_documents(ctx, int(row["id"]), use_llm=False)
            prepared.append(row["job_uid"])
        except JobAgentError as exc:
            failed.append(row["job_uid"])
            summary["errors"].append(_record(ctx, f"documents:{row['job_uid']}", exc, run))
    return {"prepared": prepared, "failed": failed}


def discover_and_score_task(ctx: "AppContext", run: "RunInfo") -> dict[str, Any]:
    summary = run_cycle(ctx, run)
    if summary.get("discovery_failed"):
        # Existing jobs were still scored, but a cycle that fetched nothing (e.g. the
        # laptop was offline at wake) must count as a failure so it is retried and
        # caught up instead of being recorded as a successful run.
        raise RecoverableError(f"discovery failed ({summary['discovery_failed']}); existing jobs were re-scored, "
                               "the run will be retried", human_action=None)
    return summary
