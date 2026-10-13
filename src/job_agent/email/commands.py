"""CLI: ``job-agent gmail ...`` (auth/status/sync/revoke) and ``job-agent emails ...``."""

from __future__ import annotations

from typing import Optional

import typer
from rich.markup import escape
from rich.table import Table

gmail_app = typer.Typer(help="Gmail (read-only): authorize, status, sync, revoke the local token.",
                        no_args_is_help=True)
emails_app = typer.Typer(help="Synced emails: list (default), show, associate, mark handled.",
                         invoke_without_command=True)


def register(app: typer.Typer) -> None:
    app.add_typer(gmail_app, name="gmail")
    app.add_typer(emails_app, name="emails")


def _e(value: object) -> str:
    """Escape untrusted text (subjects, senders) so it is never interpreted as rich markup."""
    return escape("" if value is None else str(value))


# --------------------------------------------------------------------------- #
# gmail
# --------------------------------------------------------------------------- #
@gmail_app.command("auth")
def gmail_auth(
    no_browser: bool = typer.Option(False, "--no-browser", help="Print the consent URL instead of opening a browser."),
) -> None:
    """Authorize read-only Gmail access in your browser (OAuth; your password is never asked for here)."""
    from job_agent.cli import console, fail, get_ctx
    from job_agent.email import auth
    from job_agent.errors import JobAgentError

    ctx = get_ctx()
    console.print("Opening Google's consent screen for [bold]read-only[/bold] Gmail access "
                  f"({', '.join(ctx.config.gmail.scopes)}).")
    try:
        result = auth.authorize(ctx, open_browser=not no_browser)
    except JobAgentError as exc:
        fail(exc)
        return
    try:
        shown = result.token_path.relative_to(ctx.config.home)
    except ValueError:
        shown = result.token_path
    console.print(f"[green]Authorized.[/green] Token saved to {shown} (permissions 0600).")
    console.print(f"Granted scopes: {', '.join(result.scopes)}")
    console.print("[yellow]Note:[/yellow] while your Google Cloud app is in 'Testing' mode, Google expires the "
                  "authorization after 7 days; re-run [bold]job-agent gmail auth[/bold] when sync reports it.")
    console.print("Next: [bold]job-agent gmail sync[/bold]")


@gmail_app.command("status")
def gmail_status_cmd() -> None:
    """Show whether Gmail is connected (only after a successful sync since the last auth error)."""
    from job_agent.cli import console, get_ctx
    from job_agent.email.auth import gmail_status

    ctx = get_ctx()
    st = gmail_status(ctx)
    table = Table(show_header=False)
    table.add_column("Item", style="bold")
    table.add_column("Value", overflow="fold")
    state = "[green]CONNECTED[/green]" if st.connected else "[yellow]NOT CONNECTED[/yellow]"
    table.add_row("Connection", f"{state} - {_e(st.detail)}")
    table.add_row("Enabled in config", "yes" if st.enabled else "no (gmail.enabled: false)")
    table.add_row("Gmail libraries", "installed" if st.libraries_installed else "missing")
    table.add_row("OAuth client file", "present" if st.client_file_present else "missing")
    table.add_row("Token", "present" if st.token_present else "none")
    table.add_row("Granted scopes", _e(", ".join(st.scopes)) or "-")
    table.add_row("Authorized at (UTC)", st.authorized_at or "-")
    table.add_row("Last successful sync (UTC)", st.last_success_at or "never")
    table.add_row("Authorization error", _e(f"{st.auth_error} (at {st.auth_error_at})") if st.auth_error else "none")
    if st.next_step:
        table.add_row("Next step", _e(st.next_step))
    console.print(table)


