"""CLI: ``apply``, ``approvals ...`` and ``applications ...``.

Business logic lives in ``applications.service``; this module only parses
arguments, asks for confirmation and renders. Text that comes from employer
pages (labels, options, error messages) is escaped before printing so it can
never inject terminal markup.
"""

from __future__ import annotations

from typing import Any, Optional

import typer
from rich.markup import escape
from rich.table import Table

from job_agent.errors import JobAgentError, PermanentError

approvals_app = typer.Typer(help="Review and decide pending approvals (nothing is submitted without one).",
                            invoke_without_command=True)
applications_app = typer.Typer(help="Track applications: list, show, submit, mark submitted, withdraw.",
                               invoke_without_command=True)


#: Columns holding ids you copy into other commands: never wrapped.
_ID_COLUMNS = frozenset({"Approval", "Application", "Job", "#", "Key"})


def _e(value: Any) -> str:
    return escape("" if value is None else str(value))


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _print_approval_view(view: dict[str, Any]) -> None:
    from job_agent.cli import console

    s = view["sections"]
    er = s["employer_role"]
    console.print(f"[bold]Approval {_e(view['approval_uid'])}[/bold]  status: [cyan]{_e(view['status'])}[/cyan]  "
                  f"expires: {_e(view['expires_at'])}")
    console.print(f"[bold]Employer / role:[/bold] {_e(er['employer'])} — {_e(er['role'])}  "
                  f"(job {_e(er['job_uid'])}, application {_e(view['application_uid'])})")
    console.print(f"[bold]Application URL:[/bold] {_e(s['url'])}")
    for label, doc in (("Résumé", s["documents"]["resume"]), ("Cover letter", s["documents"]["cover_letter"])):
        if doc:
            console.print(f"[bold]{label}:[/bold] {_e(doc['file'])} (sha256 {_e(doc['sha256'][:16])}…, "
                          f"document status: {_e(doc['status'])})")
        else:
            console.print(f"[bold]{label}:[/bold] none")

    def table(title: str, rows: list[dict[str, Any]], cols: list[tuple[str, str]], style: str = "bold") -> None:
        if not rows:
            return
        t = Table(title=title, header_style=style, show_lines=False)
        t.add_column("#")
        for header, _ in cols:
            t.add_column(header, overflow="fold")
        for r in rows:
            t.add_row(_e(r.get("ref") or ""), *[_e(r.get(k)) for _, k in cols])
        console.print(t)

    table("Answers that will be entered", s["answers"],
          [("Field", "label"), ("Key", "field_key"), ("Answer", "answer"), ("Source", "source")])
    table("LEGAL DECLARATIONS — review carefully", s["legal_declarations"],
          [("Question", "label"), ("Key", "field_key"), ("Answer", "answer"), ("Source", "source"),
           ("Verified", "verified")], "bold red")
    table("Required fields still unanswered", s["unanswered"],
          [("Field", "label"), ("Key", "field_key"), ("Why", "reason")], "bold yellow")
    table("Needs your answer / uncertain", s["uncertain"],
          [("Field", "label"), ("Key", "field_key"), ("Why", "reason"), ("Suggestion (not entered)",
                                                                           "suggested_answer")], "bold yellow")
    table("Agreements / certifications (never checked for you)", s["agreements"],
          [("Statement", "label"), ("Key", "field_key"), ("Your answer", "answer")], "bold magenta")
    table("Left blank by you", s["left_blank_by_you"], [("Field", "label"), ("Key", "field_key")])
    if s["validation_errors"]:
        console.print("[bold red]Validation errors shown by the form:[/bold red]")
        for err in s["validation_errors"]:
            console.print(f"  • {_e(err)}")
    if s.get("blocked_data_requests"):
        console.print("[bold red]The page tried to send what was typed or uploaded before submission "
                      "(blocked):[/bold red]")
        for url in s["blocked_data_requests"]:
            console.print(f"  • {_e(url)}")
    if s["work_authorization_reference"]:
        ref = ", ".join(f"{k}={v}" for k, v in s["work_authorization_reference"].items())
        console.print(f"[dim]Work-authorization reference from your profile (display only, never used to answer): "
                      f"{_e(ref)}[/dim]")
    if s["screenshot"]:
        console.print(f"[dim]Screenshot of the prepared form: data/{_e(s['screenshot'])}[/dim]")
    if view["browser_submission_blockers"]:
        console.print("[yellow]Browser submission would be refused until:[/yellow]")
        for b in view["browser_submission_blockers"]:
            console.print(f"  • {_e(b)}")


