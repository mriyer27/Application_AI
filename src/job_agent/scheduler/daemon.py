"""The recurring scheduler daemon (``job-agent daemon``).

Lifecycle of ``run_daemon``:

1. single-instance guard: OS lock on ``<data_dir>/daemon.lock`` plus a DB record
   of pid/host (catches a second daemon pointed at the same database from a
   different data directory or machine);
2. recovery: runs left ``running`` by a dead process are marked interrupted,
   task locks held by dead processes are released, approvals are expired;
3. one APScheduler job per enabled ``scheduler.tasks`` entry (cron in
   ``app.timezone`` or a fixed interval), ``coalesce`` + ``max_instances=1`` +
   ``misfire_grace_time``; schedules are persisted to ``scheduled_tasks``;
4. catch-up: each task whose most recent scheduled run was missed (laptop
   off/asleep, daemon stopped) runs once, shortly after start, in a fixed
   order; runs missed by more than the grace time while the daemon was alive
   (sleep) are caught up the same way;
5. a heartbeat thread writes ``daemon_heartbeat_at`` every 60 s and wakes the
   scheduler so jobs due during a sleep run promptly after resume;
6. SIGINT/SIGTERM/SIGBREAK -> graceful shutdown: running tasks finish, retries
   waiting on backoff are cancelled, the lock is released.

Tasks only ever run through ``tasks.run_task`` (pause gate, DB task lock,
``agent_runs`` record, classified errors) via ``execute_with_retries``.
"""

from __future__ import annotations

import os
import signal
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from types import FrameType
from typing import Any, Callable
from zoneinfo import ZoneInfo

from apscheduler.events import EVENT_JOB_MISSED, JobExecutionEvent
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import BaseScheduler
from apscheduler.triggers.base import BaseTrigger

from job_agent import tasks
from job_agent.config import AppConfig
from job_agent.context import AppContext
from job_agent.database import repo
from job_agent.database.connection import Database
from job_agent.errors import ConfigError, DatabaseError, JobAgentError
from job_agent.logging_config import get_logger
from job_agent.models import ErrorKind, RunStatus
from job_agent.scheduler import store
from job_agent.scheduler.lock import DaemonAlreadyRunning, InstanceLock, daemon_lock_path
from job_agent.scheduler.runner import execute_with_retries
from job_agent.scheduler.triggers import build_trigger, missed_run_marker, order_key, schedule_text
from job_agent.tasks import TaskResult
from job_agent.utils.time import to_iso

log = get_logger(__name__)

HEARTBEAT_SECONDS = 60.0
CATCH_UP_DELAY_SECONDS = 5.0
_JOB_PREFIX = "task:"
# Wall clock advancing this much more than the monotonic clock between heartbeats
# means the machine was suspended (or the clock was changed).
_SLEEP_GAP_SECONDS = 120.0

#: ``scheduler_factory(timezone=..., job_defaults=...)`` must return an unstarted scheduler.
SchedulerFactory = Callable[..., BaseScheduler]

__all__ = [
    "CATCH_UP_DELAY_SECONDS", "DaemonStartup", "HEARTBEAT_SECONDS", "ScheduledTaskInfo", "execute_with_retries",
    "once_task_order", "run_daemon", "run_once", "run_scheduled_task",
]


@dataclass
class ScheduledTaskInfo:
    task_name: str
    schedule: str
    next_run_at: str | None  # UTC ISO


@dataclass
class DaemonStartup:
    """What the daemon did on start; printed by ``job-agent daemon``."""

    pid: int
    host: str
    lock_path: str
    started_at: str
    timezone: str
    db_integrity: str
    paused: bool
    paused_reason: str | None
    interrupted_runs: int
    released_locks: list[str]
    expired_approvals: int
    scheduled: list[ScheduledTaskInfo] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (task name, reason)
    catch_up: list[str] = field(default_factory=list)
    pending_approvals: int = 0
    unresolved_errors: int = 0
    errors_needing_action: int = 0


