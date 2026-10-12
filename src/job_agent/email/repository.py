"""Persistence for ``email_messages`` and ``email_associations``.

Only metadata is stored: ids, sender, subject, received time, category,
optional short snippet / summary and a suggested reply draft. Message bodies
are never written here.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from job_agent.database import Database
from job_agent.database.connection import from_json
from job_agent.utils.time import utcnow_iso

SNIPPET_MAX = 200
SUMMARY_MAX = 300


def known_message_ids(db: Database, ids: Iterable[str]) -> set[str]:
    """Which of these Gmail message ids are already stored (checked before fetching)."""
    wanted = [i for i in dict.fromkeys(ids) if i]
    found: set[str] = set()
    for start in range(0, len(wanted), 400):
        chunk = wanted[start:start + 400]
        rows = db.query_all(
            f"SELECT gmail_message_id FROM email_messages WHERE gmail_message_id IN ({','.join('?' for _ in chunk)})",
            chunk,
        )
        found.update(r["gmail_message_id"] for r in rows)
    return found


def insert_message(db: Database, values: dict[str, Any]) -> int | None:
    """Insert one message row; returns ``None`` if the Gmail id already exists."""
    row = dict(values)
    if row.get("snippet"):
        row["snippet"] = str(row["snippet"])[:SNIPPET_MAX]
    if row.get("summary"):
        row["summary"] = str(row["summary"])[:SUMMARY_MAX]
    row.setdefault("processed_at", utcnow_iso())
    cols = list(row)
    cur = db.execute(
        f"INSERT OR IGNORE INTO email_messages ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        [row[c] for c in cols],
    )
    return int(cur.lastrowid) if cur.rowcount == 1 else None


def get_message(db: Database, email_id: int) -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM email_messages WHERE id = ?", (email_id,))


def update_message(db: Database, email_id: int, values: dict[str, Any]) -> None:
    db.update("email_messages", email_id, values)


def list_messages(db: Database, *, attention_only: bool = False, category: str | None = None,
                  limit: int = 50) -> list[sqlite3.Row]:
    """Recent messages with their best association (job title / employer / application uid)."""
    where = []
    params: list[Any] = []
    if attention_only:
        where.append("em.requires_attention = 1")
    if category:
        where.append("em.category = ?")
        params.append(category)
    sql = (
        "SELECT em.*, ea.job_id AS assoc_job_id, ea.application_id AS assoc_application_id, "
        "ea.confidence AS assoc_confidence, ea.method AS assoc_method, ea.confirmed_by_user AS assoc_confirmed, "
        "ea.status_applied AS assoc_status_applied, j.job_uid, j.employer, j.title, a.application_uid "
        "FROM email_messages em "
        "LEFT JOIN email_associations ea ON ea.id = ("
        "  SELECT id FROM email_associations x WHERE x.email_id = em.id "
        "  ORDER BY x.confirmed_by_user DESC, x.confidence DESC, x.id LIMIT 1) "
        "LEFT JOIN jobs j ON j.id = ea.job_id "
        "LEFT JOIN applications a ON a.id = ea.application_id "
        + (f"WHERE {' AND '.join(where)} " if where else "")
        + "ORDER BY COALESCE(em.received_at, em.processed_at) DESC, em.id DESC LIMIT ?"
    )
    params.append(limit)
    return db.query_all(sql, params)


def associations_for(db: Database, email_id: int) -> list[sqlite3.Row]:
    return db.query_all(
        "SELECT ea.*, j.job_uid, j.employer, j.title, a.application_uid, a.status AS application_status "
        "FROM email_associations ea LEFT JOIN jobs j ON j.id = ea.job_id "
        "LEFT JOIN applications a ON a.id = ea.application_id WHERE ea.email_id = ? "
        "ORDER BY ea.confirmed_by_user DESC, ea.confidence DESC, ea.id",
        (email_id,),
    )


def find_association(db: Database, email_id: int, job_id: int | None, application_id: int | None) -> sqlite3.Row | None:
    # ``IS`` so NULL ids compare equal (the UNIQUE constraint does not cover NULLs).
    return db.query_one(
        "SELECT * FROM email_associations WHERE email_id = ? AND job_id IS ? AND application_id IS ?",
        (email_id, job_id, application_id),
    )


def upsert_association(db: Database, *, email_id: int, job_id: int | None, application_id: int | None,
                       method: str, confidence: float, confirmed_by_user: bool = False) -> int:
    existing = find_association(db, email_id, job_id, application_id)
    if existing is not None:
        values: dict[str, Any] = {"method": method, "confidence": max(float(existing["confidence"]), confidence)}
        if confirmed_by_user:
            values.update(confirmed_by_user=1, confidence=1.0, method=method)
        db.update("email_associations", int(existing["id"]), values)
        return int(existing["id"])
    return db.insert(
        "email_associations",
        {
            "email_id": email_id,
            "job_id": job_id,
            "application_id": application_id,
            "method": method,
            "confidence": round(float(confidence), 3),
            "confirmed_by_user": int(confirmed_by_user),
            "created_at": utcnow_iso(),
        },
    )


def thread_associations(db: Database, thread_id: str, *, exclude_email_id: int | None = None) -> list[sqlite3.Row]:
    """Trusted associations (confirmed, or >= 0.8) of earlier messages in the same Gmail thread."""
    return db.query_all(
        "SELECT ea.job_id, ea.application_id, ea.confidence, ea.confirmed_by_user FROM email_associations ea "
        "JOIN email_messages em ON em.id = ea.email_id WHERE em.gmail_thread_id = ? AND em.id IS NOT ? "
        "AND (ea.confirmed_by_user = 1 OR ea.confidence >= 0.8)",
        (thread_id, exclude_email_id),
    )


def association_targets(db: Database) -> list[sqlite3.Row]:
    """Jobs the user is pursuing: every non-closed application, plus shortlisted jobs without one."""
    return db.query_all(
        "SELECT j.id AS job_id, j.job_uid, j.employer, j.title, j.job_url, j.application_url, j.source_name, "
        "j.pipeline_state, a.id AS application_id, a.application_uid, a.status AS application_status "
        "FROM jobs j LEFT JOIN applications a ON a.job_id = j.id "
        "WHERE (a.id IS NOT NULL AND a.status != 'closed') OR (a.id IS NULL AND j.pipeline_state = 'shortlisted')"
    )


def confirmed_sender_domains(db: Database) -> list[sqlite3.Row]:
    """Sender domains of emails you linked to a job yourself, with that job's employer."""
    return db.query_all(
        "SELECT DISTINCT em.from_domain, j.employer FROM email_associations ea "
        "JOIN email_messages em ON em.id = ea.email_id JOIN jobs j ON j.id = ea.job_id "
        "WHERE ea.confirmed_by_user = 1 AND em.from_domain IS NOT NULL"
    )


def last_review_reasons(db: Database, email_id: int) -> list[str]:
    """Why the most recent automatic status update for this email was withheld (empty if none recorded)."""
    row = db.query_one(
        "SELECT details FROM audit_events WHERE action = 'email_status_needs_review' AND entity_type = 'email' "
        "AND entity_id = ? ORDER BY id DESC LIMIT 1",
        (str(email_id),),
    )
    if row is None:
        return []
    details = from_json(row["details"]) or {}
    return [str(r) for r in details.get("reasons") or []]
