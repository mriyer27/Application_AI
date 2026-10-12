"""Persistence and deduplication of discovered jobs (``jobs``, ``job_changes``, ``job_sources``).

Identity and duplicates
-----------------------
* A listing's identity is ``(source_name, source_identifier, external_id)``;
  its ``job_uid`` is ``"J-"`` + 10 hex chars of a SHA-256 over that triple.
* A *source key* is ``(source_name, source_identifier)``. Different external
  ids from the same source key are always distinct listings (the source says
  so -- e.g. Greenhouse posts one requisition per city).
* Across source keys, a new listing is the *same position* as an existing
  canonical row when (1) its canonical job/application URL matches, (2) the
  employer + requisition id match and the locations are compatible, or (3) the
  employer + title + location fingerprint matches, requisition ids do not
  conflict, and the descriptions are >= 90% similar (token Jaccard).
* Duplicates are **stored** as their own row with ``duplicate_of_job_id`` set,
  ``pipeline_state='filtered_out'`` and ``filter_reason='duplicate of J-...'``,
  and a ``duplicate_detected`` change is recorded on the canonical row. Storing
  them keeps re-runs idempotent (the second listing is recognized by its own
  key next time) and keeps ``ux_jobs_job_url_canonical`` satisfied (that index
  ignores duplicate rows).

All writes happen inside ``db.transaction()`` (``BEGIN IMMEDIATE``), so the
read-then-write logic is serialized against concurrent runs; an
``sqlite3.IntegrityError`` is still handled by re-reading.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Any

from job_agent.database import repo
from job_agent.database.connection import Database, from_json, to_json
from job_agent.discovery.filters import FilterDecision
from job_agent.discovery.normalize import (
    NORMALIZER_VERSION,
    canonicalize_url,
    description_similarity,
    normalize_employer,
    normalize_location,
    normalize_title,
    text_hash,
)
from job_agent.errors import DatabaseError
from job_agent.logging_config import get_logger
from job_agent.models import JobListingStatus, NormalizedJob, PipelineState, can_transition
from job_agent.security.redaction import redact_text
from job_agent.utils.time import parse_iso

log = get_logger(__name__)

DUPLICATE_SIMILARITY = 0.9
_ACTOR = "system"

#: Fields whose change counts as a material change of the listing.
HASH_FIELDS: tuple[str, ...] = (
    "employer", "title", "location", "work_arrangement", "employment_type", "experience_level", "department",
    "requisition_id", "description_text", "required_qualifications", "preferred_qualifications",
    "salary_min", "salary_max", "salary_currency", "salary_period", "salary_is_base", "salary_text",
    "closing_at", "job_url_canonical", "application_url_canonical", "sponsorship_info",
    "work_authorization_requirements",
)


@dataclass
class UpsertOutcome:
    status: str  # new | updated | unchanged | duplicate | deferred
    job_id: int | None = None
    job_uid: str | None = None
    reopened: bool = False
    reposted: bool = False
    duplicate_of: str | None = None
    duplicate_reason: str | None = None
    filtered: bool = False
    filter_codes: tuple[str, ...] = ()
    note: str | None = None


# --------------------------------------------------------------------------- #
# Identity / hashing
# --------------------------------------------------------------------------- #
def job_uid_for(source_name: str, source_identifier: str, external_id: str, *, salt: int = 0) -> str:
    key = f"{source_name}\x1f{source_identifier}\x1f{external_id}"
    if salt:
        key += f"\x1f{salt}"
    return "J-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]


def job_fields(job: NormalizedJob) -> dict[str, Any]:
    """Column values derived from a normalized job (everything except identity/bookkeeping)."""
    return {
        "requisition_id": job.requisition_id,
        "employer": job.employer,
        "employer_normalized": normalize_employer(job.employer),
        "title": job.title,
        "title_normalized": normalize_title(job.title),
        "location": job.location,
        "location_normalized": normalize_location(job.location),
        "work_arrangement": job.work_arrangement.value,
        "employment_type": job.employment_type.value,
        "experience_level": job.experience_level,
        "department": job.department,
        "description_text": job.description_text,
        "description_hash": text_hash(job.description_text),
        "required_qualifications": to_json(job.required_qualifications),
        "preferred_qualifications": to_json(job.preferred_qualifications),
        "salary_min": job.salary_min,
        "salary_max": job.salary_max,
        "salary_currency": job.salary_currency,
        "salary_period": job.salary_period.value,
        "salary_is_base": None if job.salary_is_base is None else int(job.salary_is_base),
        "salary_text": job.salary_text,
        "posted_at": job.posted_at,
        "closing_at": job.closing_at,
        "job_url": job.job_url,
        "job_url_canonical": canonicalize_url(job.job_url),
        "application_url": job.application_url,
        "application_url_canonical": canonicalize_url(job.application_url),
        "sponsorship_info": job.sponsorship_info,
        "work_authorization_requirements": job.work_authorization_requirements,
        "raw_metadata": to_json(job.raw_metadata),
    }


def content_hash(fields: dict[str, Any]) -> str:
    material = {k: fields.get(k) for k in HASH_FIELDS}
    return hashlib.sha256(to_json(material).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# job_sources
# --------------------------------------------------------------------------- #
def ensure_source(db: Database, source_name: str, identifier: str, *, is_mock: bool, now: str) -> int:
    with db.transaction():
        db.execute(
            "INSERT INTO job_sources (source_name, identifier, enabled, is_mock, created_at, updated_at) "
            "VALUES (?, ?, 1, ?, ?, ?) ON CONFLICT(source_name, identifier) DO UPDATE SET "
            "is_mock = excluded.is_mock, enabled = 1, updated_at = excluded.updated_at",
            (source_name, identifier, int(is_mock), now, now),
        )
        return int(db.scalar("SELECT id FROM job_sources WHERE source_name = ? AND identifier = ?",
                             (source_name, identifier)))


def record_source_result(db: Database, source_id: int, *, success: bool, now: str, error: str | None = None) -> None:
    with db.transaction():
        if success:
            db.execute(
                "UPDATE job_sources SET last_run_at = ?, last_success_at = ?, last_error = NULL, "
                "consecutive_failures = 0, updated_at = ? WHERE id = ?",
                (now, now, now, source_id),
            )
        else:
            db.execute(
                "UPDATE job_sources SET last_run_at = ?, last_error = ?, "
                "consecutive_failures = consecutive_failures + 1, updated_at = ? WHERE id = ?",
                (now, redact_text(error or "unknown error", pii=True)[:500], now, source_id),
            )


def list_job_sources(db: Database) -> list[sqlite3.Row]:
    return db.query_all(
        "SELECT s.*, (SELECT COUNT(*) FROM jobs j WHERE j.source_name = s.source_name "
        "AND j.source_identifier = s.identifier AND j.listing_status = 'active') AS active_jobs "
        "FROM job_sources s ORDER BY s.source_name, s.identifier"
    )


def count_active(db: Database, source_name: str, identifier: str) -> int:
    return int(db.scalar(
        "SELECT COUNT(*) FROM jobs WHERE source_name = ? AND source_identifier = ? AND listing_status = 'active'",
        (source_name, identifier),
    ) or 0)


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def get_job_by_key(db: Database, source_name: str, source_identifier: str, external_id: str) -> sqlite3.Row | None:
    return db.query_one(
        "SELECT * FROM jobs WHERE source_name = ? AND source_identifier = ? AND external_id = ?",
        (source_name, source_identifier, external_id),
    )


def get_job_by_uid(db: Database, job_uid: str) -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM jobs WHERE job_uid = ?", (job_uid,))


def job_changes(db: Database, job_id: int) -> list[sqlite3.Row]:
    return db.query_all("SELECT * FROM job_changes WHERE job_id = ? ORDER BY id", (job_id,))


def _record_change(db: Database, job_id: int, change_type: str, *, now: str, summary: str,
                   old_hash: str | None = None, new_hash: str | None = None) -> None:
    db.insert("job_changes", {
        "job_id": job_id, "change_type": change_type, "old_hash": old_hash, "new_hash": new_hash,
        "summary": summary[:1000], "detected_at": now,
    })


def _same_key(row: sqlite3.Row, job: NormalizedJob) -> bool:
    return row["source_name"] == job.source_name and row["source_identifier"] == job.source_identifier


def _locations_compatible(a: str | None, b: str | None) -> bool:
    return a is None or b is None or a == b


def find_duplicate(db: Database, job: NormalizedJob, fields: dict[str, Any]) -> tuple[sqlite3.Row, str] | None:
    """The canonical row this listing duplicates (from a different source key), with the reason."""
    urls = [u for u in (fields["job_url_canonical"], fields["application_url_canonical"]) if u]
    if urls:
        marks = ", ".join("?" for _ in urls)
        rows = db.query_all(
            f"SELECT * FROM jobs WHERE duplicate_of_job_id IS NULL AND (job_url_canonical IN ({marks}) "
            f"OR application_url_canonical IN ({marks})) ORDER BY id",
            (*urls, *urls),
        )
        for row in rows:
            if not _same_key(row, job):
                return row, "same canonical URL"

    if fields["requisition_id"] and fields["employer_normalized"]:
        rows = db.query_all(
            "SELECT * FROM jobs WHERE employer_normalized = ? AND requisition_id = ? AND duplicate_of_job_id IS NULL "
            "AND listing_status = 'active' ORDER BY id",
            (fields["employer_normalized"], fields["requisition_id"]),
        )
        for row in rows:
            if not _same_key(row, job) and _locations_compatible(row["location_normalized"],
                                                                 fields["location_normalized"]):
                return row, f"same employer and requisition id {fields['requisition_id']}"

    rows = db.query_all(
        "SELECT * FROM jobs WHERE employer_normalized = ? AND title_normalized = ? AND location_normalized IS ? "
        "AND duplicate_of_job_id IS NULL AND listing_status = 'active' ORDER BY id",
        (fields["employer_normalized"], fields["title_normalized"], fields["location_normalized"]),
    )
    for row in rows:
        if _same_key(row, job):
            continue
        if row["requisition_id"] and fields["requisition_id"] and row["requisition_id"] != fields["requisition_id"]:
            continue  # different requisitions are different positions
        similarity = description_similarity(row["description_text"], fields["description_text"])
        if similarity >= DUPLICATE_SIMILARITY:
            return row, f"same employer/title/location, description {similarity:.0%} similar"
    return None


def find_repost_origin(db: Database, job: NormalizedJob, fields: dict[str, Any]) -> sqlite3.Row | None:
    """A *closed* listing from the same source key that this new id re-posts (same fingerprint + text)."""
    rows = db.query_all(
        "SELECT * FROM jobs WHERE source_name = ? AND source_identifier = ? AND external_id != ? "
        "AND employer_normalized = ? AND title_normalized = ? AND location_normalized IS ? "
        "AND listing_status != 'active' ORDER BY last_checked_at DESC LIMIT 5",
        (job.source_name, job.source_identifier, job.external_id, fields["employer_normalized"],
         fields["title_normalized"], fields["location_normalized"]),
    )
    for row in rows:
        if row["requisition_id"] and fields["requisition_id"] and row["requisition_id"] != fields["requisition_id"]:
            continue
        if description_similarity(row["description_text"], fields["description_text"]) >= DUPLICATE_SIMILARITY:
            return row
    return None


def _url_taken_by_other(db: Database, url: str | None, job_id: int | None) -> bool:
    if not url:
        return False
    row = db.query_one(
        "SELECT id FROM jobs WHERE job_url_canonical = ? AND duplicate_of_job_id IS NULL AND id IS NOT ?",
        (url, job_id),
    )
    return row is not None


# --------------------------------------------------------------------------- #
# Upsert
# --------------------------------------------------------------------------- #
def upsert_job(db: Database, job: NormalizedJob, *, source_id: int | None, decision: FilterDecision, now: str,
               allow_write: bool = True) -> UpsertOutcome:
    """Insert or update one listing. ``allow_write=False`` (run limit reached) defers new/changed
    listings: unchanged bookkeeping still happens, nothing new is written."""
    fields = job_fields(job)
    new_hash = content_hash(fields)
    with db.transaction():
        existing = get_job_by_key(db, job.source_name, job.source_identifier, job.external_id)
        if existing is not None:
            return _update_existing(db, existing, fields, new_hash, decision, now, allow_write)
        return _insert_new(db, job, fields, new_hash, source_id, decision, now, allow_write)


def _insert_new(db: Database, job: NormalizedJob, fields: dict[str, Any], new_hash: str, source_id: int | None,
                decision: FilterDecision, now: str, allow_write: bool) -> UpsertOutcome:
    dup = find_duplicate(db, job, fields)
    origin = None
    if dup is None:
        if not allow_write:
            return UpsertOutcome(status="deferred")
        origin = find_repost_origin(db, job, fields)
        if _url_taken_by_other(db, fields["job_url_canonical"], None):
            # Another listing from the *same* source key uses this URL, so the URL does
            # not identify the job; keep the raw URL but not the canonical one.
            fields["job_url_canonical"] = None

    values: dict[str, Any] = {
        **fields,
        "job_uid": job_uid_for(job.source_name, job.source_identifier, job.external_id),
        "source_id": source_id,
        "source_name": job.source_name,
        "source_identifier": job.source_identifier,
        "external_id": job.external_id,
        "discovered_at": now,
        "listing_status": JobListingStatus.ACTIVE.value,
        "content_hash": new_hash,
        "times_seen": 1,
        "last_checked_at": now,
        "last_changed_at": now,
        "created_at": now,
        "updated_at": now,
    }
    outcome = UpsertOutcome(status="new")
    if dup is not None:
        canonical, reason = dup
        values.update({
            "pipeline_state": PipelineState.FILTERED_OUT.value,
            "filter_reason": f"duplicate of {canonical['job_uid']} ({reason})",
            "duplicate_of_job_id": canonical["id"],
        })
        outcome = UpsertOutcome(status="duplicate", duplicate_of=canonical["job_uid"], duplicate_reason=reason)
    elif decision.passed:
        values.update({"pipeline_state": PipelineState.DISCOVERED.value, "filter_reason": None})
    else:
        values.update({"pipeline_state": PipelineState.FILTERED_OUT.value, "filter_reason": decision.reason})
        outcome.filtered = True
        outcome.filter_codes = tuple(decision.codes)

    job_id = _insert_with_retry(db, job, values)
    if isinstance(job_id, UpsertOutcome):  # a concurrent run inserted the same listing first
        return job_id
    outcome.job_id = job_id
    outcome.job_uid = values["job_uid"]
    if dup is not None:
        canonical, reason = dup
        _record_change(db, canonical["id"], "duplicate_detected", now=now,
                       summary=f"{job.source_name}:{job.source_identifier} listing {job.external_id} stored as "
                               f"{values['job_uid']} ({reason})")
    elif origin is not None:
        outcome.reposted = True
        _record_change(db, job_id, "repost", now=now, old_hash=origin["content_hash"], new_hash=new_hash,
                       summary=f"repost of {origin['job_uid']} (listing {origin['external_id']}, "
                               f"{origin['listing_status']} since {origin['last_checked_at']})")
    return outcome


def _insert_with_retry(db: Database, job: NormalizedJob, values: dict[str, Any]) -> int | UpsertOutcome:
    for attempt in range(1, 4):
        try:
            with db.transaction():  # savepoint: a failed insert does not abort the outer transaction
                return db.insert("jobs", values)
        except sqlite3.IntegrityError as exc:
            message = str(exc)
            log.info("integrity error inserting %s:%s:%s (%s); re-reading", job.source_name,
                     job.source_identifier, job.external_id, message)
            existing = get_job_by_key(db, job.source_name, job.source_identifier, job.external_id)
            if existing is not None:
                return UpsertOutcome(status="unchanged", job_id=existing["id"], job_uid=existing["job_uid"],
                                     note="inserted concurrently by another run")
            if "job_uid" in message:
                values["job_uid"] = job_uid_for(job.source_name, job.source_identifier, job.external_id,
                                                salt=attempt)
            elif "job_url_canonical" in message:
                values["job_url_canonical"] = None
            else:
                raise DatabaseError(f"cannot store job {job.source_name}:{job.external_id}: {message}") from exc
    raise DatabaseError(f"cannot store job {job.source_name}:{job.external_id} after retries")


def _changed_fields(row: sqlite3.Row, fields: dict[str, Any]) -> list[str]:
    changed = []
    for name in HASH_FIELDS:
        old, new = row[name], fields.get(name)
        if name in {"required_qualifications", "preferred_qualifications"}:
            old = from_json(old, [])
            new = from_json(new, [])
        if old != new:
            if name in {"title", "location", "salary_min", "salary_max", "closing_at"}:
                changed.append(f"{name}: {old!r} -> {new!r}"[:160])
            else:
                changed.append("description" if name == "description_text" else name)
    return changed


def _update_existing(db: Database, row: sqlite3.Row, fields: dict[str, Any], new_hash: str,
                     decision: FilterDecision, now: str, allow_write: bool) -> UpsertOutcome:
    outcome = UpsertOutcome(status="unchanged", job_id=row["id"], job_uid=row["job_uid"])
    updates: dict[str, Any] = {"last_checked_at": now, "times_seen": int(row["times_seen"]) + 1, "updated_at": now}
    if row["listing_status"] != JobListingStatus.ACTIVE.value:
        updates["listing_status"] = JobListingStatus.ACTIVE.value
        outcome.reopened = True
        _record_change(db, row["id"], "reopened", now=now,
                       summary=f"listing returned by the source again (was {row['listing_status']})")

    if new_hash != row["content_hash"] and not allow_write:
        db.update("jobs", row["id"], updates)
        outcome.status = "deferred"
        return outcome

    if new_hash == row["content_hash"]:
        if fields["posted_at"] and fields["posted_at"] != row["posted_at"]:
            updates["posted_at"] = fields["posted_at"]
            old_posted, new_posted = parse_iso(row["posted_at"]), parse_iso(fields["posted_at"])
            source = from_json(fields["raw_metadata"], {}).get("posted_at_source")
            if old_posted and new_posted and new_posted > old_posted and source not in (None, "updated_at"):
                outcome.reposted = True
                _record_change(db, row["id"], "repost", now=now, old_hash=row["content_hash"], new_hash=new_hash,
                               summary=f"posting date moved from {row['posted_at']} to {fields['posted_at']} "
                                       "with unchanged content")
        db.update("jobs", row["id"], updates)
        return outcome

    # Material change.
    old_meta = from_json(row["raw_metadata"], {})
    upgrade_only = old_meta.get("normalizer_version") != NORMALIZER_VERSION
    changed = _changed_fields(row, fields)
    values = {**fields, **updates, "content_hash": new_hash, "last_changed_at": now}
    if row["duplicate_of_job_id"] is None and _url_taken_by_other(db, fields["job_url_canonical"], row["id"]):
        values["job_url_canonical"] = None
    db.update("jobs", row["id"], values)
    outcome.status = "updated"
    _apply_filter_state(db, row, decision, outcome)
    summary = "changed: " + ", ".join(changed) if changed else "content changed"
    if upgrade_only:
        summary = None  # fields re-derived by a newer normalizer; not a change of the listing itself
    if outcome.note:
        summary = f"{summary}. {outcome.note}" if summary else outcome.note
    if summary:
        _record_change(db, row["id"], "material_change", now=now, old_hash=row["content_hash"], new_hash=new_hash,
                       summary=summary)
    return outcome


def _apply_filter_state(db: Database, row: sqlite3.Row, decision: FilterDecision, outcome: UpsertOutcome) -> None:
    """After a material change, re-apply filters within what the job state machine allows.

    The pipeline state only moves through ``repo.set_job_state``; transitions the
    state machine does not allow are left alone (the scorer re-scores changed
    listings anyway because their ``content_hash`` changed).
    """
    if row["duplicate_of_job_id"] is not None:
        return
    current = PipelineState(row["pipeline_state"])
    has_application = db.scalar("SELECT 1 FROM applications WHERE job_id = ?", (row["id"],)) is not None
    if not decision.passed:
        outcome.filtered = True
        outcome.filter_codes = tuple(decision.codes)
        if current == PipelineState.FILTERED_OUT:
            db.execute("UPDATE jobs SET filter_reason = ? WHERE id = ?", (decision.reason, row["id"]))
        elif has_application or row["user_relevance"] == "relevant":
            outcome.note = f"now fails filters ({decision.reason}) but kept {current.value}: " + (
                "an application exists" if has_application else "you marked it relevant")
        elif can_transition(current, PipelineState.FILTERED_OUT):
            repo.set_job_state(db, row["id"], PipelineState.FILTERED_OUT, actor=_ACTOR, reason=decision.reason)
        return
    if current != PipelineState.DISCOVERED and can_transition(current, PipelineState.DISCOVERED):
        repo.set_job_state(db, row["id"], PipelineState.DISCOVERED, actor=_ACTOR, reason="listing changed")
    elif current == PipelineState.FILTERED_OUT:
        outcome.note = ("listing now passes the search filters, but filtered_out -> discovered is not an "
                        "allowed transition; state left as filtered_out")


# --------------------------------------------------------------------------- #
# Closures
# --------------------------------------------------------------------------- #
def close_missing_jobs(db: Database, source_name: str, identifier: str, seen_external_ids: set[str],
                       now: str) -> list[str]:
    """After a complete fetch: active listings of this source key that were not returned are closed."""
    closed: list[str] = []
    with db.transaction():
        rows = db.query_all(
            "SELECT id, job_uid, external_id FROM jobs WHERE source_name = ? AND source_identifier = ? "
            "AND listing_status = 'active'",
            (source_name, identifier),
        )
        for row in rows:
            if row["external_id"] in seen_external_ids:
                continue
            db.execute("UPDATE jobs SET listing_status = ?, updated_at = ? WHERE id = ?",
                       (JobListingStatus.CLOSED.value, now, row["id"]))
            _record_change(db, row["id"], "closed", now=now,
                           summary=f"not returned by {source_name}:{identifier} in a complete fetch")
            closed.append(row["job_uid"])
    return closed