def _job_id(task_name: str) -> str:
    return _JOB_PREFIX + task_name


def _ensure_enabled(config: AppConfig) -> None:
    if not config.scheduler.enabled:
        raise ConfigError("the scheduler is disabled (scheduler.enabled: false)",
                          human_action="Set scheduler.enabled: true in config/config.yaml to run scheduled tasks, "
                                       "or run tasks manually with `job-agent tasks run <name>`.")


def _record_problem_safely(ctx: AppContext, *, message: str, task_name: str | None,
                           kind: ErrorKind = ErrorKind.PERMANENT, human_action: str | None = None) -> None:
    """Record an infrastructure problem; if the database itself is failing, the log keeps it."""
    try:
        store.record_scheduler_problem(ctx.db, message=message, task_name=task_name, kind=kind,
                                       human_action=human_action)
    except Exception:  # the DB may be what is broken; never let that hide the original problem
        log.exception("could not record scheduler problem in the database: %s", message)


def _report_unknown_task(db: Database, name: str) -> None:
    message = (f"scheduler.tasks.{name}: unknown task name; it is not scheduled or run. "
               f"Known tasks: {', '.join(sorted(tasks.TASKS))}")
    log.warning(message)
    store.record_scheduler_problem(db, message=message, task_name=name,
                                   human_action="Fix the task name under scheduler.tasks in config/config.yaml "
                                                "(see `job-agent tasks list`), then restart the daemon.")


# --------------------------------------------------------------------------- #
# One scheduled execution
# --------------------------------------------------------------------------- #
def run_scheduled_task(ctx: AppContext, task_name: str, *, stop_event: threading.Event | None = None,
                       next_run: Callable[[], datetime | None] | None = None,
                       trigger: str = "scheduled") -> TaskResult:
    """One scheduled execution: own forked context, retries, then persist ``next_run_at``.

    ``next_run`` returns the task's next fire time; it must not call into the
    scheduler (APScheduler holds its job-store lock while ``shutdown(wait=True)``
    waits for running jobs, so a job that calls ``get_job`` could deadlock).

    Never raises: failures inside the task are recorded by ``run_task``;
    failures around it (database unavailable, unknown task) are logged and
    recorded here so one bad run cannot stop the scheduler.
    """
    if stop_event is not None and stop_event.is_set():
        log.info("not starting %s: the daemon is stopping", task_name)
        return TaskResult(task_name, RunStatus.SKIPPED, None, {"reason": "daemon stopping"})
    run_ctx = ctx.fork()
    try:
        try:
            result = execute_with_retries(run_ctx, task_name, stop_event=stop_event, trigger=trigger)
        except Exception as exc:  # recorded below; the scheduler must keep running
            kind = exc.kind if isinstance(exc, JobAgentError) else ErrorKind.PERMANENT
            log.exception("scheduled run of %s could not start", task_name)
            _record_problem_safely(run_ctx, message=f"scheduled run of {task_name} could not start: "
                                   f"{type(exc).__name__}: {exc}", task_name=task_name, kind=kind)
            result = TaskResult(task_name, RunStatus.FAILED, None, {}, error=str(exc), error_kind=kind)
        if next_run is not None:
            try:
                store.set_next_run(run_ctx.db, task_name, to_iso(next_run()))
            except JobAgentError:
                log.exception("could not persist next run time of %s", task_name)
        return result
    finally:
        run_ctx.close()


# --------------------------------------------------------------------------- #
# Single-instance check against the database record
# --------------------------------------------------------------------------- #
def _check_recorded_daemon(db: Database, lock: InstanceLock) -> None:
    """Refuse to start if the DB shows another live daemon that our OS lock cannot see
    (same database, different data directory or machine)."""
    info = store.recorded_daemon(db)
    if info["state"] != "running" or info["pid"] is None:
        return
    host = socket.gethostname()
    pid = int(info["pid"])
    if info["host"] == host:
        if info["lock_path"] == str(lock.path):
            return  # we hold that very lock, so the recorded daemon is gone
        if pid == os.getpid() or not store.pid_alive(pid):
            return
    raise DaemonAlreadyRunning(
        f"another job-agent daemon (pid {pid} on {info['host']}) is using this database; "
        f"last heartbeat {info['heartbeat_age_seconds']} s ago"
    )


