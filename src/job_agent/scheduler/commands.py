"""CLI: ``job-agent daemon [--once]`` and ``job-agent schedule``."""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer
from rich.table import Table

if TYPE_CHECKING:  # pragma: no cover
    from job_agent.scheduler.daemon import DaemonStartup
    from job_agent.tasks import TaskResult

_STATUS_STYLE = {"succeeded": "green", "partial": "yellow", "skipped": "yellow", "running": "cyan",
                 "failed": "red", "interrupted": "red"}
_DAEMON_STYLE = {"running": "green", "stopped": "yellow", "stale": "red"}


def _local(iso: str | None, tz_name: str) -> str:
    from job_agent.utils.time import parse_iso, to_local

    dt = to_local(parse_iso(iso), tz_name)
    return dt.strftime("%a %Y-%m-%d %H:%M %Z") if dt else "—"


def _print_startup(report: "DaemonStartup") -> None:
    from job_agent.cli import console

    console.print(f"[bold]job-agent daemon[/bold] started (pid {report.pid}); times shown in {report.timezone}")
    recovered = []
    if report.interrupted_runs:
        recovered.append(f"{report.interrupted_runs} interrupted run(s) from a previous process marked")
    if report.released_locks:
        recovered.append(f"stale lock(s) released: {', '.join(report.released_locks)}")
    if report.expired_approvals:
        recovered.append(f"{report.expired_approvals} approval(s) expired")
    if report.recovered_submissions:
        recovered.append(f"{report.recovered_submissions} interrupted submission(s) marked unknown outcome — "
                         "check email/portal, then `job-agent applications show`")
    console.print("  Initialization: [green]ok[/green]" + (f" ({'; '.join(recovered)})" if recovered else ""))
    console.print(f"  Database: [green]ok[/green] (integrity {report.db_integrity})")
    if report.paused:
        console.print("  Automation: [yellow]PAUSED[/yellow]"
                      + (f" ({report.paused_reason})" if report.paused_reason else "")
                      + " — scheduled runs are skipped until `job-agent resume`")
    else:
        console.print("  Automation: [green]active[/green]")

    table = Table(title="Scheduled tasks", header_style="bold")
    table.add_column("Task", overflow="fold")
    table.add_column("Schedule", overflow="fold")
    table.add_column("Next run", overflow="fold")
    for info in report.scheduled:
        table.add_row(info.task_name, info.schedule, _local(info.next_run_at, report.timezone))
    console.print(table)
    for name, reason in report.skipped:
        console.print(f"  [yellow]not scheduled:[/yellow] {name} ({reason})")
    if report.catch_up:
        console.print(f"  Catch-up (missed while off/asleep/stopped): {', '.join(report.catch_up)} "
                      "— running once shortly, in this order")
    console.print(f"  Pending approvals: {report.pending_approvals}"
                  + (" — review with `job-agent approvals` or the dashboard" if report.pending_approvals else ""))
    console.print(f"  Unresolved errors: {report.unresolved_errors} ({report.errors_needing_action} need your action)"
                  + (" — see `job-agent status`" if report.unresolved_errors else ""))
    console.print("Running. Press Ctrl+C to stop (a task that is running finishes first).")


def _print_result(result: "TaskResult") -> None:
    from job_agent.cli import console

    style = _STATUS_STYLE.get(result.status.value, "white")
    line = f"  {result.task_name}: [{style}]{result.status.value}[/]"
    if result.run_uid:
        line += f" (run {result.run_uid})"
    if result.error:
        line += f" — {result.error}"
    console.print(line)


def daemon(
    once: bool = typer.Option(False, "--once",
                              help="Run every enabled scheduled task once now (health, discover_and_score, "
                                   "email_sync, followups, report), then exit."),
) -> None:
    """Run the scheduler in the foreground until Ctrl+C (or run every task once with --once)."""
    from job_agent.cli import console, fail, get_ctx
    from job_agent.errors import ConfigError, JobAgentError
    from job_agent.models import RunStatus
    from job_agent.scheduler import daemon as daemon_mod
    from job_agent.scheduler.lock import ALREADY_RUNNING_EXIT_CODE, DaemonAlreadyRunning

    ctx = get_ctx()
    try:
        if once:
            console.print("[bold]Running each enabled scheduled task once[/bold] (scheduled trigger: the pause "
                          "flag applies)")
            results = daemon_mod.run_once(ctx, on_result=_print_result)
            if not results:
                console.print("  no enabled tasks in scheduler.tasks")
            failed = [r.task_name for r in results if r.status == RunStatus.FAILED]
            if failed:
                console.print(f"[red]{len(failed)} task(s) failed:[/red] {', '.join(failed)} — see `job-agent status`")
                raise typer.Exit(1)
            return
        daemon_mod.run_daemon(ctx, on_started=_print_startup)
        console.print("Daemon stopped.")
    except DaemonAlreadyRunning as exc:
        fail(exc, code=ALREADY_RUNNING_EXIT_CODE)
    except ConfigError as exc:
        fail(exc, code=2)
    except JobAgentError as exc:
        fail(exc)


