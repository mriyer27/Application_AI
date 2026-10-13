"""Bounded, classified retries around ``tasks.run_task``.

Only a FAILED run whose error is RECOVERABLE (network blip, timeout, 5xx) and
whose task is registered as ``retry_safe`` is retried. Skipped runs (paused,
another run holds the lock), permanent errors, missing authorization, anything
needing a human, and unknown outcomes are never retried automatically.
"""

from __future__ import annotations

import threading

from job_agent import tasks
from job_agent.context import AppContext
from job_agent.logging_config import get_logger
from job_agent.models import ErrorKind, RunStatus
from job_agent.tasks import TaskResult

log = get_logger(__name__)


def should_retry(result: TaskResult) -> bool:
    spec = tasks.TASKS.get(result.task_name)
    return (result.status == RunStatus.FAILED and result.error_kind == ErrorKind.RECOVERABLE
            and spec is not None and spec.retry_safe)


def retry_delay(backoff: float, retry_number: int) -> float:
    """Exponential backoff: ``backoff * 2**(n-1)`` for the n-th retry (n >= 1)."""
    return float(backoff) * 2 ** (retry_number - 1)


def execute_with_retries(ctx: AppContext, task_name: str, *, stop_event: threading.Event | None = None,
                         max_retries: int | None = None, backoff: float | None = None,
                         trigger: str = "scheduled") -> TaskResult:
    """Run a task via ``tasks.run_task`` and retry recoverable failures.

    Waits between attempts use ``stop_event.wait()`` so a shutdown request
    interrupts the backoff immediately (the last result is returned).
    Raises only what ``run_task`` raises before a run starts (unknown task name,
    database unavailable); task failures come back as a FAILED ``TaskResult``.
    """
    cfg = ctx.config.scheduler
    max_retries = cfg.max_retries if max_retries is None else max_retries
    backoff = cfg.retry_backoff_seconds if backoff is None else backoff
    stop = stop_event or threading.Event()

    attempt = 1
    result = tasks.run_task(ctx, task_name, trigger=trigger, attempt=attempt)
    while should_retry(result) and attempt <= max_retries:
        delay = retry_delay(backoff, attempt)
        log.warning("task %s failed (recoverable, attempt %d/%d): %s; retrying in %.0f s",
                    task_name, attempt, max_retries + 1, result.error, delay)
        if stop.wait(delay):
            log.info("retry of %s cancelled: shutdown requested", task_name)
            break
        attempt += 1
        result = tasks.run_task(ctx, task_name, trigger=trigger, attempt=attempt)
    if result.status == RunStatus.FAILED:
        log.warning("task %s failed after %d attempt(s): %s (%s)", task_name, attempt, result.error,
                    result.error_kind.value if result.error_kind else "unknown")
    return result
