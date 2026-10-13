"""Daily report generation: Excel workbook + Markdown summary + ``reports`` history row.

Files (in ``config.reports_dir``):

* ``Job_Search_Report_YYYY-MM-DD.xlsx``
* ``Daily_Summary_YYYY-MM-DD.md``

Each file is written to a temporary file in the same directory and moved into
place with ``os.replace`` (atomic on the same filesystem). Regenerating a date
overwrites only that date's two files; other dates are never touched. The
``reports`` row is upserted (``generation_count`` incremented).
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

from job_agent.database import repo
from job_agent.database.connection import to_json
from job_agent.errors import HumanActionRequired, PermanentError
from job_agent.logging_config import get_logger
from job_agent.reporting import queries as q
from job_agent.reporting.data import collect_report_data
from job_agent.reporting.markdown import render_markdown
from job_agent.reporting.workbook import build_workbook
from job_agent.security.sanitize import ensure_within
from job_agent.utils.time import local_today, utcnow_iso

if TYPE_CHECKING:  # pragma: no cover
    from job_agent.config import AppConfig
    from job_agent.context import AppContext
    from job_agent.tasks import RunInfo

log = get_logger(__name__)

XLSX_NAME = "Job_Search_Report_{date}.xlsx"
MARKDOWN_NAME = "Daily_Summary_{date}.md"


@dataclass(frozen=True)
class ReportResult:
    report_date: date
    xlsx_path: Path
    markdown_path: Path
    stats: dict[str, Any]
    generation_count: int = 1


def report_paths(config: "AppConfig", report_date: date) -> tuple[Path, Path]:
    """Absolute (resolved) paths of the two files for ``report_date``, inside ``reports_dir``."""
    base = config.reports_dir
    day = report_date.isoformat()
    return (ensure_within(base, Path(XLSX_NAME.format(date=day))),
            ensure_within(base, Path(MARKDOWN_NAME.format(date=day))))


def _atomic_write(target: Path, write: Callable[[Path], Any]) -> None:
    """Write via a same-directory temp file + ``os.replace``; the temp file never survives."""
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        write(tmp)
        os.replace(tmp, target)
    except PermissionError as exc:
        raise HumanActionRequired(
            f"cannot write report file {target.name}: {exc.strerror or exc}",
            human_action="Close the report if it is open in another program (e.g. Excel), then generate it again.",
        ) from exc
    except OSError as exc:
        raise PermanentError(
            f"cannot write report file {target.name}: {exc.strerror or exc}",
            human_action="Check free disk space and permissions of the reports directory.",
        ) from exc
    finally:
        tmp.unlink(missing_ok=True)


def _validate_date(ctx: "AppContext", report_date: date | None) -> date:
    tz = ctx.config.app.timezone
    today = local_today(tz)
    if report_date is None:
        return today
    if isinstance(report_date, datetime) or not isinstance(report_date, date):
        raise PermanentError(f"report_date must be a date, not {type(report_date).__name__}")
    if report_date > today:
        raise PermanentError(f"cannot generate a report for a future date ({report_date.isoformat()}); "
                             f"today is {today.isoformat()} in {tz}")
    return report_date


def generate_daily_report(ctx: "AppContext", report_date: date | None = None, *, actor: str = "system",
                          run_id: int | None = None) -> ReportResult:
    """Generate (or regenerate) the workbook and Markdown summary for one local date.

    ``report_date`` defaults to today in ``config.app.timezone``. Idempotent per
    date: the same two files are replaced and the ``reports`` row's
    ``generation_count`` is incremented.
    """
    cfg = ctx.config
    day = _validate_date(ctx, report_date)
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    xlsx_path, md_path = report_paths(cfg, day)

    data = collect_report_data(ctx, day)
    workbook = build_workbook(data)
    markdown = render_markdown(data)

    _atomic_write(xlsx_path, workbook.save)
    _atomic_write(md_path, lambda p: p.write_text(markdown, encoding="utf-8", newline="\n"))

    now = utcnow_iso()
    stats: dict[str, Any] = {
        "report_date": day.isoformat(),
        "generated_at": now,
        "timezone": cfg.app.timezone,
        "counts": data.counts,
        "rows": {"all_jobs": len(data.jobs), "applications": len(data.applications),
                 "follow_ups": len(data.followups), "activity_log": len(data.activity)},
        "truncated": data.truncated,
    }
    db = ctx.db
    with db.transaction():
        db.execute(
            "INSERT INTO reports (report_date, xlsx_file, markdown_file, stats, generation_count, "
            "first_generated_at, last_generated_at) VALUES (?, ?, ?, ?, 1, ?, ?) "
            "ON CONFLICT(report_date) DO UPDATE SET xlsx_file = excluded.xlsx_file, "
            "markdown_file = excluded.markdown_file, stats = excluded.stats, "
            "generation_count = reports.generation_count + 1, last_generated_at = excluded.last_generated_at",
            (day.isoformat(), xlsx_path.name, md_path.name, to_json(stats), now, now),
        )
        generation = int(db.scalar("SELECT generation_count FROM reports WHERE report_date = ?",
                                   (day.isoformat(),)))
        repo.audit(db, actor=actor, action="report_generated", entity_type="report", entity_id=day.isoformat(),
                   outcome="success", run_id=run_id,
                   details={"xlsx_file": xlsx_path.name, "markdown_file": md_path.name,
                            "generation_count": generation})
    log.info("report %s generated (generation %d)", day.isoformat(), generation)
    return ReportResult(report_date=day, xlsx_path=xlsx_path, markdown_path=md_path, stats=stats,
                        generation_count=generation)


def missed_previous_day(ctx: "AppContext", today: date, *, exclude_run_id: int | None = None) -> date | None:
    """Yesterday (local), if it has no report yet although the agent already had data before today.

    A 21:30 report missed because the laptop was asleep or off is otherwise never
    written: the catch-up after midnight would only produce the new day.
    """
    yesterday = today - timedelta(days=1)
    db = ctx.db
    if db.scalar("SELECT 1 FROM reports WHERE report_date = ?", (yesterday.isoformat(),)):
        return None
    start, _ = q.local_day_bounds(today, ctx.config.app.timezone)
    before = "julianday({}) < julianday(?)"
    checks = (
        (f"SELECT 1 FROM jobs WHERE {before.format('discovered_at')} LIMIT 1", (start,)),
        (f"SELECT 1 FROM applications WHERE {before.format('created_at')} LIMIT 1", (start,)),
        (f"SELECT 1 FROM email_messages WHERE {before.format('COALESCE(received_at, processed_at)')} LIMIT 1",
         (start,)),
        (f"SELECT 1 FROM agent_runs WHERE {before.format('started_at')} AND id != ? LIMIT 1",
         (start, -1 if exclude_run_id is None else exclude_run_id)),
    )
    return yesterday if any(db.scalar(sql, params) for sql, params in checks) else None


def report_task(ctx: "AppContext", run: "RunInfo") -> dict[str, Any]:
    """``report`` task (scheduler / dashboard / ``job-agent tasks run report``): today's report, plus
    yesterday's when it was never generated (e.g. the 21:30 run was missed and caught up after midnight)."""
    actor = "scheduler" if run.trigger == "scheduled" else "system"
    today = local_today(ctx.config.app.timezone)
    caught_up: list[str] = []
    missed = missed_previous_day(ctx, today, exclude_run_id=run.run_id)
    if missed is not None:
        previous = generate_daily_report(ctx, missed, actor=actor, run_id=run.run_id)
        caught_up.append(previous.report_date.isoformat())
        log.info("generated the missed report for %s", missed.isoformat())
    result = generate_daily_report(ctx, today, actor=actor, run_id=run.run_id)
    counts = result.stats["counts"]
    return {
        "report_date": result.report_date.isoformat(),
        "xlsx_file": result.xlsx_path.name,
        "markdown_file": result.markdown_path.name,
        "generation_count": result.generation_count,
        "caught_up_reports": caught_up,
        **{k: counts.get(k, 0) for k in ("new_jobs_today", "submitted_today", "approvals_pending",
                                         "errors_requiring_attention")},
    }