def _is_submit_approval(db: Any, approval_uid: str) -> bool:
    from job_agent.applications.repository import NotFound
    from job_agent.database import repo

    row = repo.get_approval(db, approval_uid)
    if row is None:
        raise NotFound(f"approval {approval_uid} not found")
    return row["request_type"] == "submit_application" and row["application_id"] is not None


def _print_generic_approval(db: Any, approval_uid: str) -> dict[str, Any]:
    """Approvals of other types (e.g. follow-ups): summary + stored payload."""
    import json

    from job_agent.cli import console
    from job_agent.database import repo

    row = repo.get_approval(db, approval_uid)
    assert row is not None
    console.print(f"[bold]Approval {_e(row['approval_uid'])}[/bold] ({_e(row['request_type'])})  status: "
                  f"[cyan]{_e(row['status'])}[/cyan]  expires: {_e(row['expires_at'])}")
    console.print(f"[bold]Summary:[/bold] {_e(row['summary'])}")
    console.print(_e(json.dumps(repo.approval_payload(row), indent=2, ensure_ascii=False)))
    return {"status": row["status"], "payload_hash": row["payload_hash"]}


def _print_answers(answers: list[dict[str, Any]]) -> None:
    from job_agent.cli import console

    if not answers:
        return
    t = Table(title="Prepared answers", header_style="bold")
    for col in ("#", "Key", "Field", "Answer", "Source", "Flags"):
        t.add_column(col, overflow="fold")
    for i, a in enumerate(answers, start=1):
        flags = []
        if a["is_legal_declaration"]:
            flags.append("LEGAL")
        if a["requires_review"]:
            flags.append("review")
        if a["required"]:
            flags.append("required")
        if a["confidence"] != "verified" and a["answer"]:
            flags.append("suggestion only")
        t.add_row(f"#{i}", _e(a["field_key"]), _e(a["field_label"]), _e(a["answer"]), _e(a["answer_source"]),
                  ", ".join(flags))
    console.print(t)


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #
def apply_cmd(
    job_uid: str = typer.Argument(..., help="Job id (J-...) whose documents are ready."),
    headed: bool = typer.Option(False, "--headed", help="Show the browser window."),
    confirm_not_submitted: bool = typer.Option(
        False, "--confirm-not-submitted",
        help="After a browser submission with an UNKNOWN outcome: you checked your email / the employer's portal "
             "and it was NOT submitted. Recorded on the application; required before it is prepared again."),
) -> None:
    """Prepare the application form (fills verified answers only, never submits) and request approval."""
    from job_agent.applications import repository as arepo
    from job_agent.applications.service import prepare_application
    from job_agent.cli import console, fail, get_ctx

    ctx = get_ctx()
    try:
        result = prepare_application(ctx, job_uid, headless=False if headed else None,
                                     confirm_not_submitted=confirm_not_submitted)
    except JobAgentError as exc:
        fail(exc)
        return
    style = "green" if result.status.value == "awaiting_approval" else "yellow"
    console.print(f"[{style}]{_e(result.application_uid)}: {_e(result.status.value)}[/]")
    if result.approval_uid:
        console.print(f"Approval request: [bold]{_e(result.approval_uid)}[/bold] — {result.fields_found} field(s), "
                      f"{result.filled} filled, {result.legal_declarations} legal declaration(s), "
                      f"{result.requires_review} to review, {result.unanswered_required} required unanswered")
        app = arepo.resolve_application(ctx.db, result.application_uid)
        _print_answers([dict(r) for r in arepo.get_answers(ctx.db, int(app["id"]))])
    if result.validation_errors:
        console.print("[red]The form shows validation errors:[/red] " + _e("; ".join(result.validation_errors)))
    console.print(f"[bold]Next:[/bold] {_e(result.message)}")
    if result.status.value == "needs_manual_action":
        raise typer.Exit(1)  # scripts can tell that automation stopped and you must act
    console.print("[dim]Nothing was submitted.[/dim]")


