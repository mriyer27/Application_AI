"""CLI: ``job-agent followups ...`` (list/review/show/approve/done/dismiss). Nothing is ever sent."""

from __future__ import annotations

import typer
from rich.markup import escape
from rich.table import Table

followups_app = typer.Typer(help="Follow-up suggestions and reminders (drafts only; you send them yourself).",
                            invoke_without_command=True)


def register(app: typer.Typer) -> None:
    app.add_typer(followups_app, name="followups")


def _e(value: object) -> str:
    return escape("" if value is None else str(value))


def _print_list(status: str, show_all: bool) -> None:
    from job_agent.cli import console, fail, get_ctx
    from job_agent.followups.service import list_followups

    ctx = get_ctx()
    try:
        rows = list_followups(ctx.db, None if show_all else status)
    except ValueError:
        from job_agent.errors import PermanentError

        fail(PermanentError(f"unknown status {status!r}; use open, pending, drafted, approved, completed, dismissed"))
        return
    if not rows:
        console.print("No follow-ups" + ("" if show_all else f" with status {status}") + ".")
        return
    table = Table(header_style="bold")
    for col in ("ID", "Due (UTC)", "Type", "Status", "Job / application", "Suggested action"):
        table.add_column(col, overflow="fold")
    for r in rows:
        target = " ".join(x for x in (r["application_uid"] or r["job_uid"], r["employer"], r["title"]) if x)
        table.add_row(str(r["id"]), (r["due_at"] or "")[:16], r["followup_type"], r["status"], _e(target),
                      _e(r["suggested_action"]))
    console.print(table)


@followups_app.callback()
def followups_main(
    typer_ctx: typer.Context,
    status: str = typer.Option("open", "--status", help="open | pending | drafted | approved | completed | dismissed"),
    show_all: bool = typer.Option(False, "--all", help="Show every follow-up regardless of status."),
) -> None:
    """List follow-ups (default: open ones)."""
    if typer_ctx.invoked_subcommand is None:
        _print_list(status, show_all)


@followups_app.command("list")
def followups_list(
    status: str = typer.Option("open", "--status", help="open | pending | drafted | approved | completed | dismissed"),
    show_all: bool = typer.Option(False, "--all", help="Show every follow-up regardless of status."),
) -> None:
    """List follow-ups."""
    _print_list(status, show_all)


@followups_app.command("review")
def followups_review() -> None:
    """Apply the follow-up rules now (idempotent) and draft suggestions."""
    from job_agent.cli import console, fail, get_ctx
    from job_agent.errors import JobAgentError
    from job_agent.followups.service import review_followups

    ctx = get_ctx()
    try:
        s = review_followups(ctx)
    except JobAgentError as exc:
        fail(exc)
        return
    console.print(f"Created {s.created}, already existing {s.already_existing}, due now {s.due}"
                  + (f", suppressed by max_followups_per_application {s.skipped_limit}" if s.skipped_limit else ""))
    if s.by_type:
        console.print("New by type: " + ", ".join(f"{k}={v}" for k, v in sorted(s.by_type.items())))


def _show(row) -> None:  # type: ignore[no-untyped-def]
    from job_agent.cli import console

    console.print(f"[bold]Follow-up {row['id']}[/bold] - {row['followup_type']} ({row['status']})")
    console.print(f"Due (UTC): {row['due_at']}")
    if row["employer"] or row["title"]:
        console.print(f"Job: {_e(row['job_uid'])} {_e(row['employer'])} - {_e(row['title'])}")
    if row["application_uid"]:
        console.print(f"Application: {row['application_uid']} ({row['application_status']})")
    if row["email_subject"]:
        console.print(f"Email: {_e(row['email_subject'])}")
    console.print(f"Suggested action: {_e(row['suggested_action'])}")


def _get(followup_id: int):  # type: ignore[no-untyped-def]
    from job_agent.cli import err_console, get_ctx
    from job_agent.followups.service import get_followup

    ctx = get_ctx()
    row = get_followup(ctx.db, followup_id)
    if row is None:
        err_console.print(f"[red]Follow-up {followup_id} not found.[/red]")
        raise typer.Exit(1)
    return ctx, row


@followups_app.command("show")
def followups_show(followup_id: int = typer.Argument(..., help="Follow-up id.")) -> None:
    """Show one follow-up and its draft."""
    from job_agent.cli import console

    _ctx, row = _get(followup_id)
    _show(row)
    if row["draft_text"]:
        console.print("\n[bold]Draft[/bold] (never sent automatically):")
        console.print(_e(row["draft_text"]), highlight=False)


def _set(followup_id: int, status: str):  # type: ignore[no-untyped-def]
    from job_agent.cli import fail
    from job_agent.errors import JobAgentError
    from job_agent.followups.service import set_followup_status
    from job_agent.models import FollowupStatus

    ctx, _row = _get(followup_id)
    try:
        return set_followup_status(ctx, followup_id, FollowupStatus(status), actor="cli")
    except JobAgentError as exc:
        fail(exc)


@followups_app.command("approve")
def followups_approve(followup_id: int = typer.Argument(..., help="Follow-up id.")) -> None:
    """Approve a follow-up for manual sending and print the draft to copy (nothing is sent)."""
    from job_agent.cli import console

    row = _set(followup_id, "approved")
    console.print(f"[green]Follow-up {followup_id} approved for manual sending.[/green] "
                  "Send it yourself from your mail client, then run "
                  f"[bold]job-agent followups done {followup_id}[/bold].")
    if row is not None and row["draft_text"]:
        console.print("\n----- draft (copy) -----")
        console.print(_e(row["draft_text"]), highlight=False)
        console.print("------------------------")
    elif row is not None:
        console.print(f"(No draft for this item.) Action: {_e(row['suggested_action'])}")


@followups_app.command("done")
def followups_done(followup_id: int = typer.Argument(..., help="Follow-up id.")) -> None:
    """Mark a follow-up completed (you did it yourself)."""
    from job_agent.cli import console

    _set(followup_id, "completed")
    console.print(f"[green]Follow-up {followup_id} marked completed.[/green]")


@followups_app.command("dismiss")
def followups_dismiss(followup_id: int = typer.Argument(..., help="Follow-up id.")) -> None:
    """Dismiss a follow-up (no action needed)."""
    from job_agent.cli import console

    _set(followup_id, "dismissed")
    console.print(f"Follow-up {followup_id} dismissed.")
