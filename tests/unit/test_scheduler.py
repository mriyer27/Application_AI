"""Scheduler unit tests: triggers, previews, retries, catch-up detection, locks, status."""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import types
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from job_agent import tasks
from job_agent.config import ScheduleEntry
from job_agent.database import repo
from job_agent.errors import (
    AuthorizationRequired,
    ConfigError,
    HumanActionRequired,
    PermanentError,
    RecoverableError,
    UnknownOutcome,
)
from job_agent.models import ErrorKind, RunStatus
from job_agent.scheduler import store
from job_agent.scheduler.lock import DaemonAlreadyRunning, InstanceLock
from job_agent.scheduler.runner import execute_with_retries, retry_delay
from job_agent.scheduler.status import preview_schedule, scheduler_status
from job_agent.scheduler.triggers import (
    build_trigger,
    crontab_day_of_week,
    missed_run_marker,
    most_recent_fire_time,
    next_fire_times,
)
from job_agent.utils.time import to_iso, utcnow

NY = ZoneInfo("America/New_York")


# --------------------------------------------------------------------------- #
# Fake tasks (registered in tasks.TASKS, implemented in a fake module)
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake(monkeypatch):
    state = types.SimpleNamespace(calls=[], flaky_failures=2)

    def ok(ctx, run):
        state.calls.append((run.task_name, run.attempt, run.trigger))
        return {"ok": True}

    def flaky(ctx, run):
        state.calls.append((run.task_name, run.attempt, run.trigger))
        if run.attempt <= state.flaky_failures:
            raise RecoverableError("temporary network failure")
        return {"ok": True}

    def make_raiser(exc_type):
        def _fn(ctx, run):
            state.calls.append((run.task_name, run.attempt, run.trigger))
            raise exc_type(f"{exc_type.__name__} happened")
        return _fn

    mod = types.ModuleType("fake_sched_tasks")
    mod.ok, mod.flaky = ok, flaky
    raisers = {"recoverable": RecoverableError, "permanent": PermanentError, "auth": AuthorizationRequired,
               "human": HumanActionRequired, "unknown": UnknownOutcome}
    for key, exc in raisers.items():
        setattr(mod, key, make_raiser(exc))
    monkeypatch.setitem(sys.modules, "fake_sched_tasks", mod)
    for name in ("ok", "flaky", *raisers):
        monkeypatch.setitem(tasks.TASKS, f"t_{name}", tasks.TaskSpec(f"t_{name}", f"fake_sched_tasks:{name}", name))
    monkeypatch.setitem(tasks.TASKS, "t_unsafe", tasks.TaskSpec("t_unsafe", "fake_sched_tasks:recoverable",
                                                                "not retry safe", retry_safe=False))
    return state


def _runs(db, name):
    return db.query_all("SELECT * FROM agent_runs WHERE task_name = ? ORDER BY id", (name,))


class RecordingEvent(threading.Event):
    """Records backoff waits instead of sleeping."""

    def __init__(self):
        super().__init__()
        self.waits: list[float] = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self.is_set()


# --------------------------------------------------------------------------- #
# Triggers
# --------------------------------------------------------------------------- #
def test_crontab_day_of_week_uses_standard_numbering():
    assert crontab_day_of_week("*") == "*"
    assert crontab_day_of_week("1-5") == "mon,tue,wed,thu,fri"
    assert crontab_day_of_week("0") == "sun" and crontab_day_of_week("7") == "sun"
    assert crontab_day_of_week("sat,sun") == "sun,sat"
    assert crontab_day_of_week("mon-sun") == "sun,mon,tue,wed,thu,fri,sat"
    assert crontab_day_of_week("*/2") == "sun,tue,thu,sat"
    with pytest.raises(ValueError):
        crontab_day_of_week("8")
    with pytest.raises(ValueError):
        crontab_day_of_week("fri-mon")


