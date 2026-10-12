"""CLI commands: ``job-agent report`` and ``job-agent reports``."""

from __future__ import annotations

import re
from datetime import date
from typing import Optional

import typer

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: (label, stats key) pairs printed after ``report``.
_KEY_COUNTS = (
    ("New jobs today", "new_jobs_today"),
    ("Total jobs", "total_jobs"),
    ("Shortlisted", "shortlisted"),
    ("Prepared today", "applications_prepared_today"),
    ("Awaiting approval", "awaiting_approval"),
    ("Submitted today (evidence-based)", "submitted_today"),
    ("Submitted total (evidence-based)", "submitted"),
    ("Interviews", "interviews"),
    ("Pending follow-ups", "pending_followups"),
    ("Errors needing attention", "errors_requiring_attention"),
)


def _parse_date(value: Optional[str]) -> Optional[date]:
    if value is None:
        return None
    if not _DATE_RE.match(value):
        raise typer.BadParameter("use the format YYYY-MM-DD", param_hint="--date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--date") from None


def report_command(
    date_: Optional[str] = typer.Option(None, "--date", help="Local report date YYYY-MM-DD (default: today "
                                                             "in app.timezone)."),
) -> None:
    """Generate the Excel report and Markdown summary (regenerating a date overwrites only that date)."""
    from rich.table import Table

    from job_agent.cli import console, fail, get_ctx
    from job_agent.errors import JobAgentError
    from job_agent.reporting.service import generate_daily_report

    report_date = _parse_date(date_)
    ctx = get_ctx()
    try:
        result = generate_daily_report(ctx, report_date, actor="cli")
    except JobAgentError as exc:
        fail(exc)
        return
    console.print(f"[green]Report for {result.report_date.isoformat()}[/green] "
                  f"(generation {result.generation_count})")
    console.print(f"Excel:    {result.xlsx_path}", soft_wrap=True, highlight=False)
    console.print(f"Summary:  {result.markdown_path}", soft_wrap=True, highlight=False)
    counts = result.stats.get("counts", {})
    table = Table(header_style="bold")
    table.add_column("Metric")
    table.add_column("Count", justify="right")
    for label, key in _KEY_COUNTS:
        table.add_row(label, str(counts.get(key, 0)))
    console.print(table)


def reports_command(
    limit: int = typer.Option(30, "--limit", min=1, max=1000, help="How many report dates to show."),
) -> None:
    """List generated reports (report history)."""
    from rich.table import Table

    from job_agent.cli import console, get_ctx
    from job_agent.reporting.service import list_reports
    from job_agent.reporting.text import local_text

    ctx = get_ctx()
    rows = list_reports(ctx, limit)
    if not rows:
        console.print("No reports generated yet. Run `job-agent report`.")
        return
    tz = ctx.config.app.timezone
    table = Table(title=f"Reports in {ctx.config.reports_dir}", header_style="bold")
    for col in ("Date", "Generations", f"Last generated ({tz})", "Excel", "Markdown"):
        table.add_column(col, overflow="fold")
    for r in rows:
        table.add_row(
            r["report_date"], str(r["generation_count"]), local_text(r["last_generated_at"], tz),
            (r["xlsx_file"] or "") + ("" if r["xlsx_file_exists"] else " (missing)"),
            (r["markdown_file"] or "") + ("" if r["markdown_file_exists"] else " (missing)"),
        )
    console.print(table)


def register(app: typer.Typer) -> None:
    app.command("report")(report_command)
    app.command("reports")(reports_command)
