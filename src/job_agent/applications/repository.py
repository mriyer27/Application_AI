"""SQL owned by the applications module (answers, lookups, timelines).

Status changes and approvals always go through ``database.repo``; this module
only reads them and manages ``application_answers`` plus a few descriptive
application columns (``prepared_at``, ``application_url``, ``next_action``,
``notes``).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from job_agent.applications.field_mapping import MappedAnswer
from job_agent.database import Database
from job_agent.database.connection import from_json
from job_agent.errors import JobAgentError
from job_agent.utils.time import utcnow_iso


class NotFound(JobAgentError):
    """Unknown job / application / approval reference."""


def resolve_job(db: Database, ref: str | int) -> sqlite3.Row:
    text = str(ref).strip()
    row = db.query_one("SELECT * FROM jobs WHERE job_uid = ?", (text,))
    if row is None and text.isdigit():
        row = db.query_one("SELECT * FROM jobs WHERE id = ?", (int(text),))
    if row is None:
        app = db.query_one("SELECT job_id FROM applications WHERE application_uid = ?", (text,))
        if app is not None:
            row = db.query_one("SELECT * FROM jobs WHERE id = ?", (app["job_id"],))
    if row is None:
        raise NotFound(f"no job {text!r}", human_action="List jobs with `job-agent jobs` (use the J-... id).")
    return row


def resolve_application(db: Database, ref: str | int) -> sqlite3.Row:
    text = str(ref).strip()
    row = db.query_one("SELECT * FROM applications WHERE application_uid = ?", (text,))
    if row is None and text.isdigit():
        row = db.query_one("SELECT * FROM applications WHERE id = ?", (int(text),))
    if row is None:
        row = db.query_one("SELECT a.* FROM applications a JOIN jobs j ON j.id = a.job_id WHERE j.job_uid = ?",
                           (text,))
    if row is None:
        raise NotFound(f"no application {text!r}",
                       human_action="List applications with `job-agent applications` (use the A-... id).")
    return row


def get_job(db: Database, job_id: int) -> sqlite3.Row:
    row = db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if row is None:
        raise NotFound(f"job {job_id} does not exist")
    return row


# --------------------------------------------------------------------------- #
# Answers
# --------------------------------------------------------------------------- #
def get_answers(db: Database, application_id: int) -> list[sqlite3.Row]:
    return db.query_all("SELECT * FROM application_answers WHERE application_id = ? ORDER BY id", (application_id,))


def get_answer(db: Database, application_id: int, field_ref: str) -> sqlite3.Row | None:
    """By exact field key, or ``#n`` (1-based position as listed in the approval view)."""
    ref = field_ref.strip()
    if ref.startswith("#") and ref[1:].isdigit():
        rows = get_answers(db, application_id)
        n = int(ref[1:])
        return rows[n - 1] if 1 <= n <= len(rows) else None
    return db.query_one("SELECT * FROM application_answers WHERE application_id = ? AND field_key = ?",
                        (application_id, ref))


def user_answers(db: Database, application_id: int) -> dict[str, dict[str, Any]]:
    rows = db.query_all("SELECT * FROM application_answers WHERE application_id = ? AND answer_source = 'user'",
                        (application_id,))
    return {r["field_key"]: dict(r) for r in rows}


def save_mapped_answers(db: Database, application_id: int, mapped: list[MappedAnswer]) -> None:
    """Replace the prepared answers with the latest form's fields (keeps row ids of unchanged keys)."""
    now = utcnow_iso()
    keys = [m.key for m in mapped]
    with db.transaction():
        if keys:
            placeholders = ",".join("?" for _ in keys)
            db.execute(f"DELETE FROM application_answers WHERE application_id = ? AND field_key NOT IN ({placeholders})",
                       (application_id, *keys))
        else:
            db.execute("DELETE FROM application_answers WHERE application_id = ?", (application_id,))
        for m in mapped:
            db.execute(
                "INSERT INTO application_answers (application_id, field_key, field_label, field_help_text, field_type,"
                " required, answer, answer_source, confidence, requires_review, is_legal_declaration, review_reason,"
                " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(application_id, field_key) DO UPDATE SET field_label = excluded.field_label,"
                " field_help_text = excluded.field_help_text,"
                " field_type = excluded.field_type, required = excluded.required, answer = excluded.answer,"
                " answer_source = excluded.answer_source, confidence = excluded.confidence,"
                " requires_review = excluded.requires_review, is_legal_declaration = excluded.is_legal_declaration,"
                " review_reason = excluded.review_reason, updated_at = excluded.updated_at",
                (application_id, m.key, m.field.label, getattr(m.field, "help_text", None) or None, m.field.type,
                 int(m.field.required), m.answer, m.source,
                 m.confidence, int(m.requires_review), int(m.is_legal), m.review_reason, now),
            )