def _recover(db: Database) -> tuple[int, list[str], int]:
    interrupted = repo.mark_interrupted_runs(db)
    released = store.release_dead_task_locks(db)
    expired = repo.expire_approvals(db)
    if interrupted:
        log.warning("%d run(s) left 'running' by a previous process were marked interrupted", interrupted)
    if released:
        log.warning("released task lock(s) held by dead processes: %s", ", ".join(released))
    return interrupted, released, expired


# --------------------------------------------------------------------------- #
# Signals
# --------------------------------------------------------------------------- #
def _install_signal_handlers(stop_event: threading.Event, *, abort_on_repeat: bool = False) -> dict[int, Any]:
    """SIGINT/SIGTERM (and SIGBREAK on Windows) request a graceful stop. Main thread only.

    With ``abort_on_repeat`` (``--once``, where the task runs in the main thread) a
    second Ctrl+C raises ``KeyboardInterrupt``; ``run_task`` then records the run
    as interrupted.
    """
    if threading.current_thread() is not threading.main_thread():
        return {}

    def _handler(signum: int, frame: FrameType | None) -> None:
        name = signal.Signals(signum).name
        if stop_event.is_set():
            if abort_on_repeat and signum == signal.SIGINT:
                raise KeyboardInterrupt
            log.warning("%s received again: still waiting for running tasks to finish "
                        "(kill the process to force; its run will be marked interrupted at next start)", name)
        else:
            log.warning("%s received: stopping after running tasks finish%s", name,
                        " (Ctrl+C again aborts the running task)" if abort_on_repeat else "")
        stop_event.set()

    previous: dict[int, Any] = {}
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            previous[sig] = signal.signal(sig, _handler)
        except (ValueError, OSError) as exc:  # pragma: no cover - platform specific
            log.warning("could not install %s handler: %s", name, exc)
    return previous


def _restore_signal_handlers(previous: dict[int, Any]) -> None:
    for sig, handler in previous.items():
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, TypeError):  # pragma: no cover - platform specific
            log.warning("could not restore handler for signal %s", sig)


def _wait_for_stop(stop_event: threading.Event) -> None:
    # Short timed waits keep Ctrl+C responsive on Windows, where untimed waits block signals.
    try:
        while not stop_event.wait(1.0):
            pass
    except KeyboardInterrupt:
        log.warning("interrupted: stopping after running tasks finish")
        stop_event.set()


# --------------------------------------------------------------------------- #
# Daemon
# --------------------------------------------------------------------------- #
class _Heartbeat(threading.Thread):
    """Writes ``daemon_heartbeat_at`` and ``next_run_at`` periodically and wakes the scheduler."""

    def __init__(self, daemon: "_Daemon", interval: float) -> None:
        super().__init__(name="job-agent-heartbeat", daemon=True)
        self._daemon = daemon
        self.interval = interval
        self.halt = threading.Event()
        self._failures = 0

    def run(self) -> None:
        ctx = self._daemon.ctx.fork()
        last_wall, last_mono = time.time(), time.monotonic()
        try:
            while True:
                self._tick(ctx)
                if self.halt.wait(self.interval):
                    return
                wall, mono = time.time(), time.monotonic()
                gap = (wall - last_wall) - (mono - last_mono)
                if gap > _SLEEP_GAP_SECONDS:
                    log.info("wall clock advanced %.0f s more than expected (sleep/hibernate or clock change); "
                             "checking for due jobs", gap)
                last_wall, last_mono = wall, mono
        finally:
            ctx.close()

    def _tick(self, ctx: AppContext) -> None:
        try:
            db = ctx.db
            store.beat(db)
            self._daemon.persist_next_runs(db)
            self._failures = 0
        except Exception as exc:  # keep beating; record the first failure of a streak
            self._failures += 1
            log.exception("heartbeat failed (%d in a row)", self._failures)
            if self._failures == 1:
                _record_problem_safely(ctx, message=f"daemon heartbeat failed: {type(exc).__name__}: {exc}",
                                       task_name=None, kind=ErrorKind.RECOVERABLE)
        scheduler = self._daemon.scheduler
        if scheduler is not None and scheduler.running:
            # Timed waits do not count time spent asleep; waking the scheduler makes it
            # re-check the wall clock so jobs due during a sleep run soon after resume.
            scheduler.wakeup()