@gmail_app.command("sync")
def gmail_sync() -> None:
    """Read recent messages (read-only), categorize them and apply evidence-based status updates."""
    from job_agent.cli import console, err_console, fail, get_ctx
    from job_agent.email.service import ensure_gmail_enabled
    from job_agent.errors import JobAgentError
    from job_agent.models import RunStatus
    from job_agent.tasks import run_task

    ctx = get_ctx()
    try:
        ensure_gmail_enabled(ctx.config)  # a plain message, not a failed task run with a traceback
    except JobAgentError as exc:
        fail(exc)
        return
    result = run_task(ctx, "email_sync", trigger="manual")
    if result.status == RunStatus.FAILED:
        err_console.print(f"[red]Gmail sync failed ({result.error_kind.value if result.error_kind else 'error'}):"
                          f"[/red] {_e(result.error)}")
        action = ctx.db.scalar(
            "SELECT human_action FROM errors WHERE run_id = (SELECT id FROM agent_runs WHERE run_uid = ?) "
            "ORDER BY id DESC LIMIT 1", (result.run_uid,))
        if action:
            err_console.print(f"[yellow]Next step:[/yellow] {_e(action)}")
        raise typer.Exit(1)
    if result.status == RunStatus.SKIPPED:
        console.print(f"[yellow]Skipped:[/yellow] {_e(result.summary.get('reason'))}")
        return
    s = result.summary
    table = Table(title="Gmail sync", header_style="bold")
    for col in ("Listed", "New", "Already stored", "Linked", "Status updates", "Need attention", "Errors"):
        table.add_column(col, justify="right")
    table.add_row(*(str(x) for x in (s.get("listed", 0), s.get("new", 0), s.get("skipped_existing", 0),
                                     s.get("associated", 0), s.get("status_updates", 0), s.get("attention", 0),
                                     len(s.get("errors") or []))))
    console.print(table)
    if s.get("by_category"):
        console.print("By category: " + ", ".join(f"{k}={v}" for k, v in sorted(s["by_category"].items())))
    if s.get("injection_flagged"):
        console.print(f"[yellow]{s['injection_flagged']} message(s) contained instruction-like text; they were "
                      "flagged and not acted on automatically.[/yellow]")
    for err in s.get("errors") or []:
        console.print(f"[red]error:[/red] {_e(err)}")
    if result.status == RunStatus.PARTIAL:
        console.print("[yellow]Partial sync; see `job-agent status` for details.[/yellow]")


@gmail_app.command("revoke")
def gmail_revoke() -> None:
    """Delete the local Gmail token (then remove the app's access in your Google account)."""
    from job_agent.cli import console, get_ctx
    from job_agent.email.auth import REVOKE_URL, revoke_local

    ctx = get_ctx()
    existed = revoke_local(ctx)
    console.print("[green]Local token deleted.[/green]" if existed else "No local token was stored.")
    console.print(f"To revoke the grant at Google too, open {REVOKE_URL} and remove this app's access.")


# --------------------------------------------------------------------------- #
# emails
# --------------------------------------------------------------------------- #
def _print_emails(attention: bool, category: Optional[str], limit: int) -> None:
    from job_agent.cli import console, get_ctx
    from job_agent.email.repository import list_messages

    ctx = get_ctx()
    rows = list_messages(ctx.db, attention_only=attention, category=category, limit=limit)
    if not rows:
        console.print("No emails needing attention." if attention
                      else "No emails synced yet (run `job-agent gmail sync`).")
        return
    table = Table(header_style="bold")
    for col in ("ID", "Received (UTC)", "From", "Subject", "Category", "Attn", "Linked to"):
        table.add_column(col, overflow="fold")
    for r in rows:
        linked = ""
        if r["assoc_job_id"] is not None:
            target = r["application_uid"] or r["job_uid"] or ""
            linked = f"{target} {r['employer'] or ''} ({r['assoc_confidence']:.2f}"
            linked += ", confirmed)" if r["assoc_confirmed"] else ")"
        conf = f" {r['category_confidence']:.2f}" if r["category_confidence"] is not None else ""
        table.add_row(str(r["id"]), (r["received_at"] or "")[:16], _e(r["from_domain"]), _e((r["subject"] or "")[:80]),
                      f"{r['category']}{conf}", "!" if r["requires_attention"] else "", _e(linked))
    console.print(table)


@emails_app.callback()
def emails_main(
    typer_ctx: typer.Context,
    attention: bool = typer.Option(False, "--attention", help="Only emails needing your attention."),
    category: Optional[str] = typer.Option(None, "--category", help="Filter by category."),
    limit: int = typer.Option(50, "--limit", min=1, max=1000),
) -> None:
    """List synced emails (default)."""
    if typer_ctx.invoked_subcommand is None:
        _print_emails(attention, category, limit)


@emails_app.command("list")
def emails_list(
    attention: bool = typer.Option(False, "--attention", help="Only emails needing your attention."),
    category: Optional[str] = typer.Option(None, "--category", help="Filter by category."),
    limit: int = typer.Option(50, "--limit", min=1, max=1000),
) -> None:
    """List synced emails."""
    _print_emails(attention, category, limit)