def test_cron_trigger_fires_in_configured_timezone():
    trigger = build_trigger("discover_and_score", ScheduleEntry(cron="15 7,16 * * *"), NY)
    summer = trigger.get_next_fire_time(None, datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc))
    assert summer.astimezone(NY).strftime("%H:%M") == "07:15"
    assert summer.astimezone(timezone.utc).hour == 11  # EDT = UTC-4
    winter = trigger.get_next_fire_time(None, datetime(2026, 1, 12, 13, 0, tzinfo=timezone.utc))
    assert winter.astimezone(NY).strftime("%H:%M") == "16:15"
    assert winter.astimezone(timezone.utc).hour == 21  # EST = UTC-5


def test_cron_weekdays_follow_crontab_meaning():
    trigger = build_trigger("report", ScheduleEntry(cron="0 9 * * 1-5"), NY)
    saturday = datetime(2026, 10, 10, 12, 0, tzinfo=NY)
    nxt = trigger.get_next_fire_time(None, saturday)
    assert nxt.strftime("%a %H:%M") == "Mon 09:00"  # APScheduler's own numbering would give Tuesday


def test_interval_trigger_and_fire_times():
    now = datetime(2026, 10, 12, 8, 0, tzinfo=NY)
    trigger = build_trigger("health", ScheduleEntry(interval_minutes=60), NY, now=now)
    times = next_fire_times(trigger, now, 3)
    assert [t - now for t in times] == [timedelta(minutes=60), timedelta(minutes=120), timedelta(minutes=180)]


def test_invalid_cron_is_a_config_error():
    with pytest.raises(ConfigError, match="scheduler.tasks.report.cron"):
        build_trigger("report", ScheduleEntry(cron="61 * * * *"), NY)


def test_most_recent_fire_time_and_missed_marker():
    entry = ScheduleEntry(cron="30 21 * * *")
    trigger = build_trigger("report", entry, NY)
    now = datetime(2026, 10, 12, 10, 0, tzinfo=NY)
    assert most_recent_fire_time(trigger, now) == datetime(2026, 10, 11, 21, 30, tzinfo=NY)
    nxt = trigger.get_next_fire_time(None, now)
    marker = missed_run_marker(entry, trigger, last_success=None, now=now, next_run=nxt)
    assert marker == to_iso(datetime(2026, 10, 11, 21, 30, tzinfo=NY))
    old = datetime(2026, 10, 10, 21, 31, tzinfo=NY)
    assert missed_run_marker(entry, trigger, last_success=old, now=now, next_run=nxt) == marker
    fresh = datetime(2026, 10, 11, 21, 31, tzinfo=NY)
    assert missed_run_marker(entry, trigger, last_success=fresh, now=now, next_run=nxt) is None
    # the regular run is about to happen anyway -> no catch-up
    soon = datetime(2026, 10, 12, 21, 20, tzinfo=NY)
    assert missed_run_marker(entry, trigger, last_success=old, now=soon,
                             next_run=trigger.get_next_fire_time(None, soon)) is None
    weekly = ScheduleEntry(cron="0 9 * * 1")
    assert most_recent_fire_time(build_trigger("x", weekly, NY), now) == datetime(2026, 10, 12, 9, 0, tzinfo=NY)


def test_missed_marker_for_interval_tasks():
    entry = ScheduleEntry(interval_minutes=60)
    now = datetime(2026, 10, 12, 10, 0, tzinfo=NY)
    trigger = build_trigger("health", entry, NY, now=now)
    assert missed_run_marker(entry, trigger, last_success=None, now=now) == "never-succeeded"
    assert missed_run_marker(entry, trigger, last_success=now - timedelta(hours=3), now=now) is not None
    assert missed_run_marker(entry, trigger, last_success=now - timedelta(minutes=10), now=now) is None