class _CatchUpWorker(threading.Thread):
    """Runs missed tasks once each, one after another, in ``TASK_ORDER``.

    A separate thread (not an APScheduler job) so catch-up requests coming from
    scheduler events never call back into the scheduler, and so catch-ups run
    sequentially (report after discovery) instead of in parallel.
    """

    def __init__(self, daemon: "_Daemon", delay: float) -> None:
        super().__init__(name="job-agent-catch-up", daemon=True)
        self._daemon = daemon
        self.delay = delay
        self._cond = threading.Condition()
        self._pending: dict[str, tuple[str, str]] = {}  # task -> (marker, trigger)

    def request(self, task_name: str, marker: str, *, trigger: str) -> None:
        with self._cond:
            self._pending.setdefault(task_name, (marker, trigger))
            self._cond.notify()

    def wake(self) -> None:
        with self._cond:
            self._cond.notify()

    def run(self) -> None:
        stop = self._daemon.stop_event
        while not stop.is_set():
            with self._cond:
                while not self._pending and not stop.is_set():
                    self._cond.wait(timeout=1.0)
            if stop.wait(self.delay):  # let start-up settle; batch near-simultaneous requests
                return
            self._drain()

    def _drain(self) -> None:
        stop = self._daemon.stop_event
        while not stop.is_set():
            with self._cond:
                if not self._pending:
                    return
                name = min(self._pending, key=self._daemon.order)
                marker, trigger = self._pending.pop(name)
            try:
                if self._daemon.claim_catch_up(name, marker):
                    run_scheduled_task(self._daemon.ctx, name, stop_event=stop,
                                       next_run=lambda n=name: self._daemon.next_fire(n), trigger=trigger)
            except Exception:  # run_scheduled_task never raises; this guards the worker itself
                log.exception("catch-up of %s crashed", name)