@emails_app.command("show")
def emails_show(email_id: int = typer.Argument(..., help="Email id (see `job-agent emails`).")) -> None:
    """Show one email's metadata, category, associations and suggested reply."""
    from job_agent.cli import console, err_console, get_ctx
    from job_agent.email.repository import associations_for, get_message
    from job_agent.email.repository import last_review_reasons as review_reasons

    ctx = get_ctx()
    row = get_message(ctx.db, email_id)
    if row is None:
        err_console.print(f"[red]Email {email_id} not found.[/red]")
        raise typer.Exit(1)
    console.print(f"[bold]Email {row['id']}[/bold]  (Gmail id {_e(row['gmail_message_id'])})")
    console.print(f"From: {_e(row['from_address'])}")
    console.print(f"Received (UTC): {row['received_at'] or '-'}")
    console.print(f"Subject: {_e(row['subject'])}")
    console.print(f"Category: {row['category']} (confidence {row['category_confidence']}, {_e(row['classifier'])})")
    console.print(f"Summary: {_e(row['summary'] or '-')}")
    if row["snippet"]:
        console.print(f"Snippet: {_e(row['snippet'])}")
    console.print(f"Needs attention: {'yes' if row['requires_attention'] else 'no'}")
    held = review_reasons(ctx.db, email_id)
    if held:
        console.print("[yellow]Status not changed automatically:[/yellow]")
        for reason in held:
            console.print(f"  - {_e(reason)}")
        console.print("  If this email is about one of your applications, confirm it with "
                      "[bold]job-agent emails associate <id> <A-...> --apply-status[/bold].")
    if row["injection_suspected"]:
        console.print("[yellow]Contains instruction-like text (possible prompt injection); not acted on "
                      "automatically.[/yellow]")
    assocs = associations_for(ctx.db, email_id)
    if assocs:
        t = Table(title="Associations", header_style="bold")
        for col in ("Job", "Application", "Employer / title", "Method", "Confidence", "Confirmed", "Status applied"):
            t.add_column(col, overflow="fold")
        for a in assocs:
            t.add_row(a["job_uid"] or "", a["application_uid"] or "",
                      _e(f"{a['employer'] or ''} - {a['title'] or ''}"), a["method"], f"{a['confidence']:.2f}",
                      "yes" if a["confirmed_by_user"] else "no", a["status_applied"] or "")
        console.print(t)
    else:
        console.print("Associations: none (use `job-agent emails associate`)")
    if row["suggested_reply"]:
        console.print("\n[bold]Suggested reply[/bold] (draft only - never sent; copy it into your mail client):")
        console.print(_e(row["suggested_reply"]), highlight=False)


@emails_app.command("associate")
def emails_associate(
    email_id: int = typer.Argument(..., help="Email id."),
    ref: str = typer.Argument(..., help="Job uid (J-...) or application uid (A-...)."),
    apply_status: bool = typer.Option(False, "--apply-status",
                                      help="Also apply the status implied by the email's category."),
) -> None:
    """Confirm which job/application an email belongs to."""
    from job_agent.cli import console, fail, get_ctx
    from job_agent.email.service import associate_email
    from job_agent.errors import JobAgentError

    ctx = get_ctx()
    is_app = ref.strip().upper().startswith("A-")
    try:
        result = associate_email(ctx, email_id, job_ref=None if is_app else ref,
                                 application_ref=ref if is_app else None, apply_status=apply_status)
    except JobAgentError as exc:
        fail(exc)
        return
    console.print(f"[green]Email {result.email_id} linked[/green] to job {result.job_id}"
                  + (f", application {result.application_id}" if result.application_id else "") + ".")
    if result.status_applied:
        console.print(f"Status: {result.previous_status} -> [bold]{result.status_applied}[/bold] "
                      "(evidence: this email)")
    elif apply_status and result.skipped_reason:
        console.print(f"[yellow]Status not changed:[/yellow] {_e(result.skipped_reason)}")


@emails_app.command("handled")
def emails_handled(email_id: int = typer.Argument(..., help="Email id.")) -> None:
    """Mark an email as handled (clears 'needs attention'; e.g. you replied yourself)."""
    from job_agent.cli import console, fail, get_ctx
    from job_agent.email.service import mark_email_handled
    from job_agent.errors import JobAgentError

    ctx = get_ctx()
    try:
        changed = mark_email_handled(ctx, email_id, actor="cli")
    except JobAgentError as exc:
        fail(exc)
        return
    console.print("[green]Marked handled.[/green]" if changed else "It did not need attention.")