def schedule(
    count: int = typer.Option(3, "--count", "-n", min=1, max=20, help="Upcoming runs to show per task."),
) -> None:
    """Show the configured schedule, upcoming runs, and the daemon/task status."""
    from job_agent.cli import console, get_ctx
    from job_agent.scheduler.status import preview_schedule, scheduler_status

    ctx = get_ctx()
    cfg = ctx.config
    status = scheduler_status(ctx.db, cfg)
    preview = preview_schedule(cfg, count=count)
    tz = cfg.app.timezone

    d = status["daemon"]
    state = d["state"]
    line = f"[bold]Daemon:[/bold] [{_DAEMON_STYLE.get(state, 'white')}]{state}[/]"
    if state == "running":
        line += f" (pid {d['pid']}, heartbeat {d['heartbeat_age_seconds']} s ago, since {_local(d['started_at'], tz)})"
    elif state == "stale":
        line += (f" (last heartbeat {_local(d['heartbeat_at'], tz)}: killed, crashed or the laptop is asleep; "
                 "start it with `job-agent daemon`)")
    else:
        line += (f" (stopped {_local(d['stopped_at'], tz)})" if d["stopped_at"] else "") + \
            " — start it with `job-agent daemon`"
    console.print(line)
    if status["paused"]:
        console.print("[bold]Automation:[/bold] [yellow]PAUSED[/yellow]"
                      + (f" ({status['paused_reason']})" if status["paused_reason"] else "")
                      + " — scheduled runs are skipped; `job-agent resume` to continue")
    else:
        console.print("[bold]Automation:[/bold] [green]active[/green]")
    if not status["scheduler_enabled"]:
        console.print("[yellow]scheduler.enabled is false in config: the daemon will not start.[/yellow]")

    table = Table(title=f"Schedule ({tz})", header_style="bold")
    for col in ("Task", "Schedule", "Enabled", "Upcoming", "Last run", "Last status", "Last success", "Fails",
                "Lock"):
        table.add_column(col, overflow="fold")
    for name, t in status["tasks"].items():
        if not t["in_config"] and not t["last_run_at"]:
            continue
        upcoming = list(preview.get(name) or [])
        entry = cfg.scheduler.tasks.get(name)
        if t["next_run_at"]:  # the running daemon's real next run
            if entry is not None and entry.interval_minutes is not None:
                upcoming = [t["next_run_at"]]
            else:
                upcoming = [t["next_run_at"]] + [iso for iso in upcoming if iso > t["next_run_at"]][:count - 1]
        if not t["known_task"]:
            upcoming_text = "[red]unknown task name[/red]"
        elif not t["enabled"]:
            upcoming_text = "disabled" if t["in_config"] else "manual only"
        elif name in preview and not preview[name]:
            upcoming_text = "[red]invalid schedule[/red]"
        else:
            upcoming_text = "\n".join(_local(iso, tz) for iso in upcoming)
            if entry is not None and entry.interval_minutes is not None and state != "running":
                upcoming_text += "\n(if started now)"
        status_value = t["last_status"] or "—"
        table.add_row(
            name, t["schedule"] or "—", "yes" if t["enabled"] else "no", upcoming_text,
            _local(t["last_run_at"], tz), f"[{_STATUS_STYLE.get(status_value, 'white')}]{status_value}[/]",
            _local(t["last_success_at"], tz), str(t["consecutive_failures"]),
            "held" if t["lock_held"] else "—",
        )
    console.print(table)
    if any(t["restart_needed"] for t in status["tasks"].values()):
        console.print("[yellow]The config changed since the daemon started; restart it to apply.[/yellow]")


def register(app: typer.Typer) -> None:
    app.command("daemon")(daemon)
    app.command("schedule")(schedule)
