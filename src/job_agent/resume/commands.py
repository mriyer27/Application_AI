"""CLI: ``prepare <job-uid>`` and ``documents list|approve|reject``.

Every value derived from a listing, the LLM or stored data is passed through
``rich.markup.escape`` so text such as ``[red]`` or ``[/]`` in a job title or
an LLM rewording is printed literally (and cannot break the output).
"""

from __future__ import annotations

from typing import Optional

import typer

from job_agent.errors import JobAgentError

_SEVERITY_STYLE = {"error": "red", "review": "yellow", "info": "cyan"}
_STATUS_STYLE = {"draft": "white", "needs_review": "yellow", "approved": "green", "rejected": "red"}


def prepare_cmd(
    job_uid: str = typer.Argument(..., help="Job id (job_uid, e.g. J-1a2b3c4d5e, or numeric id)."),
    no_llm: bool = typer.Option(False, "--no-llm", help="Use verbatim verified facts only (no local-LLM rewording)."),
    no_cover_letter: bool = typer.Option(False, "--no-cover-letter", help="Generate the résumé only."),
) -> None:
    """Generate a tailored, fact-checked résumé (and cover letter) for one job."""
    from rich.markup import escape
    from rich.table import Table

    from job_agent.cli import console, fail, get_ctx
    from job_agent.resume.service import prepare_documents

    ctx = get_ctx()
    try:
        result = prepare_documents(ctx, job_uid, use_llm=False if no_llm else None,
                                   include_cover_letter=False if no_cover_letter else None, actor="cli")
    except JobAgentError as exc:
        fail(exc)
        return

    files = Table(title=escape(f"Documents for {result.job_uid} "
                               f"(application status: {result.application_status.value})"), header_style="bold")
    for col in ("ID", "Document", "Version", "Format", "Status"):
        files.add_column(col, no_wrap=True)
    files.add_column("File (under documents/)", overflow="fold")
    for d in result.resume + result.cover_letter:
        files.add_row(str(d.id), escape(d.doc_type.value), str(d.version), escape(d.format),
                      _styled(d.status.value, _STATUS_STYLE), escape(d.relative_path))
    console.print(files)

    bullets = Table(title="Résumé bullets and their verified source facts", header_style="bold")
    bullets.add_column("Section", overflow="fold")
    bullets.add_column("Bullet", overflow="fold", ratio=3)
    bullets.add_column("Fact ID(s)", no_wrap=True)
    bullets.add_column("Generator", overflow="fold")
    for c in result.claims:
        if c.section.startswith("cover_letter"):
            continue
        bullets.add_row(escape(c.section), escape(c.text), escape(", ".join(c.fact_ids)), escape(c.generator))
    console.print(bullets)

    if result.issues:
        issues = Table(title="Validation issues", header_style="bold")
        for col in ("Document", "Severity", "Code"):
            issues.add_column(col, no_wrap=True)
        issues.add_column("Message", overflow="fold")
        for i in result.issues:
            issues.add_row(escape(i.document or ""), _styled(i.severity, _SEVERITY_STYLE), escape(i.code),
                           escape(i.message))
        console.print(issues)
    console.print(f"[bold]Overall:[/bold] {escape(result.status.value)}")
    console.print(f"[bold]Next step:[/bold] {escape(result.next_step)}")


def documents_list(job_uid: str = typer.Argument(..., help="Job id (job_uid or numeric id).")) -> None:
    """List generated document versions for a job."""
    from rich.markup import escape
    from rich.table import Table

    from job_agent.cli import console, fail, get_ctx
    from job_agent.resume.service import list_documents, load_job

    ctx = get_ctx()
    try:
        job = load_job(ctx.db, job_uid)
    except JobAgentError as exc:
        fail(exc)
        return
    docs = list_documents(ctx.db, int(job["id"]))
    if not docs:
        uid = escape(str(job["job_uid"]))
        console.print(f"No documents yet for {uid}. Run `job-agent prepare {uid}`.")
        return
    table = Table(title=escape(f"Documents for {job['job_uid']} - {job['employer']} / {job['title']}"),
                  header_style="bold")
    for col in ("ID", "Document", "Version", "Format", "Status"):
        table.add_column(col, no_wrap=True)
    for col in ("Generator", "Issues", "Created (UTC)", "File"):
        table.add_column(col, overflow="fold")
    for d in docs:
        n_issues = sum(1 for i in d.validation_issues if i.get("severity") in {"error", "review"})
        table.add_row(str(d.id), escape(d.doc_type.value), str(d.version), escape(d.format),
                      _styled(d.status.value, _STATUS_STYLE), escape(d.generator), str(n_issues),
                      escape(d.created_at), escape(d.relative_path))
    console.print(table)


def _styled(value: str, styles: dict[str, str]) -> str:
    from rich.markup import escape

    return f"[{styles.get(value, 'white')}]{escape(value)}[/]"


def _review(document_id: int, status: str, note: str | None) -> None:
    from rich.markup import escape

    from job_agent.cli import console, fail, get_ctx
    from job_agent.resume.service import set_document_status

    ctx = get_ctx()
    try:
        records = set_document_status(ctx, document_id, status, actor="cli", note=note)
    except JobAgentError as exc:
        fail(exc)
        return
    for r in records:
        console.print(escape(f"{r.doc_type.value} v{r.version} ({r.format}, id {r.id}): ")
                      + f"[bold]{escape(r.status.value)}[/bold]")


def documents_approve(
    document_id: int = typer.Argument(..., help="Document id (see `documents list`)."),
    note: Optional[str] = typer.Option(None, "--note", help="Optional review note."),
) -> None:
    """Mark a document version as reviewed and approved by you (all its formats)."""
    _review(document_id, "approved", note)


def documents_reject(
    document_id: int = typer.Argument(..., help="Document id (see `documents list`)."),
    note: Optional[str] = typer.Option(None, "--note", help="Why it was rejected."),
) -> None:
    """Reject a document version (all its formats); regenerate with `prepare`."""
    _review(document_id, "rejected", note)


def register(app: typer.Typer) -> None:
    app.command("prepare")(prepare_cmd)
    documents_app = typer.Typer(help="Generated résumés / cover letters: list, approve, reject.",
                                no_args_is_help=True)
    documents_app.command("list")(documents_list)
    documents_app.command("approve")(documents_approve)
    documents_app.command("reject")(documents_reject)
    app.add_typer(documents_app, name="documents")