def test_preview_schedule(config):
    now = datetime(2026, 10, 12, 12, 1, tzinfo=timezone.utc)  # 08:01 in New York
    preview = preview_schedule(config, now=now, count=3)
    assert "email_sync" not in preview  # disabled in the example config
    assert preview["discover_and_score"] == ["2026-10-12T20:15:00+00:00", "2026-10-13T11:15:00+00:00",
                                             "2026-10-13T20:15:00+00:00"]
    assert preview["followups"][0] == "2026-10-13T12:00:00+00:00"
    assert preview["health"] == ["2026-10-12T13:01:00+00:00", "2026-10-12T14:01:00+00:00",
                                 "2026-10-12T15:01:00+00:00"]
    config.scheduler.tasks["report"] = ScheduleEntry(cron="99 * * * *")
    config.scheduler.tasks["not_a_task"] = ScheduleEntry(interval_minutes=10)
    preview = preview_schedule(config, now=now, count=1)
    assert preview["report"] == [] and "not_a_task" not in preview


# --------------------------------------------------------------------------- #
# Retries
# --------------------------------------------------------------------------- #
def test_recoverable_failure_is_retried_until_success(ctx, fake):
    result = execute_with_retries(ctx, "t_flaky", stop_event=threading.Event(), max_retries=2, backoff=0.01)
    assert result.status == RunStatus.SUCCEEDED
    runs = _runs(ctx.db, "t_flaky")
    assert [r["attempt"] for r in runs] == [1, 2, 3]
    assert [r["status"] for r in runs] == ["failed", "failed", "succeeded"]
    assert {r["trigger"] for r in runs} == {"scheduled"}


def test_retries_are_bounded_and_back_off_exponentially(ctx, fake):
    stop = RecordingEvent()
    result = execute_with_retries(ctx, "t_recoverable", stop_event=stop, max_retries=2, backoff=10)
    assert result.status == RunStatus.FAILED and result.error_kind == ErrorKind.RECOVERABLE
    assert len(_runs(ctx.db, "t_recoverable")) == 3
    assert stop.waits == [10, 20]
    assert retry_delay(120, 1) == 120 and retry_delay(120, 3) == 480


@pytest.mark.parametrize("name, kind", [("t_permanent", ErrorKind.PERMANENT),
                                        ("t_auth", ErrorKind.AUTHORIZATION_REQUIRED),
                                        ("t_human", ErrorKind.HUMAN_ACTION_REQUIRED),
                                        ("t_unknown", ErrorKind.UNKNOWN_OUTCOME)])
def test_non_recoverable_failures_are_never_retried(ctx, fake, name, kind):
    result = execute_with_retries(ctx, name, stop_event=RecordingEvent(), max_retries=3, backoff=0.01)
    assert result.status == RunStatus.FAILED and result.error_kind == kind
    assert len(_runs(ctx.db, name)) == 1


def test_task_not_marked_retry_safe_is_not_retried(ctx, fake):
    result = execute_with_retries(ctx, "t_unsafe", stop_event=RecordingEvent(), max_retries=3, backoff=0.01)
    assert result.status == RunStatus.FAILED and len(_runs(ctx.db, "t_unsafe")) == 1


def test_shutdown_interrupts_retry_backoff(ctx, fake):
    stop = threading.Event()
    stop.set()
    result = execute_with_retries(ctx, "t_recoverable", stop_event=stop, max_retries=3, backoff=3600)
    assert result.status == RunStatus.FAILED and len(_runs(ctx.db, "t_recoverable")) == 1


def test_paused_run_is_skipped_and_not_retried(ctx, fake):
    repo.set_paused(ctx.db, True, actor="cli", reason="vacation")
    result = execute_with_retries(ctx, "t_ok", stop_event=RecordingEvent(), max_retries=3, backoff=0.01)
    assert result.status == RunStatus.SKIPPED and fake.calls == []


def test_overlap_lock_held_by_other_owner_is_skipped_without_retry(ctx, fake):
    assert repo.acquire_task_lock(ctx.db, "t_ok", "otherhost:999:t_ok", 30)
    stop = RecordingEvent()
    result = execute_with_retries(ctx, "t_ok", stop_event=stop, max_retries=3, backoff=0.01)
    assert result.status == RunStatus.SKIPPED and result.error_kind == ErrorKind.RECOVERABLE
    assert fake.calls == [] and stop.waits == [] and _runs(ctx.db, "t_ok") == []