class _Daemon:
    def __init__(self, ctx: AppContext, stop_event: threading.Event, scheduler_factory: SchedulerFactory | None,
                 heartbeat_seconds: float, catch_up_delay_seconds: float) -> None:
        self.ctx = ctx
        self.config = ctx.config
        self.stop_event = stop_event
        self.scheduler_factory = scheduler_factory
        self.heartbeat_seconds = heartbeat_seconds
        self.tz = ZoneInfo(ctx.config.app.timezone)
        self.scheduler: BaseScheduler | None = None
        self.triggers: dict[str, BaseTrigger] = {}  # scheduled task name -> trigger
        self.schedules: dict[str, str] = {}  # scheduled task name -> schedule text
        self._config_order = list(ctx.config.scheduler.tasks)
        self._heartbeat = _Heartbeat(self, heartbeat_seconds)
        self._catch_up = _CatchUpWorker(self, catch_up_delay_seconds)
        self._identity_written = False

    def order(self, name: str) -> tuple[int, int, str]:
        return order_key(name, self._config_order)

    def next_fire(self, task_name: str) -> datetime | None:
        """Next fire time computed from the trigger alone (no scheduler locks)."""
        trigger = self.triggers.get(task_name)
        return trigger.get_next_fire_time(None, datetime.now(self.tz)) if trigger is not None else None

    # -- startup ----------------------------------------------------------- #
    def start(self, lock: InstanceLock) -> DaemonStartup:
        db = self.ctx.db
        integrity = db.integrity_check()
        if integrity != "ok":
            raise DatabaseError(f"database integrity check failed: {integrity}",
                                human_action="Do not run the daemon on a damaged database; restore "
                                             "data/job_agent.db from a backup (see docs/setup.md, Backups).")
        _check_recorded_daemon(db, lock)
        interrupted, released, expired = _recover(db)
        pid, host = os.getpid(), socket.gethostname()
        started_at = store.write_daemon_started(db, pid=pid, host=host, lock_path=str(lock.path),
                                                interrupted_runs=interrupted)
        self._identity_written = True

        self.scheduler = self._build_scheduler()
        skipped = self._add_task_jobs(db)
        self.scheduler.add_listener(self._on_job_missed, EVENT_JOB_MISSED)
        self.scheduler.start()
        scheduled = self._persist_schedule(db)
        self._catch_up.start()
        catch_up = self._plan_startup_catch_up(db)
        self._heartbeat.start()

        repo.audit(db, actor="scheduler", action="daemon_started", outcome="success",
                   details={"pid": pid, "interrupted_runs": interrupted, "released_locks": released,
                            "expired_approvals": expired, "scheduled": [s.task_name for s in scheduled],
                            "skipped": [name for name, _ in skipped], "catch_up": catch_up})
        log.info("daemon started (pid %s): %d task(s) scheduled, catch-up: %s", pid, len(scheduled),
                 ", ".join(catch_up) or "none")
        paused = repo.is_paused(db)
        return DaemonStartup(
            pid=pid, host=host, lock_path=str(lock.path), started_at=started_at,
            timezone=self.config.app.timezone, db_integrity=integrity, paused=paused,
            paused_reason=repo.get_state(db, "automation_paused_reason") if paused else None,
            interrupted_runs=interrupted, released_locks=released, expired_approvals=expired,
            scheduled=scheduled, skipped=skipped, catch_up=catch_up,
            pending_approvals=len(repo.pending_approvals(db)),
            unresolved_errors=int(db.scalar("SELECT COUNT(*) FROM errors WHERE resolved = 0") or 0),
            errors_needing_action=int(db.scalar(
                "SELECT COUNT(*) FROM errors WHERE resolved = 0 AND requires_human_action = 1") or 0),
        )

    def _build_scheduler(self) -> BaseScheduler:
        job_defaults = {"coalesce": True, "max_instances": 1,
                        "misfire_grace_time": self.config.scheduler.misfire_grace_minutes * 60}
        factory = self.scheduler_factory or BackgroundScheduler
        return factory(timezone=self.tz, job_defaults=job_defaults)

    def _add_task_jobs(self, db: Database) -> list[tuple[str, str]]:
        assert self.scheduler is not None
        skipped: list[tuple[str, str]] = []
        for name, entry in self.config.scheduler.tasks.items():
            if name not in tasks.TASKS:
                _report_unknown_task(db, name)
                skipped.append((name, "unknown task name"))
                continue
            text = schedule_text(entry)
            if not entry.enabled:
                store.save_schedule(db, name, schedule=text, enabled=False, next_run_at=None)
                skipped.append((name, "disabled in config"))
                continue
            try:
                trigger = build_trigger(name, entry, self.tz)
            except ConfigError as exc:
                log.warning("%s", exc)
                store.record_scheduler_problem(db, message=str(exc), task_name=name, human_action=exc.human_action)
                store.save_schedule(db, name, schedule=text, enabled=False, next_run_at=None)
                skipped.append((name, "invalid schedule"))
                continue
            self.scheduler.add_job(self._run_job, trigger, args=[name], id=_job_id(name), name=name,
                                   replace_existing=True)
            self.triggers[name] = trigger
            self.schedules[name] = text
        return skipped

    def _persist_schedule(self, db: Database) -> list[ScheduledTaskInfo]:
        assert self.scheduler is not None
        infos: list[ScheduledTaskInfo] = []
        known_in_config = {n for n in self.config.scheduler.tasks if n in tasks.TASKS}
        with db.transaction():
            store.unschedule_others(db, keep=known_in_config)
            for name, text in self.schedules.items():
                job = self.scheduler.get_job(_job_id(name))  # main thread, before any shutdown
                next_run = to_iso(job.next_run_time) if job is not None and job.next_run_time else None
                store.save_schedule(db, name, schedule=text, enabled=True, next_run_at=next_run)
                infos.append(ScheduledTaskInfo(name, text, next_run))
        infos.sort(key=lambda i: (i.next_run_at or "~", i.task_name))
        return infos

    def persist_next_runs(self, db: Database) -> None:
        if self.stop_event.is_set():
            return
        with db.transaction():
            for name in self.schedules:
                store.set_next_run(db, name, to_iso(self.next_fire(name)))

    # -- jobs -------------------------------------------------------------- #
    def _run_job(self, task_name: str) -> TaskResult:
        return run_scheduled_task(self.ctx, task_name, stop_event=self.stop_event,
                                  next_run=lambda: self.next_fire(task_name))

    # -- catch-up ---------------------------------------------------------- #
    def _plan_startup_catch_up(self, db: Database) -> list[str]:
        now = datetime.now(self.tz)
        planned: list[str] = []
        for name in sorted(self.schedules, key=self.order):
            trigger = self.triggers[name]
            marker = missed_run_marker(self.config.scheduler.tasks[name], trigger,
                                       last_success=store.last_success(db, name), now=now,
                                       next_run=trigger.get_next_fire_time(None, now))
            if marker is None or store.get_catch_up_marker(db, name) == marker:
                continue
            self._catch_up.request(name, marker, trigger="startup")
            planned.append(name)
        return planned

    def _on_job_missed(self, event: JobExecutionEvent) -> None:
        # Called from an executor thread: only hand over to the catch-up worker.
        name = event.job_id.removeprefix(_JOB_PREFIX)
        if name not in self.schedules or self.stop_event.is_set():
            return
        log.warning("%s missed its run at %s by more than the grace time (laptop asleep or busy?); "
                    "running it once now", name, event.scheduled_run_time)
        self._catch_up.request(name, to_iso(event.scheduled_run_time) or "missed", trigger="scheduled")

    def claim_catch_up(self, name: str, marker: str) -> bool:
        """Persist the marker before running so each missed run is caught up at most once."""
        ctx = self.ctx.fork()
        try:
            db = ctx.db
            if store.get_catch_up_marker(db, name) == marker:
                return False
            with db.transaction():
                store.set_catch_up_marker(db, name, marker)
                repo.audit(db, actor="scheduler", action="scheduler_catch_up", entity_type="task", entity_id=name,
                           outcome="started", details={"missed_run": marker})
            log.info("catch-up run of %s (missed: %s)", name, marker)
            return True
        except JobAgentError as exc:
            log.exception("could not prepare catch-up of %s", name)
            _record_problem_safely(ctx, message=f"catch-up of {name} skipped: {type(exc).__name__}: {exc}",
                                   task_name=name, kind=exc.kind)
            return False
        finally:
            ctx.close()

    # -- shutdown ---------------------------------------------------------- #
    def shutdown(self) -> None:
        self.stop_event.set()
        scheduler = self.scheduler
        if scheduler is not None and scheduler.running:
            log.info("stopping scheduler; waiting for running tasks to finish")
            try:
                scheduler.shutdown(wait=True)
            except Exception:  # still stop the other threads and release the lock
                log.exception("scheduler shutdown failed")
        if self._catch_up.is_alive():
            self._catch_up.wake()
            self._catch_up.join()  # a running catch-up task finishes, like scheduled ones
        if self._heartbeat.is_alive():
            self._heartbeat.halt.set()
            self._heartbeat.join(timeout=self.heartbeat_seconds + 10)
        if not self._identity_written:
            return
        try:
            db = self.ctx.db
            stopped_at = store.write_daemon_stopped(db)
            repo.audit(db, actor="scheduler", action="daemon_stopped", outcome="success",
                       details={"pid": os.getpid(), "stopped_at": stopped_at})
            log.info("daemon stopped cleanly")
        except JobAgentError:
            log.exception("could not record daemon stop (the heartbeat will go stale instead)")


