"""Daemon lifecycle tests (real BackgroundScheduler in a thread) and CLI tests."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import types
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import typer
import yaml
from apscheduler.events import EVENT_JOB_MISSED, JobExecutionEvent
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from typer.testing import CliRunner

from job_agent import tasks
from job_agent.config import ScheduleEntry
from job_agent.database import repo
from job_agent.errors import ConfigError, PermanentError
from job_agent.scheduler import daemon as daemon_mod
from job_agent.scheduler import store
from job_agent.scheduler.lock import DaemonAlreadyRunning, InstanceLock, daemon_lock_path
from job_agent.utils.time import to_iso, utcnow, utcnow_iso

NY = ZoneInfo("America/New_York")
REAL_TASKS = ("health", "discover_and_score", "email_sync", "followups", "report")


@pytest.fixture
def fake(monkeypatch):
    """Fake task implementations. Also replaces the real scheduled task names so no
    real discovery / Gmail / report code can run from these tests."""
    state = types.SimpleNamespace(calls=[], started=threading.Event(), release=threading.Event())

    def ok(ctx, run):
        state.calls.append(run.task_name)
        return {"ok": True}

    def slow(ctx, run):
        state.calls.append(run.task_name)
        state.started.set()
        assert state.release.wait(20), "test never released the slow task"
        return {"slow": True}

    def permanent(ctx, run):
        state.calls.append(run.task_name)
        raise PermanentError("cannot do it")

    mod = types.ModuleType("fake_daemon_tasks")
    mod.ok, mod.slow, mod.permanent = ok, slow, permanent
    monkeypatch.setitem(sys.modules, "fake_daemon_tasks", mod)
    for name, fn in (("t_ok", "ok"), ("t_slow", "slow"), ("t_fail", "permanent"), *((n, "ok") for n in REAL_TASKS)):
        monkeypatch.setitem(tasks.TASKS, name, tasks.TaskSpec(name, f"fake_daemon_tasks:{fn}", name))
    return state


class DaemonThread:
    """run_daemon in a background thread with a capturing scheduler factory."""

    def __init__(self, ctx, *, extra_jobs=None, heartbeat=0.2, catch_up_delay=3600.0):
        self.ctx = ctx
        self.stop = threading.Event()
        self.started = threading.Event()
        self.report = None
        self.scheduler = None
        self.error: BaseException | None = None
        self._extra_jobs = extra_jobs
        self._kwargs = {"heartbeat_seconds": heartbeat, "catch_up_delay_seconds": catch_up_delay}
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _factory(self, **kwargs):
        self.scheduler = BackgroundScheduler(**kwargs)
        if self._extra_jobs:
            self._extra_jobs(self.scheduler, self.stop)
        return self.scheduler

    def _on_started(self, report):
        self.report = report
        self.started.set()

    def _run(self):
        try:
            daemon_mod.run_daemon(self.ctx, stop_event=self.stop, scheduler_factory=self._factory,
                                  on_started=self._on_started, **self._kwargs)
        except BaseException as exc:  # surfaced to the test
            self.error = exc
            self.started.set()

    def __enter__(self):
        self.thread.start()
        assert self.started.wait(15), "daemon did not start"
        if self.error:
            raise self.error
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(20)
        assert not self.thread.is_alive(), "daemon did not stop"
        if self.error and exc[0] is None:
            raise self.error


def wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _state(db, key):
    return repo.get_state(db, key)


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _cron_hours_ago(hours: int) -> str:
    return f"0 {(datetime.now(NY).hour - hours) % 24} * * *"


# --------------------------------------------------------------------------- #
# Scheduling from config
# --------------------------------------------------------------------------- #
def test_config_tasks_scheduled_in_timezone_and_unknown_skipped(ctx, fake, config):
    config.scheduler.tasks = {
        "t_ok": ScheduleEntry(cron="0 3 * * *"),
        "health": ScheduleEntry(interval_minutes=5),
        "no_such_task": ScheduleEntry(interval_minutes=10),
        "t_fail": ScheduleEntry(cron="0 4 * * *", enabled=False),
    }
    with DaemonThread(ctx.fork()) as d:
        jobs = {job.id: job for job in d.scheduler.get_jobs()}
        assert set(jobs) == {"task:t_ok", "task:health"}
        cron = jobs["task:t_ok"].trigger
        assert isinstance(cron, CronTrigger) and cron.timezone == NY
        assert jobs["task:t_ok"].next_run_time.astimezone(NY).strftime("%H:%M") == "03:00"
        assert isinstance(jobs["task:health"].trigger, IntervalTrigger)
        assert jobs["task:health"].trigger.interval == timedelta(minutes=5)
        assert jobs["task:t_ok"].coalesce is True and jobs["task:t_ok"].max_instances == 1
        assert jobs["task:t_ok"].misfire_grace_time == config.scheduler.misfire_grace_minutes * 60

        db = ctx.db
        row = db.query_one("SELECT * FROM scheduled_tasks WHERE task_name = 't_ok'")
        assert row["schedule"] == "cron 0 3 * * *" and row["enabled"] == 1 and row["next_run_at"]
        disabled = db.query_one("SELECT * FROM scheduled_tasks WHERE task_name = 't_fail'")
        assert disabled["enabled"] == 0 and disabled["next_run_at"] is None
        err = db.query_one("SELECT * FROM errors WHERE component = 'scheduler'")
        assert "no_such_task" in err["message"] and err["requires_human_action"] == 1
        assert ("no_such_task", "unknown task name") in d.report.skipped
        assert ("t_fail", "disabled in config") in d.report.skipped
        assert {s.task_name for s in d.report.scheduled} == {"t_ok", "health"}
    assert db.scalar("SELECT next_run_at FROM scheduled_tasks WHERE task_name = 't_ok'") is None
    assert fake.calls == []


def test_scheduler_disabled_refuses_to_start(ctx, config):
    config.scheduler.enabled = False
    with pytest.raises(ConfigError, match="scheduler.enabled"):
        daemon_mod.run_daemon(ctx)


# --------------------------------------------------------------------------- #
# Running, heartbeat, graceful shutdown
# --------------------------------------------------------------------------- #
def test_daemon_runs_job_heartbeats_and_stops_cleanly(ctx, fake, config):
    config.scheduler.tasks = {}

    def jobs(scheduler, stop):  # test-only: seconds-level interval
        scheduler.add_job(daemon_mod.run_scheduled_task, IntervalTrigger(seconds=1), args=[ctx, "t_ok"],
                          kwargs={"stop_event": stop}, id="test:t_ok")

    db = ctx.db
    with DaemonThread(ctx.fork(), extra_jobs=jobs):
        assert wait_until(lambda: "t_ok" in fake.calls), "scheduled fake task never ran"
        first_beat = _state(db, store.STATE_HEARTBEAT_AT)
        assert first_beat is not None
        # the heartbeat thread keeps writing (timestamps have 1 s resolution)
        assert wait_until(lambda: _state(db, store.STATE_HEARTBEAT_AT) not in (None, first_beat), timeout=5)
        assert _state(db, store.STATE_PID) == str(os.getpid())
        assert _state(db, store.STATE_HOST) == socket.gethostname()
        assert _state(db, store.STATE_STARTED_AT) and _state(db, store.STATE_STOPPED_AT) is None
        from job_agent.scheduler.status import scheduler_status

        assert scheduler_status(db, config)["daemon"]["state"] == "running"
    assert first_beat
    assert _state(db, store.STATE_STOPPED_AT) is not None
    assert _state(db, store.STATE_HEARTBEAT_AT) is None  # cleared on clean stop
    run = db.query_one("SELECT * FROM agent_runs WHERE task_name = 't_ok' ORDER BY id LIMIT 1")
    assert run["status"] == "succeeded" and run["trigger"] == "scheduled"
    assert db.scalar("SELECT COUNT(*) FROM agent_runs WHERE status = 'running'") == 0
    assert db.scalar("SELECT COUNT(*) FROM audit_events WHERE action = 'daemon_stopped'") == 1
    with InstanceLock(daemon_lock_path(config)):  # lock released
        pass


def test_shutdown_waits_for_running_task(ctx, fake, config):
    config.scheduler.tasks = {}

    def jobs(scheduler, stop):
        scheduler.add_job(daemon_mod.run_scheduled_task, IntervalTrigger(seconds=1), args=[ctx, "t_slow"],
                          kwargs={"stop_event": stop}, id="test:t_slow")

    d = DaemonThread(ctx.fork(), extra_jobs=jobs)
    with d:
        assert fake.started.wait(10), "slow task never started"
        d.stop.set()
        time.sleep(0.5)
        assert d.thread.is_alive(), "daemon exited while a task was still running"
        fake.release.set()
    run = ctx.db.query_one("SELECT * FROM agent_runs WHERE task_name = 't_slow'")
    assert run["status"] == "succeeded" and fake.calls == ["t_slow"]
    assert _state(ctx.db, store.STATE_STOPPED_AT) is not None


# --------------------------------------------------------------------------- #
# Single instance
# --------------------------------------------------------------------------- #
def test_second_daemon_is_refused(ctx, fake, config):
    config.scheduler.tasks = {"t_ok": ScheduleEntry(cron="0 3 * * *")}
    with DaemonThread(ctx.fork()):
        second = ctx.fork()
        try:
            with pytest.raises(DaemonAlreadyRunning, match="already running"):
                daemon_mod.run_daemon(second, stop_event=threading.Event())
            with pytest.raises(DaemonAlreadyRunning):
                daemon_mod.run_once(second)
        finally:
            second.close()
    assert _state(ctx.db, store.STATE_STOPPED_AT) is not None  # the first one stopped normally


def test_daemon_recorded_in_db_from_elsewhere_is_refused(ctx, config):
    db = ctx.db
    store.write_daemon_started(db, pid=4242, host="other-machine", lock_path="/elsewhere/daemon.lock",
                               interrupted_runs=0)
    with pytest.raises(DaemonAlreadyRunning, match="other-machine"):
        daemon_mod.run_daemon(ctx, stop_event=threading.Event())
    with InstanceLock(daemon_lock_path(config)):  # released after the refusal
        pass
    repo.set_state(db, store.STATE_HEARTBEAT_AT, to_iso(utcnow() - timedelta(minutes=10)))  # stale: allowed
    stop = threading.Event()
    stop.set()
    daemon_mod.run_daemon(ctx.fork(), stop_event=stop)


# --------------------------------------------------------------------------- #
# Restart recovery and catch-up
# --------------------------------------------------------------------------- #
def test_startup_marks_dead_runs_interrupted_and_releases_their_locks(ctx, fake, config):
    config.scheduler.tasks = {"t_ok": ScheduleEntry(cron=_cron_hours_ago(0))}
    db = ctx.db
    dead = _dead_pid()
    db.insert("agent_runs", {"run_uid": "R-dead", "task_name": "t_ok", "trigger": "scheduled",
                             "status": "running", "started_at": utcnow_iso(), "attempt": 1, "host_pid": dead})
    assert repo.acquire_task_lock(db, "t_ok", f"{socket.gethostname()}:{dead}:t_ok", 90)
    with DaemonThread(ctx.fork()) as d:
        assert d.report.interrupted_runs == 1 and d.report.released_locks == ["t_ok"]
    row = db.query_one("SELECT * FROM agent_runs WHERE run_uid = 'R-dead'")
    assert row["status"] == "interrupted" and row["error_kind"] == "unknown_outcome"
    assert db.scalar("SELECT lock_owner FROM scheduled_tasks WHERE task_name = 't_ok'") is None


def test_catch_up_runs_missed_daily_task_once(ctx, fake, config):
    config.scheduler.tasks = {"t_fail": ScheduleEntry(cron=_cron_hours_ago(3))}
    db = ctx.db
    two_days_ago = to_iso(utcnow() - timedelta(days=2))
    db.execute("INSERT INTO scheduled_tasks (task_name, last_success_at, updated_at) VALUES (?, ?, ?)",
               ("t_fail", two_days_ago, utcnow_iso()))
    with DaemonThread(ctx.fork(), catch_up_delay=0.05) as d:
        assert d.report.catch_up == ["t_fail"]
        assert wait_until(lambda: fake.calls == ["t_fail"])
        assert wait_until(lambda: db.scalar("SELECT COUNT(*) FROM agent_runs WHERE status = 'failed'") == 1)
    run = db.query_one("SELECT * FROM agent_runs WHERE task_name = 't_fail'")
    assert run["trigger"] == "startup" and run["status"] == "failed"
    # still not successful, but the same missed run is never caught up twice
    with DaemonThread(ctx.fork(), catch_up_delay=0.05) as d:
        assert d.report.catch_up == []
        time.sleep(0.3)
    assert fake.calls == ["t_fail"]


def test_catch_up_order(ctx, fake, config):
    cron = _cron_hours_ago(3)
    config.scheduler.tasks = {name: ScheduleEntry(cron=cron) for name in ("report", "followups", "t_ok",
                                                                          "discover_and_score", "health")}
    with DaemonThread(ctx.fork(), catch_up_delay=0.05) as d:
        assert d.report.catch_up == ["health", "discover_and_score", "followups", "report", "t_ok"]
        assert wait_until(lambda: len(fake.calls) == 5)
    assert fake.calls == ["health", "discover_and_score", "followups", "report", "t_ok"]


def test_run_missed_while_asleep_is_caught_up_once(ctx, fake, config):
    config.scheduler.tasks = {"t_ok": ScheduleEntry(cron=_cron_hours_ago(2))}
    db = ctx.db
    db.execute("INSERT INTO scheduled_tasks (task_name, last_success_at, updated_at) VALUES (?, ?, ?)",
               ("t_ok", utcnow_iso(), utcnow_iso()))
    with DaemonThread(ctx.fork(), catch_up_delay=0.05) as d:
        assert d.report.catch_up == []
        missed_at = datetime.now(NY) - timedelta(hours=2)
        for _ in range(2):
            d.scheduler._dispatch_event(JobExecutionEvent(EVENT_JOB_MISSED, "task:t_ok", "default", missed_at))
        assert wait_until(lambda: fake.calls == ["t_ok"])
        time.sleep(0.3)
        d.scheduler._dispatch_event(JobExecutionEvent(EVENT_JOB_MISSED, "task:t_ok", "default", missed_at))
        time.sleep(0.3)
    assert fake.calls == ["t_ok"]


def test_paused_daemon_skips_runs(ctx, fake, config):
    config.scheduler.tasks = {"t_ok": ScheduleEntry(cron=_cron_hours_ago(3))}
    repo.set_paused(ctx.db, True, actor="cli", reason="on holiday")
    with DaemonThread(ctx.fork(), catch_up_delay=0.05) as d:
        assert d.report.paused and d.report.paused_reason == "on holiday"
        assert wait_until(lambda: ctx.db.scalar(
            "SELECT last_status FROM scheduled_tasks WHERE task_name = 't_ok'") == "skipped")
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
runner = CliRunner()


def _app() -> typer.Typer:
    from job_agent.scheduler.commands import register

    app = typer.Typer()

    @app.callback()
    def _root() -> None:  # keep "daemon"/"schedule" as subcommands
        pass

    register(app)
    return app


@pytest.fixture
def wide_console(monkeypatch):
    from job_agent import cli

    monkeypatch.setattr(cli.console, "width", 220)
    monkeypatch.setattr(cli.state, "_ctx", None)
    yield
    if cli.state._ctx is not None:
        cli.state._ctx.close()


def _write_schedule(home, tasks_section: dict) -> None:
    path = home / "config" / "config.yaml"
    data = yaml.safe_load(path.read_text())
    data["scheduler"]["tasks"] = tasks_section
    path.write_text(yaml.safe_dump(data))


def test_cli_schedule_shows_preview_and_status(home, wide_console):
    result = runner.invoke(_app(), ["schedule"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Daemon:" in out and "stopped" in out and "Automation:" in out
    for name in ("discover_and_score", "followups", "report", "health", "email_sync"):
        assert name in out
    assert "cron 15 7,16 * * *" in out and "every 60 min" in out and "disabled" in out


def test_cli_daemon_once_runs_enabled_tasks_in_order(home, fake, wide_console):
    _write_schedule(home, {"report": {"cron": "30 21 * * *"}, "followups": {"cron": "0 8 * * *"},
                           "email_sync": {"interval_minutes": 30, "enabled": False},
                           "discover_and_score": {"cron": "15 7,16 * * *"}, "health": {"interval_minutes": 60}})
    result = runner.invoke(_app(), ["daemon", "--once"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert fake.calls == ["health", "discover_and_score", "followups", "report"]
    assert result.output.count("succeeded") == 4


def test_cli_daemon_once_failure_and_pause(home, fake, wide_console):
    from job_agent import cli

    _write_schedule(home, {"t_fail": {"interval_minutes": 60}, "t_ok": {"interval_minutes": 60}})
    result = runner.invoke(_app(), ["daemon", "--once"], catch_exceptions=False)
    assert result.exit_code == 1 and "t_fail" in result.output and "failed" in result.output
    assert fake.calls == ["t_fail", "t_ok"]  # one failure does not stop the others
    repo.set_paused(cli.get_ctx().db, True, actor="cli", reason="testing")
    result = runner.invoke(_app(), ["daemon", "--once"], catch_exceptions=False)
    assert result.exit_code == 0 and result.output.count("skipped") == 2
    assert fake.calls == ["t_fail", "t_ok"]


def test_cli_daemon_refuses_when_already_running(home, fake, wide_console):
    from job_agent.config import load_config

    _write_schedule(home, {"t_ok": {"interval_minutes": 60}})
    with InstanceLock(daemon_lock_path(load_config(home=home))):
        result = runner.invoke(_app(), ["daemon", "--once"])
    assert result.exit_code == 3 and fake.calls == []


def test_cli_daemon_prints_startup_report(home, fake, wide_console, monkeypatch):
    _write_schedule(home, {"t_ok": {"cron": "0 3 * * *"}, "bogus": {"interval_minutes": 15}})
    real = daemon_mod.run_daemon

    def run_and_stop(ctx, **kwargs):
        stop = threading.Event()
        stop.set()  # start, print the report, then stop at once
        real(ctx, stop_event=stop, **kwargs)

    monkeypatch.setattr(daemon_mod, "run_daemon", run_and_stop)
    result = runner.invoke(_app(), ["daemon"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Initialization: ok" in out and "Database: ok" in out and "Automation: active" in out
    assert "t_ok" in out and "cron 0 3 * * *" in out and "bogus (unknown task name)" in out
    assert "Pending approvals: 0" in out and "Unresolved errors: 1" in out and "Daemon stopped." in out
    assert fake.calls == []