# --------------------------------------------------------------------------- #
# Single-instance lock + recovery helpers
# --------------------------------------------------------------------------- #
def test_instance_lock_is_exclusive(tmp_path):
    path = tmp_path / "data" / "daemon.lock"
    first = InstanceLock(path)
    first.acquire()
    try:
        with pytest.raises(DaemonAlreadyRunning, match="already running"):
            InstanceLock(path).acquire()
    finally:
        first.release()
    with InstanceLock(path) as again:
        assert again.held


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_release_dead_task_locks(db):
    host = socket.gethostname()
    dead = _dead_pid()
    assert repo.acquire_task_lock(db, "t_dead", f"{host}:{dead}:t_dead", 90)
    assert repo.acquire_task_lock(db, "t_remote", f"elsewhere:{dead}:t_remote", 90)
    assert store.release_dead_task_locks(db) == ["t_dead"]
    assert db.scalar("SELECT lock_owner FROM scheduled_tasks WHERE task_name = 't_dead'") is None
    assert db.scalar("SELECT lock_owner FROM scheduled_tasks WHERE task_name = 't_remote'") is not None


def test_scheduler_problem_recorded_once(db):
    assert store.record_scheduler_problem(db, message="scheduler.tasks.x: unknown task name") is not None
    assert store.record_scheduler_problem(db, message="scheduler.tasks.x: unknown task name") is None
    row = db.query_one("SELECT * FROM errors WHERE component = 'scheduler'")
    assert row["requires_human_action"] == 1 and row["error_kind"] == "human_action_required"


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #
def test_scheduler_status_daemon_states(db, config):
    assert scheduler_status(db, config)["daemon"]["state"] == "stopped"
    store.write_daemon_started(db, pid=4242, host="laptop", lock_path="/x/daemon.lock", interrupted_runs=0)
    status = scheduler_status(db, config)
    assert status["daemon"]["state"] == "running" and status["daemon"]["pid"] == 4242
    repo.set_state(db, store.STATE_HEARTBEAT_AT, to_iso(utcnow() - timedelta(minutes=10)))
    status = scheduler_status(db, config)
    assert status["daemon"]["state"] == "stale" and status["daemon"]["heartbeat_age_seconds"] >= 600
    store.write_daemon_stopped(db)
    assert scheduler_status(db, config)["daemon"]["state"] == "stopped"


def test_scheduler_status_tasks(ctx, fake, config):
    db = ctx.db
    config.scheduler.tasks["t_ok"] = ScheduleEntry(cron="0 6 * * *")
    tasks.run_task(ctx, "t_ok", trigger="scheduled")
    tasks.run_task(ctx, "t_permanent", trigger="scheduled")
    repo.set_paused(db, True, actor="cli", reason="interviews this week")
    assert repo.acquire_task_lock(db, "report", "someone:1:report", 30)
    status = scheduler_status(db, config)
    assert status["paused"] is True and status["paused_reason"] == "interviews this week"
    assert status["timezone"] == "America/New_York"
    t_ok = status["tasks"]["t_ok"]
    assert t_ok["schedule"] == "cron 0 6 * * *" and t_ok["enabled"] and t_ok["in_config"]
    assert t_ok["last_status"] == "succeeded" and t_ok["last_success_at"] and t_ok["consecutive_failures"] == 0
    assert t_ok["next_run_at"] is None  # no daemon running
    failed = status["tasks"]["t_permanent"]
    assert not failed["in_config"] and not failed["enabled"] and failed["consecutive_failures"] == 1
    assert failed["last_error"]["error_kind"] == "permanent"
    assert status["tasks"]["report"]["lock_held"] is True
    assert status["tasks"]["email_sync"]["enabled"] is False
    assert status["tasks"]["health"]["schedule"] == "every 60 min"