def run_daemon(ctx: AppContext, *, stop_event: threading.Event | None = None,
               scheduler_factory: SchedulerFactory | None = None, heartbeat_seconds: float = HEARTBEAT_SECONDS,
               catch_up_delay_seconds: float = CATCH_UP_DELAY_SECONDS,
               on_started: Callable[[DaemonStartup], None] | None = None) -> None:
    """Run the scheduler until ``stop_event`` is set (or SIGINT/SIGTERM/SIGBREAK).

    Raises ``DaemonAlreadyRunning`` if another daemon holds the lock / database,
    ``ConfigError`` if ``scheduler.enabled`` is false, ``DatabaseError`` if the
    database fails its integrity check. Signal handlers are installed only when
    called from the main thread.
    """
    _ensure_enabled(ctx.config)
    stop = stop_event or threading.Event()
    daemon = _Daemon(ctx, stop, scheduler_factory, heartbeat_seconds, catch_up_delay_seconds)
    lock = InstanceLock(daemon_lock_path(ctx.config))
    lock.acquire()
    previous_handlers: dict[int, Any] = {}
    try:
        previous_handlers = _install_signal_handlers(stop)
        report = daemon.start(lock)
        if on_started is not None:
            on_started(report)
        _wait_for_stop(stop)
    finally:
        try:
            daemon.shutdown()
        finally:
            _restore_signal_handlers(previous_handlers)
            lock.release()