# --------------------------------------------------------------------------- #
# approvals
# --------------------------------------------------------------------------- #
@approvals_app.callback()
def approvals_root(ctx: typer.Context) -> None:
    """With no subcommand: list pending approvals."""
    if ctx.invoked_subcommand is not None:
        return
    from job_agent.applications.service import pending_approvals
    from job_agent.cli import console, get_ctx

    rows = pending_approvals(get_ctx().db)
    if not rows:
        console.print("No pending approvals.")
        return
    t = Table(title="Pending approvals", header_style="bold")
    for col in ("Approval", "Type", "Application", "Employer / role", "Requested (UTC)", "Expires (UTC)", "Summary"):
        t.add_column(col, overflow="fold", no_wrap=col in _ID_COLUMNS)
    for r in rows:
        who = f"{r.get('employer') or ''} — {r.get('title') or ''}" if r.get("employer") else ""
        t.add_row(_e(r["approval_uid"]), _e(r["request_type"]), _e(r.get("application_uid")), _e(who),
                  _e(r["requested_at"]), _e(r["expires_at"]), _e(r["summary"]))
    console.print(t)
    console.print("Review one with [bold]job-agent approvals show <approval-uid>[/bold].")


@approvals_app.command("show")
def approvals_show(approval_uid: str = typer.Argument(..., help="Approval id (AP-...).")) -> None:
    """Show everything you would approve: documents, answers, legal declarations, open items."""
    from job_agent.applications.service import approval_view
    from job_agent.cli import fail, get_ctx

    db = get_ctx().db
    try:
        if not _is_submit_approval(db, approval_uid):
            _print_generic_approval(db, approval_uid)
            return
        view = approval_view(db, approval_uid)
    except JobAgentError as exc:
        fail(exc)
        return
    _print_approval_view(view)


@approvals_app.command("approve")
def approvals_approve(
    approval_uid: str = typer.Argument(..., help="Approval id (AP-...)."),
    note: Optional[str] = typer.Option(None, "--note", help="Optional note stored with your decision."),
) -> None:
    """Approve a submission after reviewing the full request (asks for confirmation)."""
    from job_agent.applications.service import approval_view, decide
    from job_agent.cli import console, fail, get_ctx

    ctx = get_ctx()
    try:
        if not _is_submit_approval(ctx.db, approval_uid):
            view = _print_generic_approval(ctx.db, approval_uid)
        else:
            view = approval_view(ctx.db, approval_uid)
            _print_approval_view(view)
    except JobAgentError as exc:
        fail(exc)
        return
    if view["status"] != "pending":
        console.print(f"[red]This approval is {_e(view['status'])}; only pending approvals can be decided.[/red]")
        raise typer.Exit(1)
    legal = len(view["sections"]["legal_declarations"]) if "sections" in view else 0
    prompt = ("Approve this application for submission"
              + (f", including {legal} legal declaration(s) shown above" if legal else "") + "?")
    if not typer.confirm(prompt, default=False):
        console.print("Not approved. Nothing changed.")
        raise typer.Exit(1)
    try:
        result = decide(ctx, approval_uid, approve=True, actor="cli", note=note,
                        expected_payload_hash=view["payload_hash"])
    except JobAgentError as exc:
        fail(exc)
        return
    console.print(f"[green]Approved {_e(approval_uid)}.[/green]")
    console.print(f"[bold]Next:[/bold] {_e(result.next_action)}")


@approvals_app.command("reject")
def approvals_reject(
    approval_uid: str = typer.Argument(..., help="Approval id (AP-...)."),
    note: str = typer.Option(..., "--note", help="Why you reject it (stored on the application)."),
) -> None:
    """Reject a submission request, or withdraw an approval you gave that was never used."""
    from job_agent.applications.service import decide
    from job_agent.cli import console, fail, get_ctx

    try:
        result = decide(get_ctx(), approval_uid, approve=False, actor="cli", note=note)
    except JobAgentError as exc:
        fail(exc)
        return
    console.print(f"[yellow]Rejected {_e(approval_uid)}.[/yellow]"
                  + (f" Application is now {_e(result.status.value)}." if result.status else ""))
    console.print(f"[bold]Next:[/bold] {_e(result.next_action)}")


@approvals_app.command("answer")
def approvals_answer(
    application_uid: str = typer.Argument(..., help="Application id (A-...)."),
    field_key: str = typer.Argument(..., help='Field key, or "#n" as numbered in `approvals show`.'),
    value: str = typer.Argument(..., help='Your answer ("" = deliberately leave blank; yes/no for checkboxes).'),
) -> None:
    """Answer a field that needs your review (creates a new approval request to review)."""
    from job_agent.applications.service import set_answer
    from job_agent.cli import console, fail, get_ctx

    try:
        new_uid = set_answer(get_ctx(), application_uid, field_key, value)
    except JobAgentError as exc:
        fail(exc)
        return
    console.print(f"[green]Saved your answer for {_e(field_key)}.[/green]")
    if new_uid:
        console.print(f"New approval request: [bold]{_e(new_uid)}[/bold] (the previous one was superseded). "
                      f"Review it with `job-agent approvals show {_e(new_uid)}`.")


