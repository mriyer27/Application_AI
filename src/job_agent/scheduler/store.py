"""Scheduler persistence: daemon identity/heartbeat (``agent_state``), the schedule
columns of ``scheduled_tasks``, catch-up markers and stale-lock recovery.

Lock acquisition and run results stay in ``database.repo`` / ``tasks.run_task``;
this module only owns the schedule-related columns (``schedule``, ``enabled``,
``next_run_at``) and the ``daemon_*`` / ``scheduler_*`` state keys.
"""

from __future__ import annotations

import os
import socket
from datetime import datetime
from typing import Any

from job_agent.database import repo
from job_agent.database.connection import Database
from job_agent.models import ErrorKind
from job_agent.security.redaction import redact_text
from job_agent.utils.time import parse_iso, utcnow, utcnow_iso

# agent_state keys (health.py and the dashboard read daemon_heartbeat_at).
STATE_PID = "daemon_pid"
STATE_HOST = "daemon_host"
STATE_LOCK_PATH = "daemon_lock_path"
STATE_STARTED_AT = "daemon_started_at"
STATE_HEARTBEAT_AT = "daemon_heartbeat_at"
STATE_STOPPED_AT = "daemon_stopped_at"
STATE_INTERRUPTED_AT_START = "daemon_interrupted_runs_at_start"
_CATCH_UP_PREFIX = "scheduler_catch_up:"

#: A heartbeat younger than this means the daemon is running (same rule as health.py).
HEARTBEAT_STALE_SECONDS = 180


# --------------------------------------------------------------------------- #
# Daemon identity / heartbeat
# --------------------------------------------------------------------------- #
def write_daemon_started(db: Database, *, pid: int, host: str, lock_path: str, interrupted_runs: int) -> str:
    now = utcnow_iso()
    with db.transaction():
        repo.set_state(db, STATE_PID, str(pid))
        repo.set_state(db, STATE_HOST, host)
        repo.set_state(db, STATE_LOCK_PATH, lock_path)
        repo.set_state(db, STATE_STARTED_AT, now)
        repo.set_state(db, STATE_HEARTBEAT_AT, now)
        repo.set_state(db, STATE_STOPPED_AT, None)
        repo.set_state(db, STATE_INTERRUPTED_AT_START, str(interrupted_runs))
    return now


def beat(db: Database) -> str:
    now = utcnow_iso()
    repo.set_state(db, STATE_HEARTBEAT_AT, now)
    return now


def write_daemon_stopped(db: Database) -> str:
    """Clean stop: record the time and clear the heartbeat so health checks see
    "not running" immediately instead of after the 180 s staleness window."""
    now = utcnow_iso()
    with db.transaction():
        repo.set_state(db, STATE_STOPPED_AT, now)
        repo.set_state(db, STATE_HEARTBEAT_AT, None)
        clear_next_runs(db)
    return now


def daemon_state(heartbeat: datetime | None, stopped: datetime | None,
                 now: datetime | None = None) -> tuple[str, int | None]:
    """``("running"|"stopped"|"stale", heartbeat age in seconds)``.

    ``stale`` = a heartbeat exists but is old: the daemon was killed, crashed, or
    the laptop is/was asleep.
    """
    if heartbeat is None:
        return "stopped", None
    age = max(0, int(((now or utcnow()) - heartbeat).total_seconds()))
    if stopped is not None and stopped >= heartbeat:
        return "stopped", age
    return ("running" if age < HEARTBEAT_STALE_SECONDS else "stale"), age


def recorded_daemon(db: Database) -> dict[str, Any]:
    heartbeat = parse_iso(repo.get_state(db, STATE_HEARTBEAT_AT))
    stopped = parse_iso(repo.get_state(db, STATE_STOPPED_AT))
    state, age = daemon_state(heartbeat, stopped)
    pid = repo.get_state(db, STATE_PID)
    return {
        "state": state,
        "heartbeat_at": repo.get_state(db, STATE_HEARTBEAT_AT),
        "heartbeat_age_seconds": age,
        "started_at": repo.get_state(db, STATE_STARTED_AT),
        "stopped_at": repo.get_state(db, STATE_STOPPED_AT),
        "pid": int(pid) if pid and pid.isdigit() else None,
        "host": repo.get_state(db, STATE_HOST),
        "lock_path": repo.get_state(db, STATE_LOCK_PATH),
    }


