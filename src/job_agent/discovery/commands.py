"""CLI commands: ``discover``, ``sources`` and ``sources check``."""

from __future__ import annotations

from typing import Optional

import typer
from rich.table import Table


def register(app: typer.Typer) -> None:
    sources_app = typer.Typer(help="Configured job sources: status and reachability.",
                              invoke_without_command=True, no_args_is_help=False)

    @app.command()
    def discover(
        source: Optional[list[str]] = typer.Option(
            None, "--source", "-s",
            help="Source to run, e.g. 'greenhouse' or 'greenhouse:capco' (repeatable). Default: all enabled. "
                 "'fixture:<file.json>' runs the MOCK fixture source.",
        ),
        limit: Optional[int] = typer.Option(None, "--limit", "-n", min=1,
                                            help="Max new + updated jobs to store (default search.max_jobs_per_run)."),
    ) -> None:
        """Fetch jobs from public job-board APIs, filter, deduplicate and store them."""
        from job_agent.cli import console, fail, get_ctx
        from job_agent.discovery.service import discover_jobs
        from job_agent.errors import JobAgentError

        ctx = get_ctx()
        try:
            result = discover_jobs(ctx, sources=source or None, limit=limit, actor="cli")
        except JobAgentError as exc:
            fail(exc)
            return
        table = Table(title="Discovery", header_style="bold")
        for col in ("Source", "Fetched", "Relevant", "New", "Updated", "Unchanged", "Dup", "Filtered", "Closed",
                    "Status"):
            table.add_column(col, justify="left" if col in ("Source", "Status") else "right", overflow="fold")
        for s in result.sources:
            if s.error:
                status = f"[red]FAILED[/red] {s.error}"
            elif s.skipped:
                status = f"[yellow]skipped[/yellow] ({s.skipped})"
            else:
                bits = ["[green]ok[/green]"]
                if s.is_mock:
                    bits.append("[magenta]MOCK[/magenta]")
                if not s.complete:
                    bits.append("[yellow]incomplete[/yellow]")
                if s.deferred:
                    bits.append(f"{s.deferred} deferred")
                if s.invalid:
                    bits.append(f"{s.invalid} invalid")
                status = " ".join(bits)
            table.add_row(s.key, str(s.fetched), str(s.relevant), str(s.new), str(s.updated), str(s.unchanged),
                          str(s.duplicates), str(s.filtered), str(s.closed), status)
        t = result.totals
        table.add_row("[bold]Total[/bold]", str(t.fetched), str(t.relevant), str(t.new), str(t.updated),
                      str(t.unchanged), str(t.duplicates), str(t.filtered), str(t.closed),
                      "[yellow]partial[/yellow]" if result.partial else "", end_section=True)
        console.print(table)
        if result.limit_reached:
            console.print(f"[yellow]Run limit of {result.limit} new/updated jobs reached; the rest will be "
                          "picked up next run.[/yellow]")
        if result.failed_sources:
            console.print(f"[yellow]{len(result.failed_sources)} source(s) failed; details in "
                          "`job-agent status`.[/yellow]")
        attempted = [s for s in result.sources if not s.skipped]
        if attempted and all(s.error for s in attempted):
            raise typer.Exit(1)

    @sources_app.callback()
    def sources(ctx: typer.Context) -> None:
        """List configured sources with last success and failure counts."""
        if ctx.invoked_subcommand is not None:
            return
        from job_agent.cli import console, get_ctx
        from job_agent.discovery.service import list_sources

        app_ctx = get_ctx()
        rows = list_sources(app_ctx)
        table = Table(title="Job sources", header_style="bold")
        for col in ("Source", "Identifier", "Enabled", "Active jobs", "Last success (UTC)", "Failures",
                    "Last error"):
            table.add_column(col, overflow="fold")
        for r in rows:
            enabled = "yes" if r.enabled else ("no" if r.configured else "not configured")
            if r.is_mock:
                enabled += " (MOCK)"
            table.add_row(r.source, r.identifier, enabled, str(r.active_jobs), r.last_success_at or "never",
                          str(r.consecutive_failures), (r.last_error or "")[:120])
        console.print(table)
        if not rows:
            console.print("No sources configured (see sources.* in config and docs/sources.md).")

    @sources_app.command("check")
    def sources_check(
        source: Optional[list[str]] = typer.Option(None, "--source", "-s",
                                                   help="Only check this source or source:identifier."),
    ) -> None:
        """Make one lightweight live request per configured identifier and report reachability."""
        from job_agent.cli import console, fail, get_ctx
        from job_agent.discovery.service import check_sources
        from job_agent.errors import JobAgentError

        app_ctx = get_ctx()
        try:
            results = check_sources(app_ctx, sources=source or None)
        except JobAgentError as exc:
            fail(exc)
            return
        table = Table(title="Source reachability", header_style="bold")
        for col in ("Source", "Identifier", "Status", "Detail"):
            table.add_column(col, overflow="fold")
        for r in results:
            table.add_row(r.source, r.identifier, "[green]OK[/green]" if r.ok else "[red]FAIL[/red]", r.detail)
        console.print(table)
        if any(not r.ok for r in results):
            raise typer.Exit(1)

    app.add_typer(sources_app, name="sources")