# --------------------------------------------------------------------------- #
# applications
# --------------------------------------------------------------------------- #
def _print_list(status: str | None) -> None:
    from job_agent.applications.service import list_applications
    from job_agent.cli import console, get_ctx

    rows = list_applications(get_ctx().db, status)
    if not rows:
        console.print("No applications" + (f" with status {_e(status)}." if status else "."))
        return
    t = Table(title="Applications", header_style="bold")
    for col in ("Application", "Job", "Employer", "Role", "Status", "Next action", "Last activity (UTC)"):
        t.add_column(col, overflow="fold", no_wrap=col in _ID_COLUMNS)
    for r in rows:
        t.add_row(_e(r["application_uid"]), _e(r["job_uid"]), _e(r["employer"]), _e(r["title"]), _e(r["status"]),
                  _e(r["next_action"]), _e(r["last_activity_at"]))
    console.print(t)


@applications_app.callback()
def applications_root(ctx: typer.Context) -> None:
    """With no subcommand: list applications."""
    if ctx.invoked_subcommand is None:
        _print_list(None)


@applications_app.command("list")
def applications_list(status: Optional[str] = typer.Option(None, "--status", help="Filter by status.")) -> None:
    """List applications (optionally by status)."""
    _print_list(status)


@applications_app.command("show")
def applications_show(application_uid: str = typer.Argument(..., help="Application id (A-...).")) -> None:
    """Show one application: status, timeline, answers, documents, approvals."""
    from job_agent.applications.service import application_detail
    from job_agent.cli import console, fail, get_ctx

    try:
        d = application_detail(get_ctx().db, application_uid)
    except JobAgentError as exc:
        fail(exc)
        return
    a, j = d["application"], d["job"]
    console.print(f"[bold]{_e(a['application_uid'])}[/bold] — {_e(j['employer'])} — {_e(j['title'])} "
                  f"(job {_e(j['job_uid'])})")
    console.print(f"[bold]Status:[/bold] {_e(a['status'])} since {_e(a['status_updated_at'])}; "
                  f"submission attempts: {a['submission_attempts']}; confirmation: {_e(a['confirmation_status'])}")
    console.print(f"[bold]Application URL:[/bold] {_e(a['application_url'] or j['application_url'] or j['job_url'])}")
    if a["next_action"]:
        console.print(f"[bold]Next:[/bold] {_e(a['next_action'])}")
    if d["submission_abandoned"]:
        console.print("[red]The process that clicked submit stopped before recording the outcome: it is UNKNOWN "
                      "whether the application was sent. Check your email/portal, then run `job-agent applications "
                      "mark-submitted`, `set-status ... closed` or `withdraw` (the unknown outcome is recorded "
                      "first).[/red]")
    elif d["unknown_outcome"]:
        console.print("[red]A browser submission had an UNKNOWN outcome. Check your email/portal before doing "
                      "anything else; the agent will not retry it.[/red]")
    for doc_type, variants in d["documents"].items():
        if variants:
            files = ", ".join(f"{v['file']} ({v['status']})" for v in variants)
            console.print(f"[bold]{doc_type.replace('_', ' ').title()}:[/bold] {_e(files)}")
    t = Table(title="Timeline", header_style="bold")
    for col in ("When (UTC)", "Event", "From → To", "Actor", "Details"):
        t.add_column(col, overflow="fold")
    for ev in d["events"]:
        move = f"{ev['from_status'] or ''} → {ev['to_status']}" if ev["to_status"] else ""
        details = {k: v for k, v in (ev["details"] or {}).items() if k not in {"fields", "eligibility_reference"}}
        t.add_row(_e(ev["created_at"]), _e(ev["event_type"]), _e(move), _e(ev["actor"]), _e(details or ""))
    console.print(t)
    _print_answers(d["answers"])
    if d["approvals"]:
        t = Table(title="Approvals", header_style="bold")
        for col in ("Approval", "Status", "Requested", "Decided by", "Note"):
            t.add_column(col, overflow="fold", no_wrap=col in _ID_COLUMNS)
        for ap in d["approvals"]:
            t.add_row(_e(ap["approval_uid"]), _e(ap["status"]), _e(ap["requested_at"]), _e(ap["decided_by"]),
                      _e(ap["decision_note"]))
        console.print(t)