# --------------------------------------------------------------------------- #
# scheduled_tasks schedule columns
# --------------------------------------------------------------------------- #
def save_schedule(db: Database, task_name: str, *, schedule: str | None, enabled: bool,
                  next_run_at: str | None) -> None:
    now = utcnow_iso()
    db.execute(
        "INSERT INTO scheduled_tasks (task_name, schedule, enabled, next_run_at, updated_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(task_name) DO UPDATE SET schedule = excluded.schedule, enabled = excluded.enabled, "
        "next_run_at = excluded.next_run_at, updated_at = excluded.updated_at",
        (task_name, schedule, int(enabled), next_run_at, now),
    )


def set_next_run(db: Database, task_name: str, next_run_at: str | None) -> None:
    db.execute("UPDATE scheduled_tasks SET next_run_at = ?, updated_at = ? WHERE task_name = ?",
               (next_run_at, utcnow_iso(), task_name))


def clear_next_runs(db: Database) -> None:
    db.execute("UPDATE scheduled_tasks SET next_run_at = NULL, updated_at = ? WHERE next_run_at IS NOT NULL",
               (utcnow_iso(),))


def unschedule_others(db: Database, keep: set[str]) -> None:
    """Rows for tasks no longer in the config (or only ever run manually) are not scheduled."""
    rows = db.query_all("SELECT task_name FROM scheduled_tasks")
    now = utcnow_iso()
    for row in rows:
        if row["task_name"] not in keep:
            db.execute("UPDATE scheduled_tasks SET schedule = NULL, enabled = 0, next_run_at = NULL, updated_at = ? "
                       "WHERE task_name = ?", (now, row["task_name"]))


def last_success(db: Database, task_name: str) -> datetime | None:
    return parse_iso(db.scalar("SELECT last_success_at FROM scheduled_tasks WHERE task_name = ?", (task_name,)))


# --------------------------------------------------------------------------- #
# Recovery
# --------------------------------------------------------------------------- #
def pid_alive(pid: int) -> bool:
    # repo owns the cross-platform liveness check (Windows-safe; os.kill(pid, 0) is not).
    return repo._pid_alive(pid)  # noqa: SLF001 - see foundation_change_requests: make it public


def release_dead_task_locks(db: Database) -> list[str]:
    """Release task locks held by processes on this host that no longer exist.

    ``tasks.run_task`` uses lock owners of the form ``host:pid:task``. Without
    this, a hard-killed run would block its task until the lock TTL expires.
    """
    host = socket.gethostname()
    released: list[str] = []
    rows = db.query_all("SELECT task_name, lock_owner FROM scheduled_tasks WHERE lock_owner IS NOT NULL")
    for row in rows:
        parts = str(row["lock_owner"]).rsplit(":", 2)
        if len(parts) != 3 or not parts[1].isdigit():
            continue
        owner_host, pid = parts[0], int(parts[1])
        if owner_host != host or pid == os.getpid() or pid_alive(pid):
            continue
        repo.release_task_lock(db, row["task_name"], row["lock_owner"])
        repo.audit(db, actor="scheduler", action="task_lock_released", entity_type="task",
                   entity_id=row["task_name"], outcome="success",
                   details={"reason": "lock owner process no longer exists", "pid": pid})
        released.append(row["task_name"])
    return released


# --------------------------------------------------------------------------- #
# Catch-up markers
# --------------------------------------------------------------------------- #
def get_catch_up_marker(db: Database, task_name: str) -> str | None:
    return repo.get_state(db, _CATCH_UP_PREFIX + task_name)


def set_catch_up_marker(db: Database, task_name: str, marker: str) -> None:
    repo.set_state(db, _CATCH_UP_PREFIX + task_name, marker)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
def record_scheduler_problem(db: Database, *, message: str, task_name: str | None = None,
                             kind: ErrorKind = ErrorKind.HUMAN_ACTION_REQUIRED,
                             human_action: str | None = None) -> int | None:
    """Record a scheduler/config problem once (no duplicate while an identical one is unresolved)."""
    stored = redact_text(message, pii=True)[:2000]
    existing = db.scalar("SELECT id FROM errors WHERE resolved = 0 AND component = 'scheduler' AND message = ?",
                         (stored,))
    if existing is not None:
        return None
    return repo.record_error(db, component="scheduler", error=message, kind=kind, task_name=task_name,
                             human_action=human_action, retryable=False)
