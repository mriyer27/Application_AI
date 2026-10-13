"""CLI: ``score``, ``jobs`` and ``job show|mark|note``.

Business logic lives in ``scoring.service`` / ``scoring.repository``; these
commands only parse options and render tables. Listing text is untrusted, so
every value taken from a job row is escaped before Rich renders it.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, List, Optional

import typer
from rich.markup import escape

from job_agent.models import MatchCategory

_SEVERITY_STYLE = {"blocker": "red", "review": "yellow", "info": "cyan"}
_CATEGORY_STYLE = {"strong_match": "green", "possible_match": "cyan", "weak_match": "white",
                   "needs_review": "yellow"}


class Relevance(str, Enum):
    relevant = "relevant"
    irrelevant = "irrelevant"
    clear = "clear"


def _e(value: Any) -> str:
    return escape("" if value is None else str(value))


def _flags_cell(flags: list[dict[str, Any]]) -> str:
    seen: list[str] = []
    for f in flags:
        code = f.get("code", "")
        style = _SEVERITY_STYLE.get(f.get("severity", ""), "white")
        cell = f"[{style}]{_e(code)}[/]"
        if cell not in seen:
            seen.append(cell)
    return ", ".join(seen)


# --------------------------------------------------------------------------- #
# score
# --------------------------------------------------------------------------- #
def score_command(
    rescore: bool = typer.Option(False, "--rescore", help="Re-score every active job, even unchanged ones."),
    limit: Optional[int] = typer.Option(None, "--limit", min=1, help="Score at most N jobs in this run."),
    job: Optional[List[str]] = typer.Option(
        None, "--job", help="Score only this job (id or job uid); repeatable. A filtered-out job named here is "
                            "re-evaluated."),
    no_llm: bool = typer.Option(False, "--no-llm", help="Rules only; do not call the local LLM."),
) -> None:
    """Score and rank new or changed jobs (explainable rules; optional local-AI enrichment)."""
    from rich.table import Table

    from job_agent.cli import console, err_console, fail, get_ctx
    from job_agent.errors import JobAgentError
    from job_agent.scoring import repository as score_repo
    from job_agent.scoring.service import default_analyzer, score_jobs

    ctx = get_ctx()
    try:
        ids: list[int] | None = None
        if job:
            ids = []
            for ref in job:
                row = score_repo.find_job(ctx.db, ref)
                if row is None:
                    err_console.print(f"[red]Job {_e(ref)} not found.[/red] Use `job-agent jobs` to list job ids.")
                    raise typer.Exit(1)
                ids.append(int(row["id"]))
        analyzer = None if no_llm else default_analyzer(ctx)
        summary = score_jobs(ctx, rescore=rescore or bool(ids), limit=limit, job_ids=ids, analyzer=analyzer)
    except JobAgentError as exc:
        fail(exc)
        return

    table = Table(title="Scoring run", header_style="bold", show_header=False)
    table.add_column("What")
    table.add_column("Count", justify="right")
    table.add_row("Scored", str(summary.scored))
    table.add_row("Unchanged (kept current score)", str(summary.skipped_unchanged))
    if summary.deferred:
        table.add_row("Deferred by --limit", str(summary.deferred))
    table.add_row("Shortlisted", str(summary.shortlisted))
    for cat, n in summary.by_category.items():
        table.add_row(f"  {cat}", str(n))
    table.add_row("Scored with local AI analysis", str(summary.llm_scored))
    if summary.llm_failed:
        table.add_row("Local AI analysis failed (rules score kept)", str(summary.llm_failed))
    table.add_row("Errors", str(len(summary.errors)))
    console.print(table)
    if analyzer is None:
        console.print("Local AI analysis: off (rules-only scoring)" + (" [--no-llm]" if no_llm else ""))
    for err in summary.errors:
        console.print(f"[yellow]•[/yellow] {_e(err)}")
    console.print("Next: [bold]job-agent jobs[/bold] to see the ranking, [bold]job-agent job show <job-uid>[/bold] "
                  "for the full explanation.")


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #
def jobs_command(
    top: int = typer.Option(20, "--top", "-n", min=1, max=1000, help="How many jobs to show."),
    category: Optional[MatchCategory] = typer.Option(None, "--category", case_sensitive=False,
                                                     help="Only this match category."),
    include_filtered: bool = typer.Option(False, "--include-filtered",
                                          help="Also show filtered-out, duplicate and irrelevant-marked jobs."),
    include_closed: bool = typer.Option(False, "--include-closed", help="Also show closed listings."),
) -> None:
    """List the best-scoring jobs."""
    from rich.table import Table

    from job_agent.cli import console, fail, get_ctx
    from job_agent.errors import JobAgentError
    from job_agent.scoring.engine import salary_display
    from job_agent.scoring.repository import top_jobs

    ctx = get_ctx()
    try:
        rows = top_jobs(ctx.db, limit=top, category=category.value if category else None,
                        include_filtered=include_filtered, include_closed=include_closed)
    except JobAgentError as exc:
        fail(exc)
        return
    if not rows:
        console.print("No scored jobs match. Run [bold]job-agent discover[/bold] and then [bold]job-agent score[/bold].")
        return
    table = Table(header_style="bold", title=f"Top {len(rows)} jobs", caption="* = shortlisted")
    table.add_column("Job UID", no_wrap=True)
    table.add_column("Score", justify="right", no_wrap=True)
    table.add_column("Category", no_wrap=True)
    table.add_column("Employer", overflow="fold")
    table.add_column("Title", overflow="fold")
    table.add_column("Location", overflow="fold")
    table.add_column("Salary", overflow="fold")
    table.add_column("Flags", overflow="fold")
    for r in rows:
        cat = str(r["category"])
        style = _CATEGORY_STYLE.get(cat, "white")
        table.add_row(
            _e(r["job_uid"]),
            f"{float(r['overall_score']):.1f}",
            f"[{style}]{_e(cat)}[/]{' *' if r['shortlisted'] else ''}",
            _e(r["employer"]),
            _e(r["title"]),
            _e(r.get("location") or ""),
            _e(salary_display(r)),
            _flags_cell(r.get("eligibility_flags") or []),
        )
    console.print(table)


# --------------------------------------------------------------------------- #
# job show / mark / note
# --------------------------------------------------------------------------- #
def job_show(ref: str = typer.Argument(..., help="Job uid (e.g. J-1a2b3c4d5e) or numeric id.")) -> None:
    """Show one job with its full score explanation."""
    from rich.table import Table

    from job_agent.cli import console, err_console, get_ctx
    from job_agent.scoring.engine import salary_display
    from job_agent.scoring.repository import job_detail

    ctx = get_ctx()
    detail = job_detail(ctx.db, ref)
    if detail is None:
        err_console.print(f"[red]Job {_e(ref)} not found.[/red] Use `job-agent jobs` to list job ids.")
        raise typer.Exit(1)
    job, score, app = detail["job"], detail["score"], detail["application"]

    console.print(f"[bold]{_e(job['title'])}[/bold] at [bold]{_e(job['employer'])}[/bold]  ({_e(job['job_uid'])})")
    facts = [
        ("Location", job.get("location") or "not listed"),
        ("Arrangement", job.get("work_arrangement")),
        ("Employment type", job.get("employment_type")),
        ("Experience level", job.get("experience_level") or "not stated"),
        ("Salary", salary_display(job) or "not listed"),
        ("Posted", job.get("posted_at") or "unknown"),
        ("Pipeline state", job.get("pipeline_state")),
        ("Listing status", job.get("listing_status")),
        ("Source", f"{job.get('source_name')}:{job.get('source_identifier')}"),
        ("Job URL", job.get("job_url") or "-"),
        ("Application URL", job.get("application_url") or "-"),
    ]
    if job.get("filter_reason"):
        facts.append(("Filter reason", job["filter_reason"]))
    if job.get("user_relevance"):
        facts.append(("Your mark", job["user_relevance"]))
    if job.get("user_notes"):
        facts.append(("Your notes", job["user_notes"]))
    if app:
        facts.append(("Application", f"{app.get('application_uid')} ({app.get('status')})"))
    for label, value in facts:
        console.print(f"  [bold]{label}:[/bold] {_e(value)}")

    if score is None:
        console.print("\n[yellow]Not scored yet.[/yellow] Run [bold]job-agent score[/bold].")
        return

    cat = str(score["category"])
    console.print(
        f"\n[bold]Score {float(score['overall_score']):.1f}/100[/bold]  "
        f"[{_CATEGORY_STYLE.get(cat, 'white')}]{_e(cat)}[/]"
        f"{'  [green]shortlisted[/green]' if score['shortlisted'] else ''}  "
        f"(version {_e(score['scoring_version'])}"
        f"{', local AI: ' + _e(score['llm_model']) if score.get('used_llm') else ', rules only'}; "
        f"scored {_e(score['scored_at'])})"
    )
    console.print(f"[bold]Recommended next action:[/bold] {_e(score['recommended_next_action'])}")

    factors = Table(title="Factors", header_style="bold", show_lines=True)
    factors.add_column("Factor", no_wrap=True)
    factors.add_column("Weight", justify="right")
    factors.add_column("Score", justify="right")
    factors.add_column("Points", justify="right")
    factors.add_column("Explanation", overflow="fold")
    for f in score["factors"]:
        weight, value = float(f["weight"]), float(f["score"])
        factors.add_row(_e(f["name"]), f"{weight:.1f}", f"{value:.2f}", f"{weight * value:.1f}", _e(f["explanation"]))
    console.print(factors)

    def bullets(title: str, items: list[str], style: str = "white") -> None:
        if items:
            console.print(f"[bold]{title}[/bold]")
            for item in items:
                console.print(f"  [{style}]•[/] {_e(item)}")

    bullets("Matching qualifications", score["matching_qualifications"], "green")
    bullets("Missing qualifications", score["missing_qualifications"], "yellow")
    bullets("Potential disqualifiers", score["potential_disqualifiers"], "red")

    flags = score["eligibility_flags"]
    if flags:
        ft = Table(title="Eligibility flags (verbatim excerpts; not legal advice)", header_style="bold",
                   show_lines=True)
        ft.add_column("Severity", no_wrap=True)
        ft.add_column("Code", no_wrap=True)
        ft.add_column("Excerpt", overflow="fold")
        ft.add_column("Note", overflow="fold")
        for f in flags:
            sev = str(f.get("severity"))
            ft.add_row(f"[{_SEVERITY_STYLE.get(sev, 'white')}]{_e(sev)}[/]", _e(f.get("code")),
                       f"\"{_e(f.get('excerpt'))}\"", _e(f.get("message")))
        console.print(ft)
    else:
        console.print("[bold]Eligibility flags:[/bold] none found in the listing text (still read the listing).")

    evidence = score["evidence"]
    if evidence:
        et = Table(title="Evidence from your verified profile", header_style="bold")
        et.add_column("Requirement", overflow="fold")
        et.add_column("Fact id", no_wrap=True)
        et.add_column("Fact", overflow="fold")
        for ev in evidence:
            et.add_row(_e(ev.get("requirement")), _e(ev.get("fact_id")), _e(ev.get("fact_text")))
        console.print(et)

    bullets("Reasons to apply", score["reasons_to_apply"], "green")
    bullets("Reasons not to apply", score["reasons_not_to_apply"], "yellow")


def job_mark(
    ref: str = typer.Argument(..., help="Job uid or numeric id."),
    relevance: Relevance = typer.Argument(..., help="relevant | irrelevant | clear"),
) -> None:
    """Mark a job as relevant or irrelevant to you (irrelevant jobs are hidden from `jobs` by default)."""
    from job_agent.cli import console, fail, get_ctx
    from job_agent.errors import JobAgentError
    from job_agent.scoring.repository import set_user_relevance

    ctx = get_ctx()
    value = None if relevance == Relevance.clear else relevance.value
    try:
        job = set_user_relevance(ctx.db, ref, value, actor="cli")
    except JobAgentError as exc:
        fail(exc)
        return
    console.print(f"[green]{_e(job['job_uid'])}[/green] marked {_e(value or 'unmarked')}.")


def job_note(
    ref: str = typer.Argument(..., help="Job uid or numeric id."),
    text: str = typer.Argument(..., help="Note text (replaces the existing note unless --append)."),
    append: bool = typer.Option(False, "--append", help="Append to the existing note instead of replacing it."),
) -> None:
    """Attach a private note to a job."""
    from job_agent.cli import console, fail, get_ctx
    from job_agent.errors import JobAgentError
    from job_agent.scoring.repository import set_user_note

    ctx = get_ctx()
    try:
        job = set_user_note(ctx.db, ref, text, actor="cli", append=append)
    except JobAgentError as exc:
        fail(exc)
        return
    console.print(f"[green]Note saved[/green] for {_e(job['job_uid'])}.")


def register(app: typer.Typer) -> None:
    """Attach ``score``, ``jobs`` and the ``job`` sub-app to the main CLI."""
    app.command("score")(score_command)
    app.command("jobs")(jobs_command)
    job_app = typer.Typer(help="Inspect and annotate one job (show / mark / note).", no_args_is_help=True)
    job_app.command("show")(job_show)
    job_app.command("mark")(job_mark)
    job_app.command("note")(job_note)
    app.add_typer(job_app, name="job")