@applications_app.command("mark-submitted")
def applications_mark_submitted(
    application_uid: str = typer.Argument(..., help="Application id (A-...)."),
    note: Optional[str] = typer.Option(None, "--note", help="e.g. where/how you submitted it."),
) -> None:
    """Record that YOU submitted the application (manually, or an unknown outcome you verified)."""
    from job_agent.applications.service import mark_submitted
    from job_agent.cli import console, fail, get_ctx

    try:
        previous = mark_submitted(get_ctx(), application_uid, note=note)
    except JobAgentError as exc:
        fail(exc)
        return
    console.print(f"[green]{_e(application_uid)} marked submitted[/green] (was {_e(previous.value)}).")


@applications_app.command("set-status")
def applications_set_status(
    application_uid: str = typer.Argument(..., help="Application id (A-...)."),
    status: str = typer.Argument(..., help="submitted | confirmation_received | assessment_received | "
                                           "interview_requested | interview_scheduled | rejected | "
                                           "offer_received | closed"),
    note: Optional[str] = typer.Option(None, "--note", help="Why (e.g. 'rejection email was for another role')."),
) -> None:
    """Record or correct an outcome yourself (e.g. undo a status an email set wrongly)."""
    from job_agent.applications.service import set_status
    from job_agent.cli import console, fail, get_ctx

    try:
        previous = set_status(get_ctx(), application_uid, status.strip().lower(), note=note)
    except (JobAgentError, ValueError) as exc:
        if isinstance(exc, ValueError):
            exc = PermanentError(f"unknown status {status!r}")
        fail(exc)
        return
    console.print(f"[green]{_e(application_uid)} set to {_e(status)}[/green] (was {_e(previous.value)}).")


@applications_app.command("submit")
def applications_submit(approval_uid: str = typer.Argument(..., help="Approved approval id (AP-...).")) -> None:
    """Submit an APPROVED application with the browser (only if browser_submission_enabled)."""
    from job_agent.applications.service import submit_approved
    from job_agent.cli import console, err_console, fail, get_ctx

    ctx = get_ctx()
    if not ctx.config.applications.browser_submission_enabled:
        err_console.print("[yellow]Browser submission is disabled[/yellow] (applications.browser_submission_enabled:"
                          " false). Submit the application yourself using the approved answers "
                          f"(`job-agent approvals show {_e(approval_uid)}`), then run "
                          "`job-agent applications mark-submitted <application-uid>`. Nothing was submitted.")
        raise typer.Exit(1)
    if not typer.confirm(f"Submit the application approved in {approval_uid} now? This cannot be undone.",
                         default=False):
        console.print("Not submitted.")
        raise typer.Exit(1)
    try:
        result = submit_approved(ctx, approval_uid)
    except JobAgentError as exc:
        fail(exc)
        return
    style = {"submitted": "green", "failed": "red"}.get(result.outcome, "yellow")
    console.print(f"[{style}]{_e(result.application_uid)}: {_e(result.outcome)} ({_e(result.status.value)})[/]")
    if result.evidence:
        console.print(f"Evidence: {_e(result.evidence.get('matched_text'))} at {_e(result.evidence.get('url'))}")
    console.print(f"[bold]Next:[/bold] {_e(result.message)}")
    if result.outcome in {"failed", "unknown", "not_submitted"}:
        raise typer.Exit(1)


@applications_app.command("withdraw")
def applications_withdraw(
    application_uid: str = typer.Argument(..., help="Application id (A-...)."),
    note: Optional[str] = typer.Option(None, "--note", help="Why (stored on the application)."),
) -> None:
    """Mark an application as withdrawn by you."""
    from job_agent.applications.service import withdraw
    from job_agent.cli import console, fail, get_ctx

    if not typer.confirm(f"Mark {application_uid} as withdrawn?", default=False):
        raise typer.Exit(1)
    try:
        previous = withdraw(get_ctx(), application_uid, note=note)
    except JobAgentError as exc:
        fail(exc)
        return
    console.print(f"[yellow]{_e(application_uid)} withdrawn[/yellow] (was {_e(previous.value)}).")


@applications_app.command("note")
def applications_note(
    application_uid: str = typer.Argument(..., help="Application id (A-...)."),
    text: str = typer.Argument(..., help="Note text."),
) -> None:
    """Add a note to an application's timeline."""
    from job_agent.applications.service import add_note
    from job_agent.cli import console, fail, get_ctx

    try:
        add_note(get_ctx(), application_uid, text)
    except JobAgentError as exc:
        fail(exc)
        return
    console.print("[green]Note added.[/green]")


def register(app: typer.Typer) -> None:
    app.command("apply")(apply_cmd)
    app.add_typer(approvals_app, name="approvals")
    app.add_typer(applications_app, name="applications")