def list_reports(ctx: "AppContext", limit: int = 60) -> list[dict[str, Any]]:
    """Report history (newest first) with whether each file still exists on disk."""
    base = ctx.config.reports_dir
    out = []
    for row in q.report_history(ctx.db, limit):
        for key in ("xlsx_file", "markdown_file"):
            name = row.get(key)
            exists = False
            if name:
                try:
                    exists = ensure_within(base, Path(name)).is_file()
                except PermanentError:
                    log.warning("report row %s has a %s outside the reports directory", row["id"], key)
            row[f"{key}_exists"] = exists
        out.append(row)
    return out


def resolve_report_file(ctx: "AppContext", report_id: int, kind: Literal["xlsx", "markdown"]) -> Path:
    """Path of a report file by ``reports.id`` (for downloads); always inside ``reports_dir``."""
    if kind not in ("xlsx", "markdown"):
        raise PermanentError(f"unknown report file kind {kind!r}")
    row = ctx.db.query_one("SELECT xlsx_file, markdown_file FROM reports WHERE id = ?", (int(report_id),))
    name = None if row is None else row["xlsx_file" if kind == "xlsx" else "markdown_file"]
    if not name:
        raise PermanentError(f"report {report_id} has no {kind} file")
    path = ensure_within(ctx.config.reports_dir, Path(name))
    if not path.is_file():
        raise PermanentError(f"report file {name} no longer exists",
                             human_action="Regenerate it with `job-agent report --date YYYY-MM-DD`.")
    return path