def mark_fill_failed(db: Database, application_id: int, field_key: str, reason: str) -> None:
    db.execute("UPDATE application_answers SET requires_review = 1, review_reason = ?, updated_at = ? "
               "WHERE application_id = ? AND field_key = ?",
               (reason[:500], utcnow_iso(), application_id, field_key))


def set_user_answer(db: Database, answer_id: int, value: str, *, is_legal: bool) -> None:
    db.execute(
        "UPDATE application_answers SET answer = ?, answer_source = 'user', confidence = 'verified', "
        "requires_review = ?, review_reason = ?, updated_at = ? WHERE id = ?",
        (value, int(is_legal),
         "legal/eligibility declaration answered by you; confirm it in the approval" if is_legal else
         ("left blank by you" if value == "" else "answered by you"),
         utcnow_iso(), answer_id),
    )


# --------------------------------------------------------------------------- #
# Events / approvals / documents
# --------------------------------------------------------------------------- #
def latest_event(db: Database, application_id: int, event_type: str) -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM application_events WHERE application_id = ? AND event_type = ? "
                        "ORDER BY id DESC LIMIT 1", (application_id, event_type))


def latest_event_details(db: Database, application_id: int, event_type: str) -> dict[str, Any]:
    row = latest_event(db, application_id, event_type)
    return from_json(row["details"], {}) if row else {}


def events(db: Database, application_id: int) -> list[dict[str, Any]]:
    rows = db.query_all("SELECT * FROM application_events WHERE application_id = ? ORDER BY id", (application_id,))
    out = []
    for r in rows:
        item = dict(r)
        item["details"] = from_json(r["details"], {})
        item["evidence"] = from_json(r["evidence"], None)
        out.append(item)
    return out


def approvals_for(db: Database, application_id: int) -> list[sqlite3.Row]:
    return db.query_all("SELECT * FROM approval_requests WHERE application_id = ? ORDER BY id", (application_id,))


def live_approvals(db: Database, application_id: int, request_type: str) -> list[sqlite3.Row]:
    return db.query_all("SELECT * FROM approval_requests WHERE application_id = ? AND request_type = ? "
                        "AND status IN ('pending','approved') ORDER BY id", (application_id, request_type))


def update_application(db: Database, application_id: int, values: dict[str, Any]) -> None:
    allowed = {"prepared_at", "application_url", "next_action", "notes"}
    bad = set(values) - allowed
    if bad:
        raise JobAgentError(f"applications module may not update {sorted(bad)} directly")
    now = utcnow_iso()
    db.update("applications", application_id, {**values, "updated_at": now, "last_activity_at": now})


def list_applications(db: Database, status: str | None = None) -> list[dict[str, Any]]:
    sql = ("SELECT a.*, j.job_uid, j.employer, j.title, j.location FROM applications a "
           "JOIN jobs j ON j.id = a.job_id")
    params: tuple[Any, ...] = ()
    if status:
        sql += " WHERE a.status = ?"
        params = (status,)
    sql += " ORDER BY a.last_activity_at DESC, a.id DESC"
    return [dict(r) for r in db.query_all(sql, params)]


def unknown_outcome_attempts(db: Database, application_id: int) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM application_events WHERE application_id = ? "
                         "AND event_type = 'submission_outcome_unknown'", (application_id,)) or 0)
