"""Read-only SQL for reports and the dashboard.

All functions here only ``SELECT``. Timestamps are compared with SQLite's
``julianday()`` so that ISO strings with ``Z``, ``+00:00`` or fractional seconds
compare correctly. "Today" windows are passed in as UTC ISO strings computed
from the *local* report date (see :func:`local_day_bounds`).

Metric definitions (also documented in ``docs/reports.md``):

* **submitted** -- an application whose status is ``submitted`` or a later
  outcome state (or ``withdrawn``/``closed`` with a recorded ``submitted_at``)
  *and* that has submission evidence: ``confirmation_status`` is
  ``page_evidence``/``email_confirmed``/``user_reported``, or a status-change
  event into ``submitted``/``confirmation_received`` carries an evidence payload
  or was recorded by the user. Prepared forms, pending approvals and
  ``submission_attempted`` (unknown outcome) never count.
* **interviews / assessments / rejections / offers** -- applications that ever
  reached that state (from ``application_events``; the current status is used
  as a fallback for rows without events). A milestone you corrected away (the
  next status change is one of ``models.USER_CORRECTIONS``, e.g. an email
  wrongly set "rejected" and you set it back to "submitted") is not counted;
  the same holds for confirmations.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

from job_agent.database.connection import Database, from_json
from job_agent.models import USER_CORRECTIONS, PipelineState
from job_agent.reporting.text import parse_ts
from job_agent.utils.time import to_iso, utcnow_iso

# --------------------------------------------------------------------------- #
# State groups
# --------------------------------------------------------------------------- #
POST_SUBMIT_STATES: tuple[str, ...] = (
    "submitted",
    "confirmation_received",
    "assessment_received",
    "interview_requested",
    "interview_scheduled",
    "rejected",
    "offer_received",
)
INTERVIEW_STATES: tuple[str, ...] = ("interview_requested", "interview_scheduled")
ASSESSMENT_STATES: tuple[str, ...] = ("assessment_received",)
REJECTION_STATES: tuple[str, ...] = ("rejected",)
OFFER_STATES: tuple[str, ...] = ("offer_received",)
EVIDENCE_CONFIRMATION_STATUSES: tuple[str, ...] = ("page_evidence", "email_confirmed", "user_reported")
CONFIRMED_STATUSES: tuple[str, ...] = ("page_evidence", "email_confirmed")
OPEN_FOLLOWUP_EXCLUDED: tuple[str, ...] = ("completed", "dismissed")
TERMINAL_APPLICATION_STATES: tuple[str, ...] = ("rejected", "withdrawn", "closed", "failed")

INTERVIEW_EMAIL_CATEGORIES: tuple[str, ...] = ("interview_invitation", "scheduling_request")
RECRUITER_EMAIL_CATEGORIES: tuple[str, ...] = ("recruiter_message", "follow_up_opportunity")
ASSESSMENT_EMAIL_CATEGORIES: tuple[str, ...] = ("assessment",)

_LITERAL_OK = re.compile(r"^[a-z_]+$")


def _lit(values: Iterable[str]) -> str:
    """SQL list of *code constants* (never user data) for ``IN (...)``."""
    items = list(values)
    for v in items:
        if not _LITERAL_OK.match(v):  # defensive: constants only
            raise ValueError(f"unsafe SQL literal {v!r}")
    return ", ".join(f"'{v}'" for v in items)


def _win(column: str) -> str:
    """``column`` within [start, end) -- two ``?`` parameters."""
    return f"(julianday({column}) >= julianday(?) AND julianday({column}) < julianday(?))"


def _before(column: str) -> str:
    """``column`` strictly before a bound -- one ``?`` parameter."""
    return f"(julianday({column}) < julianday(?))"


#: Applications (alias ``a``) that were actually submitted, with evidence.
SUBMITTED_SQL = (
    f"((a.status IN ({_lit(POST_SUBMIT_STATES)}) OR (a.status IN ('withdrawn', 'closed') "
    f"AND a.submitted_at IS NOT NULL)) AND (a.confirmation_status IN ({_lit(EVIDENCE_CONFIRMATION_STATUSES)}) "
    "OR EXISTS (SELECT 1 FROM application_events ev WHERE ev.application_id = a.id "
    "AND ev.to_status IN ('submitted', 'confirmation_received') AND (ev.actor = 'user' OR "
    "(ev.evidence IS NOT NULL AND trim(ev.evidence) NOT IN ('', '{}', 'null'))))))"
)
SUBMITTED_AT_SQL = "COALESCE(a.submitted_at, a.status_updated_at)"

#: Latest current score row for job alias ``j``.
CURRENT_SCORE_JOIN = (
    "LEFT JOIN job_scores s ON s.id = (SELECT MAX(s2.id) FROM job_scores s2 "
    "WHERE s2.job_id = j.id AND s2.is_current = 1)"
)


def _rows(rows: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _int(db: Database, sql: str, params: Sequence[Any] = ()) -> int:
    value = db.scalar(sql, params)
    return int(value or 0)


def _correction_pairs_sql(alias: str) -> str:
    """``alias`` (an application_events row) is a move in ``models.USER_CORRECTIONS``."""
    by_source: dict[str, set[str]] = {}
    for src, dst in USER_CORRECTIONS:
        by_source.setdefault(src.value, set()).add(dst.value)
    parts = [f"({alias}.from_status = {_lit([src])} AND {alias}.to_status IN ({_lit(sorted(dsts))}))"
             for src, dsts in sorted(by_source.items())]
    return "(" + " OR ".join(parts) + ")" if parts else "0"


def _corrected_sql(alias: str) -> str:
    """The status change ``alias`` was undone by you: the application's NEXT status change is a user
    correction (``models.USER_CORRECTIONS``) away from the state ``alias`` entered."""
    if alias not in {"ev", "c1"}:  # code constants only
        raise ValueError("bad event alias")
    return (
        f"EXISTS (SELECT 1 FROM application_events nx WHERE nx.id = (SELECT MIN(n2.id) FROM application_events n2 "
        f"WHERE n2.application_id = {alias}.application_id AND n2.id > {alias}.id AND n2.to_status IS NOT NULL) "
        f"AND nx.actor = 'user' AND nx.from_status = {alias}.to_status AND {_correction_pairs_sql('nx')})"
    )


def _milestones_sql(states: Sequence[str]) -> str:
    """(application_id, status, at) for every time an application reached ``states``.

    Uses status-change events, except ones you corrected away (see
    :func:`_corrected_sql`); an application currently in one of the states
    without a matching event (e.g. imported data) contributes its
    ``status_updated_at`` instead.
    """
    lit = _lit(states)
    return (
        f"SELECT ev.application_id AS application_id, ev.to_status AS status, ev.created_at AS at "
        f"FROM application_events ev WHERE ev.to_status IN ({lit}) AND NOT {_corrected_sql('ev')} "
        f"UNION ALL "
        f"SELECT a.id AS application_id, a.status AS status, a.status_updated_at AS at FROM applications a "
        f"WHERE a.status IN ({lit}) AND NOT EXISTS (SELECT 1 FROM application_events e2 "
        f"WHERE e2.application_id = a.id AND e2.to_status = a.status)"
    )


# --------------------------------------------------------------------------- #
# Windows
# --------------------------------------------------------------------------- #
def local_day_bounds(day: date, tz_name: str) -> tuple[str, str]:
    """UTC ISO bounds [start, end) of a local calendar day (DST-aware)."""
    tz = ZoneInfo(tz_name)
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return to_iso(start), to_iso(end)  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# Dashboard counts and trends (reused by the dashboard)
# --------------------------------------------------------------------------- #
def dashboard_counts(db: Database, day_start_utc: str, day_end_utc: str) -> dict[str, int]:
    """Headline counts for one report day.

    ``*_today`` counts use the [day_start_utc, day_end_utc) window. Cumulative
    counts include records created before ``day_end_utc``. "Open item" counts
    (awaiting approval, pending approvals/follow-ups, errors) reflect the
    current database state.
    """
    s, e = day_start_utc, day_end_utc
    now = utcnow_iso()
    c: dict[str, int] = {}
    c["total_jobs"] = _int(db, f"SELECT COUNT(*) FROM jobs WHERE duplicate_of_job_id IS NULL AND "
                               f"{_before('discovered_at')}", (e,))
    c["new_jobs_today"] = _int(db, f"SELECT COUNT(*) FROM jobs WHERE duplicate_of_job_id IS NULL AND "
                                   f"{_win('discovered_at')}", (s, e))
    c["new_relevant_today"] = _int(db, f"SELECT COUNT(*) FROM jobs WHERE duplicate_of_job_id IS NULL AND "
                                       f"pipeline_state != 'filtered_out' AND {_win('discovered_at')}", (s, e))
    c["jobs_scored"] = _int(
        db,
        "SELECT COUNT(DISTINCT j.id) FROM jobs j JOIN job_scores s ON s.job_id = j.id AND s.is_current = 1 "
        f"WHERE j.duplicate_of_job_id IS NULL AND {_before('j.discovered_at')}",
        (e,),
    )
    c["shortlisted"] = _int(db, "SELECT COUNT(*) FROM jobs WHERE duplicate_of_job_id IS NULL "
                                "AND pipeline_state = 'shortlisted'")
    c["applications_total"] = _int(db, f"SELECT COUNT(*) FROM applications WHERE {_before('created_at')}", (e,))
    c["applications_prepared_today"] = len(prepared_application_ids(db, s, e))
    c["awaiting_approval"] = _int(db, "SELECT COUNT(*) FROM applications WHERE status = 'awaiting_approval'")
    c["approvals_pending"] = _int(
        db,
        "SELECT COUNT(*) FROM approval_requests WHERE status = 'pending' "
        "AND (expires_at IS NULL OR julianday(expires_at) > julianday(?))",
        (now,),
    )
    c["submitted"] = _int(db, f"SELECT COUNT(*) FROM applications a WHERE {SUBMITTED_SQL} "
                              f"AND {_before(SUBMITTED_AT_SQL)}", (e,))
    c["submitted_today"] = _int(db, f"SELECT COUNT(*) FROM applications a WHERE {SUBMITTED_SQL} "
                                    f"AND {_win(SUBMITTED_AT_SQL)}", (s, e))
    c["confirmations"] = _int(
        db,
        f"SELECT COUNT(*) FROM applications a WHERE {SUBMITTED_SQL} AND {_before(SUBMITTED_AT_SQL)} AND "
        f"(a.confirmation_status IN ({_lit(CONFIRMED_STATUSES)}) OR EXISTS (SELECT 1 FROM application_events c1 "
        f"WHERE c1.application_id = a.id AND c1.to_status = 'confirmation_received' AND NOT {_corrected_sql('c1')}))",
        (e,),
    )
    for key, states in (("interviews", INTERVIEW_STATES), ("assessments", ASSESSMENT_STATES),
                        ("rejections", REJECTION_STATES), ("offers", OFFER_STATES)):
        c[key] = _int(db, f"SELECT COUNT(DISTINCT m.application_id) FROM ({_milestones_sql(states)}) m "
                          f"WHERE {_before('m.at')}", (e,))
    c["pending_followups"] = _int(db, f"SELECT COUNT(*) FROM followups WHERE status NOT IN "
                                      f"({_lit(OPEN_FOLLOWUP_EXCLUDED)})")
    c["followups_due"] = _int(db, f"SELECT COUNT(*) FROM followups WHERE status NOT IN "
                                  f"({_lit(OPEN_FOLLOWUP_EXCLUDED)}) AND {_before('due_at')}", (e,))
    c["errors_requiring_attention"] = _int(db, "SELECT COUNT(*) FROM errors WHERE resolved = 0 "
                                               "AND requires_human_action = 1")
    c["errors_today"] = _int(db, f"SELECT COUNT(*) FROM errors WHERE {_win('occurred_at')}", (s, e))
    c["documents_generated_today"] = _int(db, f"SELECT COUNT(*) FROM document_versions WHERE "
                                              f"{_win('created_at')}", (s, e))
    email_at = "COALESCE(received_at, processed_at)"
    c["emails_today"] = _int(db, f"SELECT COUNT(*) FROM email_messages WHERE {_win(email_at)}", (s, e))
    for key, cats in (("recruiter_messages_today", RECRUITER_EMAIL_CATEGORIES),
                      ("interview_messages_today", INTERVIEW_EMAIL_CATEGORIES),
                      ("assessment_messages_today", ASSESSMENT_EMAIL_CATEGORIES)):
        c[key] = _int(db, f"SELECT COUNT(*) FROM email_messages WHERE category IN ({_lit(cats)}) "
                          f"AND {_win(email_at)}", (s, e))
    return c


def trend_counts(db: Database, days: int, *, tz_name: str = "UTC",
                 end_date: date | None = None) -> list[dict[str, Any]]:
    """Per-local-day counts for the ``days`` days ending at ``end_date`` (default: today in ``tz_name``).

    Returns ``[{date, discovered, applications, submitted, interviews, rejections, offers, emails}]``
    oldest first; ``date`` is an ISO ``YYYY-MM-DD`` string. Each timestamp is bucketed by its own
    local date (DST-aware), so the report and the dashboard agree. Pass
    ``tz_name=config.app.timezone`` so days match the user's calendar.
    """
    if days < 1 or days > 366:
        raise ValueError("days must be between 1 and 366")
    tz = ZoneInfo(tz_name)
    end_date = end_date or datetime.now(tz).date()
    dates = [end_date - timedelta(days=i) for i in range(days - 1, -1, -1)]
    start_utc, _ = local_day_bounds(dates[0], tz_name)
    _, end_utc = local_day_bounds(end_date, tz_name)
    keys = ("discovered", "applications", "submitted", "interviews", "rejections", "offers", "emails")
    buckets: dict[date, dict[str, Any]] = {d: {"date": d.isoformat(), **{k: 0 for k in keys}} for d in dates}
    params = (start_utc, end_utc)

    def add(key: str, rows: Sequence[sqlite3.Row]) -> None:
        seen: set[tuple[Any, date]] = set()
        for row in rows:
            dt = parse_ts(row["at"])
            if dt is None:
                continue
            local = dt.astimezone(tz).date()
            ident = (row["ident"], local)
            if local in buckets and ident not in seen:
                seen.add(ident)
                buckets[local][key] += 1

    add("discovered", db.query_all(
        f"SELECT id AS ident, discovered_at AS at FROM jobs WHERE duplicate_of_job_id IS NULL AND "
        f"{_win('discovered_at')}", params))
    add("applications", db.query_all(
        f"SELECT id AS ident, created_at AS at FROM applications WHERE {_win('created_at')}", params))
    add("submitted", db.query_all(
        f"SELECT a.id AS ident, {SUBMITTED_AT_SQL} AS at FROM applications a WHERE {SUBMITTED_SQL} "
        f"AND {_win(SUBMITTED_AT_SQL)}", params))
    for key, states in (("interviews", INTERVIEW_STATES), ("rejections", REJECTION_STATES),
                        ("offers", OFFER_STATES)):
        add(key, db.query_all(
            f"SELECT m.application_id AS ident, m.at AS at FROM ({_milestones_sql(states)}) m "
            f"WHERE {_win('m.at')}", params))
    email_at = "COALESCE(received_at, processed_at)"
    add("emails", db.query_all(
        f"SELECT id AS ident, {email_at} AS at FROM email_messages WHERE {_win(email_at)}", params))
    return [buckets[d] for d in dates]


def applications_by_status(db: Database) -> list[tuple[str, int]]:
    rows = db.query_all("SELECT status, COUNT(*) AS n FROM applications GROUP BY status")
    counts = {r["status"]: int(r["n"]) for r in rows}
    order = [p.value for p in PipelineState]
    ordered = [(st, counts[st]) for st in order if st in counts]
    ordered += [(st, n) for st, n in sorted(counts.items()) if st not in order]
    return ordered


def score_distribution(db: Database, day_end_utc: str | None = None) -> list[tuple[str, int]]:
    """Current scores bucketed into 10-point ranges (``90-100`` includes 100)."""
    where = "j.duplicate_of_job_id IS NULL"
    params: tuple[Any, ...] = ()
    if day_end_utc:
        where += f" AND {_before('j.discovered_at')}"
        params = (day_end_utc,)
    rows = db.query_all(f"SELECT s.overall_score AS score FROM jobs j {CURRENT_SCORE_JOIN} "
                        f"WHERE {where} AND s.id IS NOT NULL", params)
    buckets = [0] * 10
    for r in rows:
        score = float(r["score"])
        buckets[min(int(score // 10), 9) if score >= 0 else 0] += 1
    labels = [f"{i * 10}-{i * 10 + 9}" for i in range(9)] + ["90-100"]
    return list(zip(labels, buckets))


# --------------------------------------------------------------------------- #
# Sheet data
# --------------------------------------------------------------------------- #
_JOB_COLUMNS = (
    "j.id, j.job_uid, j.employer, j.title, j.location, j.work_arrangement, j.employment_type, "
    "j.salary_min, j.salary_max, j.salary_currency, j.salary_period, j.salary_is_base, j.salary_text, "
    "j.posted_at, j.discovered_at, j.closing_at, j.job_url, j.application_url, j.source_name, "
    "j.source_identifier, j.listing_status, j.pipeline_state, j.filter_reason, j.last_checked_at, "
    "s.overall_score, s.category, s.shortlisted AS score_shortlisted, s.missing_qualifications, "
    "s.matching_qualifications, s.eligibility_flags, s.reasons_to_apply, s.recommended_next_action, "
    "a.id AS application_id, a.application_uid, a.status AS application_status"
)
_JSON_LIST_COLUMNS = ("missing_qualifications", "matching_qualifications", "eligibility_flags", "reasons_to_apply")


def _decode_job(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for col in _JSON_LIST_COLUMNS:
        if col in d:
            d[col] = from_json(d[col], []) or []
    return d


def all_jobs(db: Database, day_end_utc: str, limit: int) -> tuple[list[dict[str, Any]], int]:
    """Non-duplicate jobs discovered before ``day_end_utc``, best score first. Returns (rows, total)."""
    where = f"j.duplicate_of_job_id IS NULL AND {_before('j.discovered_at')}"
    total = _int(db, f"SELECT COUNT(*) FROM jobs j WHERE {where}", (day_end_utc,))
    rows = db.query_all(
        f"SELECT {_JOB_COLUMNS} FROM jobs j {CURRENT_SCORE_JOIN} LEFT JOIN applications a ON a.job_id = j.id "
        f"WHERE {where} ORDER BY (s.overall_score IS NULL), s.overall_score DESC, "
        "julianday(j.discovered_at) DESC, j.id DESC LIMIT ?",
        (day_end_utc, limit),
    )
    return [_decode_job(r) for r in rows], total


def new_jobs(db: Database, day_start_utc: str, day_end_utc: str, limit: int) -> tuple[list[dict[str, Any]], int]:
    where = f"j.duplicate_of_job_id IS NULL AND {_win('j.discovered_at')}"
    params = (day_start_utc, day_end_utc)
    total = _int(db, f"SELECT COUNT(*) FROM jobs j WHERE {where}", params)
    rows = db.query_all(
        f"SELECT {_JOB_COLUMNS} FROM jobs j {CURRENT_SCORE_JOIN} LEFT JOIN applications a ON a.job_id = j.id "
        f"WHERE {where} ORDER BY (s.overall_score IS NULL), s.overall_score DESC, j.id DESC LIMIT ?",
        (*params, limit),
    )
    return [_decode_job(r) for r in rows], total


def top_matches(db: Database, day_end_utc: str, limit: int) -> list[dict[str, Any]]:
    """Best current scores among active, not filtered-out, non-duplicate listings."""
    rows = db.query_all(
        f"SELECT {_JOB_COLUMNS} FROM jobs j {CURRENT_SCORE_JOIN} LEFT JOIN applications a ON a.job_id = j.id "
        "WHERE j.duplicate_of_job_id IS NULL AND s.id IS NOT NULL AND j.listing_status = 'active' "
        f"AND j.pipeline_state NOT IN ('filtered_out', 'closed') AND {_before('j.discovered_at')} "
        "ORDER BY s.overall_score DESC, julianday(j.discovered_at) DESC, j.id DESC LIMIT ?",
        (day_end_utc, limit),
    )
    return [_decode_job(r) for r in rows]


def needs_review_jobs(db: Database, limit: int = 50) -> tuple[list[dict[str, Any]], int]:
    """Active jobs scored ``needs_review`` that carry eligibility flags (and have no application yet)."""
    where = (
        "j.duplicate_of_job_id IS NULL AND s.category = 'needs_review' AND j.listing_status = 'active' "
        "AND j.pipeline_state NOT IN ('filtered_out', 'closed') AND a.id IS NULL "
        "AND s.eligibility_flags IS NOT NULL AND trim(s.eligibility_flags) NOT IN ('', '[]', 'null')"
    )
    base = f"FROM jobs j {CURRENT_SCORE_JOIN} LEFT JOIN applications a ON a.job_id = j.id WHERE {where}"
    total = _int(db, f"SELECT COUNT(*) {base}")
    rows = db.query_all(f"SELECT {_JOB_COLUMNS} {base} ORDER BY s.overall_score DESC, j.id DESC LIMIT ?", (limit,))
    return [_decode_job(r) for r in rows], total


_APP_COLUMNS = (
    "a.id, a.application_uid, a.job_id, a.status, a.status_updated_at, a.application_url, a.prepared_at, "
    "a.submitted_at, a.confirmation_status, a.submission_attempts, a.last_activity_at, a.next_action, "
    "a.follow_up_due_at, a.notes, a.created_at, j.job_uid, j.employer, j.title, j.job_url, "
    f"CASE WHEN {SUBMITTED_SQL} THEN 1 ELSE 0 END AS evidenced_submitted"
)


def _doc_names_sql(doc_type: str, id_column: str) -> str:
    if (doc_type, id_column) not in {("resume", "resume_document_id"), ("cover_letter", "cover_letter_document_id")}:
        raise ValueError("bad document column")
    return (
        f"COALESCE((SELECT d.file_name FROM document_versions d WHERE d.id = a.{id_column}), "
        f"(SELECT group_concat(d.file_name, ', ') FROM document_versions d WHERE d.job_id = a.job_id "
        f"AND d.doc_type = '{doc_type}' AND d.version = (SELECT MAX(d2.version) FROM document_versions d2 "
        f"WHERE d2.job_id = a.job_id AND d2.doc_type = '{doc_type}')))"
    )


def applications(db: Database, limit: int) -> tuple[list[dict[str, Any]], int]:
    total = _int(db, "SELECT COUNT(*) FROM applications")
    rows = db.query_all(
        f"SELECT {_APP_COLUMNS}, {_doc_names_sql('resume', 'resume_document_id')} AS resume_file, "
        f"{_doc_names_sql('cover_letter', 'cover_letter_document_id')} AS cover_letter_file, "
        f"(SELECT f.due_at FROM followups f WHERE f.application_id = a.id AND f.status NOT IN "
        f"({_lit(OPEN_FOLLOWUP_EXCLUDED)}) ORDER BY julianday(f.due_at) LIMIT 1) AS next_followup_due "
        "FROM applications a JOIN jobs j ON j.id = a.job_id "
        "ORDER BY julianday(a.last_activity_at) DESC, a.id DESC LIMIT ?",
        (limit,),
    )
    return _rows(rows), total


def prepared_application_ids(db: Database, day_start_utc: str, day_end_utc: str) -> set[int]:
    """Applications whose form was prepared (or that entered awaiting_approval) in the window."""
    params = (day_start_utc, day_end_utc)
    ids = {int(r[0]) for r in db.query_all(f"SELECT id FROM applications WHERE {_win('prepared_at')}", params)}
    ids |= {int(r[0]) for r in db.query_all(
        "SELECT DISTINCT application_id FROM application_events WHERE (event_type = 'form_prepared' "
        f"OR to_status = 'awaiting_approval') AND {_win('created_at')}", params)}
    return ids


def applications_by_ids(db: Database, ids: Iterable[int]) -> list[dict[str, Any]]:
    id_list = sorted(set(int(i) for i in ids))
    if not id_list:
        return []
    placeholders = ", ".join("?" for _ in id_list)
    rows = db.query_all(f"SELECT {_APP_COLUMNS} FROM applications a JOIN jobs j ON j.id = a.job_id "
                        f"WHERE a.id IN ({placeholders}) ORDER BY a.id", id_list)
    return _rows(rows)


def submissions(db: Database, day_start_utc: str, day_end_utc: str) -> list[dict[str, Any]]:
    """Applications actually submitted (evidence-based) within the window."""
    rows = db.query_all(
        f"SELECT {_APP_COLUMNS} FROM applications a JOIN jobs j ON j.id = a.job_id "
        f"WHERE {SUBMITTED_SQL} AND {_win(SUBMITTED_AT_SQL)} ORDER BY julianday({SUBMITTED_AT_SQL})",
        (day_start_utc, day_end_utc),
    )
    return _rows(rows)


def confirmations(db: Database, day_start_utc: str, day_end_utc: str) -> list[dict[str, Any]]:
    """Applications confirmed within the window (confirmation event, or confirmed submission that day)."""
    params = (day_start_utc, day_end_utc)
    rows = db.query_all(
        f"SELECT {_APP_COLUMNS} FROM applications a JOIN jobs j ON j.id = a.job_id WHERE {SUBMITTED_SQL} AND ("
        f"EXISTS (SELECT 1 FROM application_events c1 WHERE c1.application_id = a.id "
        f"AND c1.to_status = 'confirmation_received' AND NOT {_corrected_sql('c1')} AND {_win('c1.created_at')}) OR "
        f"(a.confirmation_status IN ({_lit(CONFIRMED_STATUSES)}) AND {_win(SUBMITTED_AT_SQL)})) ORDER BY a.id",
        (*params, *params),
    )
    return _rows(rows)


def milestones(db: Database, states: Sequence[str], day_start_utc: str, day_end_utc: str) -> list[dict[str, Any]]:
    """Applications that reached one of ``states`` within the window: one row per application,
    showing the latest such milestone (SQLite returns the bare columns of the ``MAX`` row)."""
    rows = db.query_all(
        f"SELECT m.application_id, m.status AS milestone, m.at AS at, MAX(julianday(m.at)) AS _jd, "
        f"j.employer, j.title, j.job_url FROM ({_milestones_sql(states)}) m "
        f"JOIN applications a ON a.id = m.application_id JOIN jobs j ON j.id = a.job_id "
        f"WHERE {_win('m.at')} GROUP BY m.application_id ORDER BY _jd, m.application_id",
        (day_start_utc, day_end_utc),
    )
    return _rows(rows)


def documents_created(db: Database, day_start_utc: str, day_end_utc: str, limit: int = 500) -> list[dict[str, Any]]:
    rows = db.query_all(
        "SELECT d.id, d.job_id, d.doc_type, d.version, d.format, d.file_name, d.status, d.generator, "
        "d.created_at, j.employer, j.title FROM document_versions d JOIN jobs j ON j.id = d.job_id "
        f"WHERE {_win('d.created_at')} ORDER BY julianday(d.created_at), d.id LIMIT ?",
        (day_start_utc, day_end_utc, limit),
    )
    return _rows(rows)


def emails_received(db: Database, day_start_utc: str, day_end_utc: str, limit: int = 500) -> list[dict[str, Any]]:
    """Email messages received (or, if unknown, processed) within the window, with their best job match."""
    at = "COALESCE(m.received_at, m.processed_at)"
    rows = db.query_all(
        f"SELECT m.id, {at} AS at, m.from_domain, m.subject, m.category, m.summary, m.requires_attention, "
        "m.injection_suspected, j.employer, j.title FROM email_messages m "
        "LEFT JOIN jobs j ON j.id = (SELECT COALESCE(ea.job_id, ap.job_id) FROM email_associations ea "
        "LEFT JOIN applications ap ON ap.id = ea.application_id WHERE ea.email_id = m.id "
        "ORDER BY ea.confirmed_by_user DESC, ea.confidence DESC, ea.id LIMIT 1) "
        f"WHERE {_win(at)} ORDER BY julianday({at}), m.id LIMIT ?",
        (day_start_utc, day_end_utc, limit),
    )
    return _rows(rows)


def pending_approvals(db: Database, limit: int = 200) -> list[dict[str, Any]]:
    """Pending, unexpired approval requests (read-only; does not expire rows)."""
    rows = db.query_all(
        "SELECT ar.id, ar.approval_uid, ar.request_type, ar.application_id, ar.followup_id, ar.summary, "
        "ar.requested_at, ar.expires_at, j.employer, j.title FROM approval_requests ar "
        "LEFT JOIN followups f ON f.id = ar.followup_id "
        "LEFT JOIN applications a ON a.id = COALESCE(ar.application_id, f.application_id) "
        "LEFT JOIN jobs j ON j.id = COALESCE(a.job_id, f.job_id) "
        "WHERE ar.status = 'pending' AND (ar.expires_at IS NULL OR julianday(ar.expires_at) > julianday(?)) "
        "ORDER BY julianday(ar.requested_at), ar.id LIMIT ?",
        (utcnow_iso(), limit),
    )
    return _rows(rows)


_FOLLOWUP_SELECT = (
    "SELECT f.id, f.application_id, f.job_id, f.followup_type, f.due_at, f.suggested_action, f.draft_text, "
    "f.status, f.completed_at, f.updated_at, COALESCE(a.last_activity_at, m.received_at, f.updated_at) "
    "AS last_activity_at, j.employer, j.title, "
    "(SELECT ar.status FROM approval_requests ar WHERE ar.followup_id = f.id "
    "ORDER BY julianday(ar.requested_at) DESC, ar.id DESC LIMIT 1) AS approval_status, "
    "(SELECT ar.expires_at FROM approval_requests ar WHERE ar.followup_id = f.id "
    "ORDER BY julianday(ar.requested_at) DESC, ar.id DESC LIMIT 1) AS approval_expires_at "
    "FROM followups f LEFT JOIN applications a ON a.id = f.application_id "
    "LEFT JOIN email_messages m ON m.id = f.email_id "
    "LEFT JOIN jobs j ON j.id = COALESCE(a.job_id, f.job_id)"
)


def followups(db: Database, limit: int) -> tuple[list[dict[str, Any]], int]:
    """All follow-ups, open ones first (by due date)."""
    total = _int(db, "SELECT COUNT(*) FROM followups")
    rows = db.query_all(
        f"{_FOLLOWUP_SELECT} ORDER BY (f.status IN ({_lit(OPEN_FOLLOWUP_EXCLUDED)})), julianday(f.due_at), f.id "
        "LIMIT ?",
        (limit,),
    )
    return _rows(rows), total


def followups_due(db: Database, day_end_utc: str, limit: int = 100) -> list[dict[str, Any]]:
    rows = db.query_all(
        f"{_FOLLOWUP_SELECT} WHERE f.status NOT IN ({_lit(OPEN_FOLLOWUP_EXCLUDED)}) AND {_before('f.due_at')} "
        "ORDER BY julianday(f.due_at), f.id LIMIT ?",
        (day_end_utc, limit),
    )
    return _rows(rows)


def deadlines(db: Database, day_start_utc: str, horizon_utc: str, limit: int = 100) -> list[dict[str, Any]]:
    """Open deadline-type follow-ups due before the horizon, and closing dates of jobs you care about."""
    open_f = f"f.status NOT IN ({_lit(OPEN_FOLLOWUP_EXCLUDED)})"
    rows = db.query_all(
        "SELECT * FROM (SELECT 'followup' AS kind, f.followup_type AS what, f.due_at AS at, j.employer, j.title, "
        "j.job_url "
        "FROM followups f LEFT JOIN applications a ON a.id = f.application_id "
        "LEFT JOIN jobs j ON j.id = COALESCE(a.job_id, f.job_id) "
        f"WHERE {open_f} AND f.followup_type IN ('deadline', 'assessment_due', 'interview_upcoming') "
        f"AND {_before('f.due_at')} "
        "UNION ALL "
        "SELECT 'closing' AS kind, 'listing closes' AS what, j.closing_at AS at, j.employer, j.title, j.job_url "
        "FROM jobs j LEFT JOIN applications a ON a.job_id = j.id "
        "WHERE j.duplicate_of_job_id IS NULL AND j.listing_status = 'active' AND j.closing_at IS NOT NULL "
        f"AND {_win('j.closing_at')} AND (j.pipeline_state = 'shortlisted' OR (a.id IS NOT NULL AND a.status NOT IN "
        f"({_lit(TERMINAL_APPLICATION_STATES)}) AND NOT ({SUBMITTED_SQL})))) "
        "ORDER BY julianday(at) LIMIT ?",
        (horizon_utc, day_start_utc, horizon_utc, limit),
    )
    return _rows(rows)


def human_action_errors(db: Database, limit: int = 50) -> tuple[list[dict[str, Any]], int]:
    where = "resolved = 0 AND requires_human_action = 1"
    total = _int(db, f"SELECT COUNT(*) FROM errors WHERE {where}")
    rows = db.query_all(
        "SELECT id, occurred_at, component, task_name, job_id, application_id, error_kind, message, human_action "
        f"FROM errors WHERE {where} ORDER BY julianday(occurred_at) DESC, id DESC LIMIT ?",
        (limit,),
    )
    return _rows(rows), total


def failures(db: Database, day_start_utc: str, day_end_utc: str, limit: int = 200) -> list[dict[str, Any]]:
    """Errors recorded in the window plus failed/interrupted runs that have no error row."""
    params = (day_start_utc, day_end_utc)
    rows = db.query_all(
        "SELECT * FROM (SELECT 'error' AS source, e.occurred_at AS at, e.component AS component, "
        "e.task_name AS task_name, "
        "e.error_kind AS kind, e.message AS message, e.human_action AS human_action, e.resolved AS resolved "
        f"FROM errors e WHERE {_win('e.occurred_at')} "
        "UNION ALL "
        "SELECT 'run', r.started_at, 'task:' || r.task_name, r.task_name, COALESCE(r.error_kind, r.status), "
        "COALESCE(r.error_message, 'run ' || r.status), NULL, 0 FROM agent_runs r "
        f"WHERE r.status IN ('failed', 'interrupted') AND {_win('r.started_at')} "
        "AND NOT EXISTS (SELECT 1 FROM errors e2 WHERE e2.run_id = r.id)) "
        "ORDER BY julianday(at) LIMIT ?",
        (*params, *params, limit),
    )
    return _rows(rows)


def activity_log(db: Database, day_start_utc: str, day_end_utc: str, limit: int) -> tuple[list[dict[str, Any]], int]:
    """agent_runs + errors + audit_events of the window, newest first. Returns (rows, total)."""
    params = (day_start_utc, day_end_utc)
    union = (
        "SELECT 'run' AS source, r.started_at AS at, r.task_name AS task, NULL AS entity, "
        "'run (' || r.trigger || ', attempt ' || r.attempt || ')' AS action, r.status AS outcome, "
        "r.error_message AS error, CASE WHEN r.error_kind IN ('authorization_required', "
        "'human_action_required', 'unknown_outcome') THEN 'See errors' END AS human_action, NULL AS details "
        f"FROM agent_runs r WHERE {_win('r.started_at')} "
        "UNION ALL "
        "SELECT 'error', e.occurred_at, COALESCE(e.task_name, e.component), "
        "CASE WHEN e.application_id IS NOT NULL THEN 'application ' || e.application_id "
        "WHEN e.job_id IS NOT NULL THEN 'job ' || e.job_id END, e.component, e.error_kind, e.message, "
        "CASE WHEN e.requires_human_action = 1 AND e.resolved = 0 THEN COALESCE(e.human_action, "
        "'Review this error') END, NULL "
        f"FROM errors e WHERE {_win('e.occurred_at')} "
        "UNION ALL "
        "SELECT 'audit', ae.occurred_at, COALESCE((SELECT r2.task_name FROM agent_runs r2 WHERE r2.id = ae.run_id), "
        "ae.actor), CASE WHEN ae.entity_type IS NOT NULL THEN ae.entity_type || ' ' || COALESCE(ae.entity_id, '') "
        "END, ae.action, ae.outcome, NULL, NULL, ae.details "
        f"FROM audit_events ae WHERE {_win('ae.occurred_at')}"
    )
    total = _int(db, f"SELECT COUNT(*) FROM ({union})", (*params, *params, *params))
    rows = db.query_all(f"SELECT * FROM ({union}) ORDER BY julianday(at) DESC LIMIT ?",
                        (*params, *params, *params, limit))
    return _rows(rows), total


def report_history(db: Database, limit: int = 60) -> list[dict[str, Any]]:
    rows = db.query_all(
        "SELECT id, report_date, xlsx_file, markdown_file, stats, generation_count, first_generated_at, "
        "last_generated_at FROM reports ORDER BY report_date DESC LIMIT ?",
        (limit,),
    )
    out = []
    for r in rows:
        d = dict(r)
        d["stats"] = from_json(d["stats"], {}) or {}
        out.append(d)
    return out
