"""Named, predefined tasks and a safe runner.

The scheduler, CLI and dashboard can only run tasks listed in ``TASKS`` -- there
is no way to run arbitrary code or shell commands. Each task:

* is gated by the pause flag (except when ``trigger="manual"``),
* takes a cross-process lock so it never overlaps with itself,
* records an ``agent_runs`` row (start / finish / status / summary),
* converts exceptions into classified ``errors`` rows instead of crashing.

Task implementations have the signature ``fn(ctx: AppContext, run: RunInfo) -> dict``
and return a JSON-serializable summary. Returning ``{"partial": True, ...}``
marks the run as partially successful (e.g. one job source failed).
"""

from __future__ import annotations

import importlib
import os
import socket
from dataclasses import dataclass, field
from typing import Any, Callable

from job_agent.context import AppContext
from job_agent.database import repo
from job_agent.errors import AutomationPaused, JobAgentError, TaskLocked
from job_agent.logging_config import get_logger
from job_agent.models import ErrorKind, RunStatus

log = get_logger(__name__)


@dataclass(frozen=True)
class TaskSpec:
    name: str
    target: str  # "module.path:function"
    description: str
    #: Safe to retry automatically after a recoverable failure.
    retry_safe: bool = True
    #: May be triggered from the dashboard.
    dashboard_allowed: bool = True


@dataclass
class RunInfo:
    run_id: int
    run_uid: str
    task_name: str
    trigger: str
    attempt: int = 1
    notes: list[str] = field(default_factory=list)


@dataclass
class TaskResult:
    task_name: str
    status: RunStatus
    run_uid: str | None
    summary: dict[str, Any]
    error: str | None = None
    error_kind: ErrorKind | None = None


#: Registry of every task the agent can run. Add new tasks here only.
TASKS: dict[str, TaskSpec] = {
    "health": TaskSpec("health", "job_agent.health:health_task", "Run system health checks"),
    "discover": TaskSpec("discover", "job_agent.discovery.service:discover_task",
                         "Fetch jobs from configured public job boards"),
    "score": TaskSpec("score", "job_agent.scoring.service:score_task", "Score and rank unscored/changed jobs"),
    "discover_and_score": TaskSpec("discover_and_score", "job_agent.pipeline:discover_and_score_task",
                                   "Discover, score, analyze top matches with the local LLM"),
    "email_sync": TaskSpec("email_sync", "job_agent.email.service:email_sync_task",
                           "Read and categorize recent Gmail messages (read-only)"),
    "followups": TaskSpec("followups", "job_agent.followups.service:followups_task",
                          "Review applications and messages for follow-ups; draft suggestions"),
    "report": TaskSpec("report", "job_agent.reporting.service:report_task",
                       "Generate today's Excel report and Markdown summary"),
}


def register_task(spec: TaskSpec) -> None:
    TASKS[spec.name] = spec


def _resolve(target: str) -> Callable[[AppContext, RunInfo], dict[str, Any]]:
    module_name, _, func_name = target.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, func_name)


def run_task(ctx: AppContext, name: str, *, trigger: str = "manual", attempt: int = 1,
             ignore_pause: bool | None = None) -> TaskResult:
    if name not in TASKS:
        raise JobAgentError(f"unknown task {name!r}; known tasks: {', '.join(sorted(TASKS))}")
    spec = TASKS[name]
    db = ctx.db
    if ignore_pause is None:
        ignore_pause = trigger == "manual"
    if not ignore_pause and repo.is_paused(db):
        log.info("task %s skipped: automation paused", name)
        repo.record_task_result(db, name, RunStatus.SKIPPED)
        return TaskResult(name, RunStatus.SKIPPED, None, {"reason": "automation paused"},
                          error=AutomationPaused.human_action, error_kind=ErrorKind.HUMAN_ACTION_REQUIRED)

    owner = f"{socket.gethostname()}:{os.getpid()}:{name}"
    if not repo.acquire_task_lock(db, name, owner, ctx.config.scheduler.lock_timeout_minutes):
        log.info("task %s skipped: already running", name)
        return TaskResult(name, RunStatus.SKIPPED, None, {"reason": "another run of this task is in progress"},
                          error=str(TaskLocked("locked")), error_kind=ErrorKind.RECOVERABLE)

    run_id, run_uid = repo.start_run(db, name, trigger, attempt)
    info = RunInfo(run_id=run_id, run_uid=run_uid, task_name=name, trigger=trigger, attempt=attempt)
    log.info("task %s started (run %s, trigger=%s)", name, run_uid, trigger)
    try:
        func = _resolve(spec.target)
        summary = func(ctx, info) or {}
        status = RunStatus.PARTIAL if summary.get("partial") else RunStatus.SUCCEEDED
        repo.finish_run(db, run_id, status, summary=summary)
        repo.record_task_result(db, name, status)
        if status == RunStatus.SUCCEEDED or status == RunStatus.PARTIAL:
            repo.set_state(db, "last_successful_run_at", summary.get("finished_at") or _now())
        log.info("task %s finished: %s", name, status.value)
        return TaskResult(name, status, run_uid, summary)
    except KeyboardInterrupt:
        repo.finish_run(db, run_id, RunStatus.INTERRUPTED, error_kind=ErrorKind.UNKNOWN_OUTCOME,
                        error_message="interrupted by user")
        repo.record_task_result(db, name, RunStatus.INTERRUPTED)
        raise
    except Exception as exc:  # classified and recorded, never swallowed
        kind = exc.kind if isinstance(exc, JobAgentError) else ErrorKind.PERMANENT
        if isinstance(exc, ImportError):
            message = f"task {name} is not available in this installation: {exc}"
        else:
            message = str(exc)
        log.exception("task %s failed: %s", name, message)
        repo.record_error(db, component=f"task:{name}", error=exc, kind=kind, task_name=name, run_id=run_id,
                          retryable=spec.retry_safe and (kind == ErrorKind.RECOVERABLE))
        repo.finish_run(db, run_id, RunStatus.FAILED, error_kind=kind, error_message=message)
        repo.record_task_result(db, name, RunStatus.FAILED)
        return TaskResult(name, RunStatus.FAILED, run_uid, {}, error=message, error_kind=kind)
    finally:
        repo.release_task_lock(db, name, owner)


def _now() -> str:
    from job_agent.utils.time import utcnow_iso

    return utcnow_iso()
