"""Collects everything one daily report needs into a plain ``ReportData`` snapshot.

Renderers (:mod:`.workbook`, :mod:`.markdown`) only format this data; they never
query the database themselves. Nothing here reads ``ctx.secrets`` or the
process environment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from job_agent import __version__
from job_agent.database import repo
from job_agent.reporting import queries as q
from job_agent.utils.time import to_iso, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from job_agent.context import AppContext

#: Row caps keep the workbook usable on a phone and bounded in size.
MAX_JOB_ROWS = 5_000
MAX_APPLICATION_ROWS = 2_000
MAX_FOLLOWUP_ROWS = 2_000
MAX_ACTIVITY_ROWS = 2_000
MAX_NEW_JOB_ROWS = 200
TREND_DAYS = 14
DEADLINE_HORIZON_DAYS = 7


@dataclass
class ReportData:
    report_date: date
    tz_name: str
    generated_at: datetime  # aware, local time zone
    day_start_utc: str
    day_end_utc: str
    app_version: str
    mode: str
    top_n: int
    counts: dict[str, int]
    trend: list[dict[str, Any]]
    apps_by_status: list[tuple[str, int]]
    score_buckets: list[tuple[str, int]]
    jobs: list[dict[str, Any]]
    jobs_total: int
    applications: list[dict[str, Any]]
    applications_total: int
    new_jobs: list[dict[str, Any]]
    new_jobs_total: int
    top_matches: list[dict[str, Any]]
    documents: list[dict[str, Any]]
    prepared: list[dict[str, Any]]
    submissions: list[dict[str, Any]]
    confirmations: list[dict[str, Any]]
    emails: list[dict[str, Any]]
    interview_milestones: list[dict[str, Any]]
    assessment_milestones: list[dict[str, Any]]
    rejection_milestones: list[dict[str, Any]]
    offer_milestones: list[dict[str, Any]]
    deadlines: list[dict[str, Any]]
    approvals: list[dict[str, Any]]
    failures: list[dict[str, Any]]
    followups: list[dict[str, Any]]
    followups_total: int
    followups_due: list[dict[str, Any]]
    review_jobs: list[dict[str, Any]]
    review_jobs_total: int
    action_errors: list[dict[str, Any]]
    action_errors_total: int
    gmail_auth_error: str | None
    activity: list[dict[str, Any]]
    activity_total: int
    config_rows: list[tuple[str, Any]]
    config_hash: str
    using_example_config: bool
    truncated: dict[str, int] = field(default_factory=dict)

    def emails_in(self, categories: tuple[str, ...]) -> list[dict[str, Any]]:
        return [e for e in self.emails if e.get("category") in categories]


def flatten_config(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """``config.safe_dict()`` -> sorted ``(dotted.key, scalar)`` rows.

    Lists of scalars are joined with ``", "``; lists of mappings are indexed.
    """
    rows: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key in sorted(value):
            rows.extend(flatten_config(value[key], f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list):
        if all(not isinstance(v, (dict, list)) for v in value):
            rows.append((prefix, ", ".join("" if v is None else str(v) for v in value)))
        else:
            for i, item in enumerate(value):
                rows.extend(flatten_config(item, f"{prefix}[{i}]"))
    else:
        rows.append((prefix, value))
    return rows


def collect_report_data(ctx: "AppContext", report_date: date) -> ReportData:
    cfg = ctx.config
    db = ctx.db
    tz_name = cfg.app.timezone
    start, end = q.local_day_bounds(report_date, tz_name)
    horizon = to_iso(datetime.fromisoformat(start) + timedelta(days=DEADLINE_HORIZON_DAYS))
    top_n = cfg.reporting.top_matches

    jobs, jobs_total = q.all_jobs(db, end, MAX_JOB_ROWS)
    apps, apps_total = q.applications(db, MAX_APPLICATION_ROWS)
    new, new_total = q.new_jobs(db, start, end, MAX_NEW_JOB_ROWS)
    fups, fups_total = q.followups(db, MAX_FOLLOWUP_ROWS)
    review, review_total = q.needs_review_jobs(db)
    errs, errs_total = q.human_action_errors(db)
    activity, activity_total = q.activity_log(db, start, end, MAX_ACTIVITY_ROWS)

    truncated = {name: total for name, total, shown in (
        ("All Jobs", jobs_total, len(jobs)), ("Applications", apps_total, len(apps)),
        ("Follow-ups", fups_total, len(fups)), ("Activity Log", activity_total, len(activity)),
        ("New opportunities", new_total, len(new)),
    ) if total > shown}

    return ReportData(
        report_date=report_date,
        tz_name=tz_name,
        generated_at=utcnow().astimezone(ZoneInfo(tz_name)),
        day_start_utc=start,
        day_end_utc=end,
        app_version=__version__,
        mode=cfg.app.mode,
        top_n=top_n,
        counts=q.dashboard_counts(db, start, end),
        trend=q.trend_counts(db, TREND_DAYS, tz_name=tz_name, end_date=report_date),
        apps_by_status=q.applications_by_status(db),
        score_buckets=q.score_distribution(db, end),
        jobs=jobs,
        jobs_total=jobs_total,
        applications=apps,
        applications_total=apps_total,
        new_jobs=new,
        new_jobs_total=new_total,
        top_matches=q.top_matches(db, end, top_n),
        documents=q.documents_created(db, start, end),
        prepared=q.applications_by_ids(db, q.prepared_application_ids(db, start, end)),
        submissions=q.submissions(db, start, end),
        confirmations=q.confirmations(db, start, end),
        emails=q.emails_received(db, start, end),
        interview_milestones=q.milestones(db, q.INTERVIEW_STATES, start, end),
        assessment_milestones=q.milestones(db, q.ASSESSMENT_STATES, start, end),
        rejection_milestones=q.milestones(db, q.REJECTION_STATES, start, end),
        offer_milestones=q.milestones(db, q.OFFER_STATES, start, end),
        deadlines=q.deadlines(db, start, horizon),  # type: ignore[arg-type]
        approvals=q.pending_approvals(db),
        failures=q.failures(db, start, end),
        followups=fups,
        followups_total=fups_total,
        followups_due=q.followups_due(db, end),
        review_jobs=review,
        review_jobs_total=review_total,
        action_errors=errs,
        action_errors_total=errs_total,
        gmail_auth_error=repo.get_state(db, "gmail_auth_error") or None,
        activity=activity,
        activity_total=activity_total,
        config_rows=flatten_config(cfg.safe_dict()),
        config_hash=cfg.config_hash(),
        using_example_config=cfg.using_example,
        truncated=truncated,
    )
