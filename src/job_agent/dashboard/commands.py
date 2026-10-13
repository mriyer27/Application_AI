"""CLI: ``job-agent dashboard`` (serve) and ``job-agent dashboard set-password``."""

from __future__ import annotations

import os
import re
from pathlib import Path

import typer

#: Wildcard binds expose the dashboard on every interface (including public Wi-Fi).
WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", "[::]", "*", ""})
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._@\-]{1,64}$")
_SESSION_LINE_RE = re.compile(r"^\s*(export\s+)?JOB_AGENT_SESSION_SECRET\s*=\s*\S", re.M)
MAX_PASSWORD_CHARS = 1024


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def serve() -> None:
    """Start the dashboard with uvicorn on dashboard.host/port from the config."""
    from job_agent.cli import console, err_console, fail, get_ctx
    from job_agent.dashboard.app import create_app
    from job_agent.dashboard.auth import is_loopback
    from job_agent.errors import JobAgentError

    ctx = get_ctx()
    cfg = ctx.config.dashboard
    host, port = cfg.host.strip(), cfg.port
    if host in WILDCARD_HOSTS:
        err_console.print(f"[red]Refusing to bind the dashboard to {host or '(empty)'}[/red]: that exposes it on every "
                          "network interface. Use 127.0.0.1 (with `tailscale serve`) or your Tailscale 100.x IP. "
                          "See docs/remote-access.md.")
        raise typer.Exit(2)
    try:
        web_app = create_app(ctx_factory=ctx.fork)
    except JobAgentError as exc:  # HumanActionRequired when the login is not configured
        fail(exc, code=2)
        return
    if not is_loopback(host):
        err_console.print(
            f"[bold red]WARNING: the dashboard will listen on {host}, not on localhost.[/bold red]\n"
            "[red]Anyone who can reach this address can try to log in. Only do this on a private VPN address "
            "(e.g. your Tailscale 100.x IP), never on public or shared Wi-Fi, and never port-forward it. "
            "See docs/remote-access.md.[/red]"
        )
    url = f"http://{_url_host(host)}:{port}/"
    console.print(f"[green]Dashboard:[/green] {url}  (Ctrl+C to stop)")
    if cfg.secure_cookies:
        console.print("[yellow]secure_cookies is on:[/yellow] the browser only sends the session cookie over HTTPS "
                      "(e.g. via `tailscale serve`); a plain http:// URL will not keep you signed in.")
    import uvicorn

    uvicorn.run(web_app, host=host, port=port, log_level="warning", proxy_headers=False, server_header=False,
                access_log=False)


def set_password() -> None:
    """Set the dashboard username and password (stored as an scrypt hash in <home>/.env)."""
    from job_agent.cli import console, err_console
    from job_agent.config import resolve_home
    from job_agent.security.envfile import generate_secret, set_env_var
    from job_agent.security.passwords import hash_password

    username = typer.prompt("Dashboard username").strip()
    if not _USERNAME_RE.match(username):
        err_console.print("[red]Username must be 1-64 characters: letters, digits, . _ @ -[/red]")
        raise typer.Exit(2)
    password = typer.prompt("Dashboard password (at least 12 characters)", hide_input=True,
                            confirmation_prompt=True)
    if len(password) < 12 or len(password) > MAX_PASSWORD_CHARS:
        err_console.print(f"[red]Password must be 12-{MAX_PASSWORD_CHARS} characters. Nothing was changed.[/red]")
        raise typer.Exit(2)
    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        err_console.print(f"[red]{exc}. Nothing was changed.[/red]")
        raise typer.Exit(2) from None

    env_path: Path = resolve_home() / ".env"
    set_env_var(env_path, "JOB_AGENT_DASHBOARD_USERNAME", username)
    set_env_var(env_path, "JOB_AGENT_DASHBOARD_PASSWORD_HASH", password_hash)
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    created_secret = False
    if not os.environ.get("JOB_AGENT_SESSION_SECRET") and not _SESSION_LINE_RE.search(existing):
        set_env_var(env_path, "JOB_AGENT_SESSION_SECRET", generate_secret())
        created_secret = True
    console.print(f"[green]Dashboard login saved[/green] to {env_path} (mode 600; the password itself is not stored).")
    if created_secret:
        console.print("Generated JOB_AGENT_SESSION_SECRET.")
    console.print("Restart the dashboard for the change to take effect: [bold]job-agent dashboard[/bold]")


def register(app: typer.Typer) -> None:
    dashboard_app = typer.Typer(help="Local web dashboard for monitoring and approvals (localhost by default).")

    @dashboard_app.callback(invoke_without_command=True)
    def dashboard(ctx: typer.Context) -> None:
        """Start the dashboard (no subcommand), or manage its login."""
        if ctx.invoked_subcommand is None:
            serve()

    dashboard_app.command("set-password")(set_password)
    app.add_typer(dashboard_app, name="dashboard")