# --------------------------------------------------------------------------- #
# --once
# --------------------------------------------------------------------------- #
def once_task_order(config: AppConfig) -> list[str]:
    """Enabled, known tasks from config: health, discover_and_score, email_sync, followups, report, others."""
    config_order = list(config.scheduler.tasks)
    names = [n for n, e in config.scheduler.tasks.items() if e.enabled and n in tasks.TASKS]
    return sorted(names, key=lambda n: order_key(n, config_order))


def run_once(ctx: AppContext, *, stop_event: threading.Event | None = None,
             on_result: Callable[[TaskResult], None] | None = None) -> list[TaskResult]:
    """Run every enabled scheduled task once now (in order) and return the results.

    For testing and for OS schedulers that start the process periodically. Holds
    the same single-instance lock as the daemon, performs the same start-up
    recovery, respects the pause flag and retries recoverable failures.
    """
    _ensure_enabled(ctx.config)
    stop = stop_event or threading.Event()
    lock = InstanceLock(daemon_lock_path(ctx.config))
    lock.acquire()
    previous_handlers: dict[int, Any] = {}
    try:
        previous_handlers = _install_signal_handlers(stop, abort_on_repeat=True)
        db = ctx.db
        _check_recorded_daemon(db, lock)
        interrupted, released, expired = _recover(db)
        for name in ctx.config.scheduler.tasks:
            if name not in tasks.TASKS:
                _report_unknown_task(db, name)
        results: list[TaskResult] = []
        for name in once_task_order(ctx.config):
            if stop.is_set():
                log.warning("stop requested; not running remaining tasks")
                break
            result = execute_with_retries(ctx, name, stop_event=stop, trigger="scheduled")
            results.append(result)
            if on_result is not None:
                on_result(result)
        repo.audit(db, actor="scheduler", action="daemon_run_once", outcome="success",
                   details={"interrupted_runs": interrupted, "released_locks": released,
                            "expired_approvals": expired,
                            "results": {r.task_name: r.status.value for r in results}})
        return results
    finally:
        _restore_signal_handlers(previous_handlers)
        lock.release()
