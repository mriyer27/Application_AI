"""Command-line interface (``job-agent``).

Only commands that are actually implemented are registered. Module commands are
attached by ``_register_module_commands`` from each module's ``commands.py``.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from job_agent import __version__
from job_agent.errors import JobAgentError

app = typer.Typer(
    name="job-agent",
    help="Local-first, human-supervised AI job application agent.\n\n"
    "Nothing is ever submitted or sent without your explicit approval.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
db_app = typer.Typer(help="Database maintenance (backup, info).", no_args_is_help=True)
tasks_app = typer.Typer(help="List and run predefined agent tasks.", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(tasks_app, name="tasks")

console = Console()
err_console = Console(stderr=True)

STATUS_STYLE = {"ok": "green", "warn": "yellow", "fail": "red", "info": "cyan"}


class State:
    config_path: Optional[Path] = None
    verbose: bool = False
    _ctx = None

    def context(self):
        if self._ctx is None:
            from job_agent.context import AppContext

            self._ctx = AppContext.load(self.config_path, console_level="INFO" if self.verbose else "WARNING")
        return self._ctx


state = State()


def get_ctx():
    """Load the application context or exit with a readable error."""
    try:
        return state.context()
    except JobAgentError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        if exc.human_action:
            err_console.print(f"[yellow]Next step:[/yellow] {exc.human_action}")
        raise typer.Exit(2)


def fail(exc: JobAgentError, code: int = 1) -> None:
    err_console.print(f"[red]Error ({exc.kind.value}):[/red] {exc}")
    if exc.human_action:
        err_console.print(f"[yellow]Next step:[/yellow] {exc.human_action}")
    raise typer.Exit(code)


def _version_cb(value: bool) -> None:
    if value:
        console.print(f"job-agent {__version__}")
        raise typer.Exit()


@app.callback()
def main_callback(
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Path to config YAML.", envvar=None),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show info-level logs on the console."),
    version: bool = typer.Option(False, "--version", callback=_version_cb, is_eager=True, help="Show version."),
) -> None:
    state.config_path = config
    state.verbose = verbose
    state._ctx = None


# --------------------------------------------------------------------------- #
# init / doctor / status
# --------------------------------------------------------------------------- #
@app.command()
def init(
    home: Optional[Path] = typer.Option(None, help="Project home (default: JOB_AGENT_HOME or current directory)."),
) -> None:
    """Create local config, profile, .env and database from the examples (never overwrites)."""
    from job_agent.config import resolve_home
    from job_agent.security.envfile import generate_secret, set_env_var

    root = (home or resolve_home()).resolve()
    actions = []
    pairs = [
        (root / "config" / "config.example.yaml", root / "config" / "config.yaml"),
        (root / "profiles" / "candidate_profile.example.yaml", root / "profiles" / "candidate_profile.yaml"),
    ]
    for src, dst in pairs:
        if dst.exists():
            actions.append(f"kept existing {dst.relative_to(root)}")
        elif src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            actions.append(f"created {dst.relative_to(root)} from example")
        else:
            err_console.print(f"[red]missing example file {src}[/red]")
            raise typer.Exit(2)
    env_path = root / ".env"
    import os

    if not os.environ.get("JOB_AGENT_SESSION_SECRET"):
        existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        if "JOB_AGENT_SESSION_SECRET=" not in existing:
            set_env_var(env_path, "JOB_AGENT_SESSION_SECRET", generate_secret())
            actions.append("generated JOB_AGENT_SESSION_SECRET in .env (chmod 600)")
    state.config_path = root / "config" / "config.yaml"
    os.environ.setdefault("JOB_AGENT_HOME", str(root))
    ctx = get_ctx()
    ctx.db  # creates + migrates
    actions.append(f"database ready at {ctx.config.db_path.relative_to(root)}")
    for a in actions:
        console.print(f"  [green]•[/green] {a}")
    console.print("\nNext: edit [bold]profiles/candidate_profile.yaml[/bold] and [bold]config/config.yaml[/bold], "
                  "then run [bold]job-agent doctor[/bold].")


def _print_checks(checks) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")
    table.add_column("Next step", overflow="fold")
    for c in checks:
        table.add_row(c.name, f"[{STATUS_STYLE.get(c.status, 'white')}]{c.status.upper()}[/]", c.detail,
                      c.action or "")
    console.print(table)


@app.command()
def doctor() -> None:
    """Run health checks: config, database, profile, Ollama, Gmail, browser, scheduler."""
    from job_agent.health import FAIL, overall, run_checks

    ctx = get_ctx()
    checks = run_checks(ctx)
    _print_checks(checks)
    result = overall(checks)
    console.print(f"\nOverall: [{STATUS_STYLE[result]}]{result.upper()}[/]")
    if result == FAIL:
        raise typer.Exit(1)


@app.command()
def status() -> None:
    """Show agent status: runs, pending approvals, errors needing attention."""
    from job_agent.database import repo

    ctx = get_ctx()
    db = ctx.db
    paused = repo.is_paused(db)
    console.print(f"[bold]Automation:[/bold] {'[yellow]PAUSED[/yellow]' if paused else '[green]active[/green]'}"
                  + (f" ({repo.get_state(db, 'automation_paused_reason')})" if paused and
                     repo.get_state(db, 'automation_paused_reason') else ""))
    last = repo.last_successful_run(db)
    console.print(f"[bold]Last successful run:[/bold] "
                  + (f"{last['task_name']} at {last['finished_at']}" if last else "none yet"))
    counts = {
        "Jobs (total)": db.scalar("SELECT COUNT(*) FROM jobs"),
        "Jobs shortlisted": db.scalar("SELECT COUNT(*) FROM jobs WHERE pipeline_state = 'shortlisted'"),
        "Applications": db.scalar("SELECT COUNT(*) FROM applications"),
        "Pending approvals": db.scalar("SELECT COUNT(*) FROM approval_requests WHERE status = 'pending'"),
        "Pending follow-ups": db.scalar("SELECT COUNT(*) FROM followups WHERE status IN ('pending','drafted')"),
        "Unresolved errors": db.scalar("SELECT COUNT(*) FROM errors WHERE resolved = 0"),
    }
    for k, v in counts.items():
        console.print(f"[bold]{k}:[/bold] {v}")
    runs = db.query_all("SELECT task_name, trigger, status, started_at, finished_at, error_message "
                        "FROM agent_runs ORDER BY id DESC LIMIT 8")
    if runs:
        t = Table(title="Recent runs", header_style="bold")
        for col in ("Task", "Trigger", "Status", "Started (UTC)", "Error"):
            t.add_column(col, overflow="fold")
        for r in runs:
            t.add_row(r["task_name"], r["trigger"], r["status"], r["started_at"], (r["error_message"] or "")[:80])
        console.print(t)
    errors = repo.unresolved_errors(db, limit=8)
    if errors:
        t = Table(title="Errors needing attention", header_style="bold red")
        for col in ("ID", "When (UTC)", "Component", "Kind", "Message", "Next step"):
            t.add_column(col, overflow="fold")
        for e in errors:
            t.add_row(str(e["id"]), e["occurred_at"], e["component"], e["error_kind"], e["message"][:100],
                      e["human_action"] or "")
        console.print(t)


@app.command()
def pause(reason: Optional[str] = typer.Option(None, help="Why automation is paused.")) -> None:
    """Pause scheduled automation (manual commands still work)."""
    from job_agent.database import repo

    ctx = get_ctx()
    repo.set_paused(ctx.db, True, actor="cli", reason=reason)
    console.print("[yellow]Automation paused.[/yellow] Scheduled tasks will be skipped until `job-agent resume`.")


@app.command()
def resume() -> None:
    """Resume scheduled automation."""
    from job_agent.database import repo

    ctx = get_ctx()
    repo.set_paused(ctx.db, False, actor="cli")
    console.print("[green]Automation resumed.[/green]")


@app.command("config-check")
def config_check(show: bool = typer.Option(False, "--show", help="Print the effective (non-secret) config.")) -> None:
    """Validate the configuration and candidate profile files."""
    import yaml

    ctx = get_ctx()
    console.print(f"[green]Config OK[/green] ({ctx.config.source_path})"
                  + (" [yellow](example config)[/yellow]" if ctx.config.using_example else ""))
    try:
        profile = ctx.profile
        console.print(f"[green]Profile OK[/green] ({len(profile.all_facts())} facts, {len(profile.skills)} skills)")
        if profile.placeholders():
            console.print(f"[yellow]Profile still has {len(profile.placeholders())} placeholder value(s).[/yellow]")
    except JobAgentError as exc:
        fail(exc, code=2)
    if show:
        console.print(yaml.safe_dump(ctx.config.safe_dict(), sort_keys=False))


# --------------------------------------------------------------------------- #
# db
# --------------------------------------------------------------------------- #
@db_app.command("backup")
def db_backup(dest: Optional[Path] = typer.Option(None, help="Destination file (default: data/backups/...).")) -> None:
    """Create a consistent online backup of the SQLite database."""
    from job_agent.utils.time import utcnow

    ctx = get_ctx()
    target = dest or (ctx.config.data_dir / "backups" / f"job_agent_{utcnow().strftime('%Y%m%dT%H%M%SZ')}.db")
    path = ctx.db.backup(target)
    console.print(f"[green]Backup written:[/green] {path}")


@db_app.command("info")
def db_info() -> None:
    """Show schema version and row counts."""
    ctx = get_ctx()
    db = ctx.db
    console.print(f"Path: {ctx.config.db_path}")
    console.print(f"Schema version: {db.scalar('SELECT MAX(version) FROM schema_migrations')}")
    console.print(f"Integrity: {db.integrity_check()}")
    t = Table(header_style="bold")
    t.add_column("Table")
    t.add_column("Rows", justify="right")
    for row in db.query_all("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        t.add_row(row["name"], str(db.scalar(f"SELECT COUNT(*) FROM \"{row['name']}\"")))
    console.print(t)


# --------------------------------------------------------------------------- #
# tasks
# --------------------------------------------------------------------------- #
@tasks_app.command("list")
def tasks_list() -> None:
    """List predefined tasks."""
    from job_agent.tasks import TASKS

    t = Table(header_style="bold")
    t.add_column("Task")
    t.add_column("Description")
    for spec in TASKS.values():
        t.add_row(spec.name, spec.description)
    console.print(t)


@tasks_app.command("run")
def tasks_run(name: str = typer.Argument(..., help="Task name (see `job-agent tasks list`).")) -> None:
    """Run one predefined task now (manual trigger; ignores the pause flag)."""
    from job_agent.models import RunStatus
    from job_agent.tasks import run_task

    ctx = get_ctx()
    try:
        result = run_task(ctx, name, trigger="manual")
    except JobAgentError as exc:
        fail(exc)
        return
    style = {"succeeded": "green", "partial": "yellow", "skipped": "yellow"}.get(result.status.value, "red")
    console.print(f"[{style}]{name}: {result.status.value}[/] (run {result.run_uid})")
    if result.summary:
        console.print(result.summary)
    if result.error:
        console.print(f"[red]{result.error}[/red]")
    if result.status == RunStatus.FAILED:
        raise typer.Exit(1)


# --------------------------------------------------------------------------- #
def _register_module_commands() -> None:
    """Attach commands from implemented modules. Each module exposes ``register(app)``."""
    import importlib

    for module_name in _MODULE_COMMANDS:
        module = importlib.import_module(module_name)
        module.register(app)


#: Modules whose ``commands.register(app)`` adds CLI commands. Only implemented
#: modules are listed here.
_MODULE_COMMANDS: list[str] = []

_register_module_commands()


def main() -> None:
    try:
        app()
    except JobAgentError as exc:  # last-resort readable error
        err_console.print(f"[red]Error ({exc.kind.value}):[/red] {exc}")
        if exc.human_action:
            err_console.print(f"[yellow]Next step:[/yellow] {exc.human_action}")
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
