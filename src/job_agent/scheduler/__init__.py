"""Recurring scheduler: daemon, retries, catch-up after downtime, status and previews.

Public entry points (see ``docs/scheduling.md``):

* ``daemon.run_daemon(ctx, ...)`` -- foreground daemon (``job-agent daemon``)
* ``daemon.run_once(ctx, ...)`` -- every enabled task once (``job-agent daemon --once``)
* ``runner.execute_with_retries(ctx, task_name, ...)`` -- classified, bounded retries
* ``status.scheduler_status(db, config)`` / ``status.preview_schedule(config, ...)``
"""

from __future__ import annotations

from job_agent.scheduler.daemon import DaemonStartup, run_daemon, run_once, run_scheduled_task
from job_agent.scheduler.lock import DaemonAlreadyRunning
from job_agent.scheduler.runner import execute_with_retries, should_retry
from job_agent.scheduler.status import preview_schedule, scheduler_status

__all__ = [
    "DaemonAlreadyRunning", "DaemonStartup", "execute_with_retries", "preview_schedule", "run_daemon", "run_once",
    "run_scheduled_task", "scheduler_status", "should_retry",
]
