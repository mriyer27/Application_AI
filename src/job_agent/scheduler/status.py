"""Scheduler status and schedule previews (no running daemon needed).

Used by ``job-agent schedule`` and intended for the dashboard. All timestamps
in the returned dicts are UTC ISO-8601 strings; convert for display with
``utils.time.to_local``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from job_agent import tasks
from job_agent.config import AppConfig
from job_agent.database import repo
from job_agent.database.connection import Database
from job_agent.errors import ConfigError
from job_agent.scheduler import store
from job_agent.scheduler.triggers import build_trigger, next_fire_times, schedule_text
from job_agent.utils.time import parse_iso, to_iso, utcnow


def _schedulable(name: str, config: AppConfig) -> bool:
    entry = config.scheduler.tasks.get(name)
    if entry is None or not entry.enabled or name not in tasks.TASKS:
        return False
    try:
        build_trigger(name, entry, ZoneInfo(config.app.timezone))
    except ConfigError:
        return False
    return True


def scheduler_status(db: Database, config: AppConfig) -> dict[str, Any]:
    """Daemon state, pause flag and per-task schedule/run state.

    ``daemon.state`` is ``running`` (heartbeat < 180 s old), ``stopped`` (clean
    stop or never started) or ``stale`` (heartbeat old: killed, crashed, or the
    laptop is asleep). ``next_run_at`` is only reported while the daemon runs.
    """
    now = utcnow()
    daemon = store.recorded_daemon(db)
    running = daemon["state"] == "running"
    paused = repo.is_paused(db)
    rows = {r["task_name"]: r for r in db.query_all("SELECT * FROM scheduled_tasks")}
    names = list(config.scheduler.tasks) + sorted(n for n in rows if n not in config.scheduler.tasks)

    out: dict[str, dict[str, Any]] = {}
    for name in names:
        entry = config.scheduler.tasks.get(name)
        row = rows.get(name)
        config_text = schedule_text(entry) if entry is not None else None
        daemon_text = row["schedule"] if row is not None else None
        lock_owner = row["lock_owner"] if row is not None else None
        lock_expires = parse_iso(row["lock_expires_at"]) if row is not None else None
        expected = _schedulable(name, config)
        scheduled_by_daemon = bool(row is not None and row["enabled"] and row["next_run_at"])
        last_error = db.query_one(
            "SELECT occurred_at, error_kind, message, human_action FROM errors WHERE task_name = ? AND resolved = 0 "
            "ORDER BY id DESC LIMIT 1", (name,))
        out[name] = {
            "schedule": config_text or daemon_text,
            "enabled": bool(entry.enabled) if entry is not None else False,
            "in_config": entry is not None,
            "known_task": name in tasks.TASKS,
            "last_run_at": row["last_run_at"] if row is not None else None,
            "last_status": row["last_status"] if row is not None else None,
            "last_success_at": row["last_success_at"] if row is not None else None,
            "next_run_at": row["next_run_at"] if row is not None and running else None,
            "consecutive_failures": int(row["consecutive_failures"]) if row is not None else 0,
            "lock_held": bool(lock_owner) and (lock_expires is None or lock_expires > now),
            "lock_owner": lock_owner,
            "lock_expires_at": row["lock_expires_at"] if row is not None else None,
            # The running daemon was started with a different schedule than the config now says.
            "restart_needed": running and entry is not None and (
                scheduled_by_daemon != expected or (expected and daemon_text != config_text)),
            "last_error": dict(last_error) if last_error is not None else None,
        }
    return {
        "generated_at": to_iso(now),
        "timezone": config.app.timezone,
        "scheduler_enabled": config.scheduler.enabled,
        "daemon": daemon,
        "paused": paused,
        "paused_reason": repo.get_state(db, "automation_paused_reason") if paused else None,
        "tasks": out,
    }


def preview_schedule(config: AppConfig, now: datetime | None = None, count: int = 3) -> dict[str, list[str]]:
    """Upcoming fire times (UTC ISO) for each enabled, known task, computed from config alone.

    Interval tasks are previewed as if the daemon started at ``now``. A task with
    an invalid cron expression maps to an empty list.
    """
    tz = ZoneInfo(config.app.timezone)
    ref = now or utcnow()
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    ref = ref.astimezone(tz)
    preview: dict[str, list[str]] = {}
    for name, entry in config.scheduler.tasks.items():
        if not entry.enabled or name not in tasks.TASKS:
            continue
        try:
            trigger = build_trigger(name, entry, tz, now=ref)
        except ConfigError:
            preview[name] = []
            continue
        preview[name] = [iso for iso in (to_iso(t) for t in next_fire_times(trigger, ref, count)) if iso]
    return preview
