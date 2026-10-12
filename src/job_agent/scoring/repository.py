"""Persistence and queries for job scores (``job_scores``) and job annotations.

Return values are plain dicts (JSON columns decoded) so the CLI, reporting and
the dashboard can reuse them without importing models.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

from job_agent.database import repo
from job_agent.database.connection import Database, from_json, to_json
from job_agent.errors import PermanentError
from job_agent.logging_config import get_logger
from job_agent.models import JOB_STATES, MatchCategory, PipelineState, ScoreResult, can_transition
from job_agent.utils.time import utcnow_iso

log = get_logger(__name__)

_SCORE_JSON = ("factors", "matching_qualifications", "missing_qualifications", "potential_disqualifiers",
               "eligibility_flags", "evidence", "reasons_to_apply", "reasons_not_to_apply")
_JOB_JSON = {"required_qualifications": [], "preferred_qualifications": [], "raw_metadata": {}}
_UNIQUE_KEY = ("job_id", "scoring_version", "profile_hash", "config_hash", "content_hash", "used_llm")
RELEVANCE_VALUES = ("relevant", "irrelevant")


@dataclass(frozen=True)
class SavedScore:
    score_id: int
    created: bool  # False when an identical (UNIQUE) score row already existed and was refreshed
    pipeline_state: str  # job state after saving
    state_changed: bool
    state_note: str | None = None  # why the state was left alone, if it was


# --------------------------------------------------------------------------- #
# Decoding helpers
# --------------------------------------------------------------------------- #
def _decode_job(row: Mapping[str, Any], *, light: bool = False) -> dict[str, Any]:
    job = dict(row)
    for key, default in _JOB_JSON.items():
        if key in job:
            job[key] = from_json(job[key], default) if isinstance(job[key], str) else (job[key] or default)
    if "salary_is_base" in job and job["salary_is_base"] is not None:
        job["salary_is_base"] = bool(job["salary_is_base"])
    if light:
        job.pop("description_text", None)
        job.pop("raw_metadata", None)
    return job


def decode_score(row: Mapping[str, Any]) -> dict[str, Any]:
    score = dict(row)
    for key in _SCORE_JSON:
        if key in score:
            score[key] = from_json(score[key], [])
    for key in ("shortlisted", "used_llm", "is_current"):
        if key in score and score[key] is not None:
            score[key] = bool(score[key])
    return score


# --------------------------------------------------------------------------- #
# Saving
# --------------------------------------------------------------------------- #
def save_score(db: Database, job_row: Mapping[str, Any], result: ScoreResult, profile_hash: str,
               config_hash: str, *, allow_filtered: bool = False, actor: str = "system") -> SavedScore:
    """Store ``result`` as the job's current score and update the job's pipeline state.

    * idempotent: the UNIQUE (job, version, profile, config, content, used_llm)
      row is refreshed instead of duplicated; other rows lose ``is_current``;
    * the job moves to SHORTLISTED / SCORED via ``repo.set_job_state`` (through
      SCORED when the state machine requires it);
    * jobs with an application, closed jobs and (unless ``allow_filtered``)
      filtered-out jobs keep their state.

    ``content_hash`` comes from ``job_row`` -- the content the score was
    computed from -- so a concurrent discovery update is detected as stale later.
    """
    job_id = int(job_row["id"])
    if result.job_id != job_id:
        raise PermanentError(f"score is for job {result.job_id}, not job {job_id}")
    content_hash = str(job_row["content_hash"])
    key = (job_id, result.scoring_version, profile_hash, config_hash, content_hash, int(result.used_llm))
    values: dict[str, Any] = {
        "overall_score": float(result.overall_score),
        "category": result.category.value,
        "shortlisted": int(result.shortlisted),
        "factors": to_json([f.model_dump(mode="json") for f in result.factors]),
        "matching_qualifications": to_json(result.matching_qualifications),
        "missing_qualifications": to_json(result.missing_qualifications),
        "potential_disqualifiers": to_json(result.potential_disqualifiers),
        "eligibility_flags": to_json([f.model_dump(mode="json") for f in result.eligibility_flags]),
        "evidence": to_json(result.evidence),
        "reasons_to_apply": to_json(result.reasons_to_apply),
        "reasons_not_to_apply": to_json(result.reasons_not_to_apply),
        "recommended_next_action": result.recommended_next_action,
        "llm_model": result.llm_model,
        "is_current": 1,
        "scored_at": utcnow_iso(),
    }
    where = " AND ".join(f"{c} = ?" for c in _UNIQUE_KEY)
    with db.transaction():
        existing = db.scalar(f"SELECT id FROM job_scores WHERE {where}", key)
        if existing is None:
            score_id = db.insert("job_scores", {**dict(zip(_UNIQUE_KEY, key)), **values})
            created = True
        else:
            score_id = int(existing)
            db.update("job_scores", score_id, values)
            created = False
        db.execute("UPDATE job_scores SET is_current = 0 WHERE job_id = ? AND id != ? AND is_current = 1",
                    (job_id, score_id))
        state, changed, note = _apply_state(db, job_id, result, allow_filtered=allow_filtered, actor=actor)
    return SavedScore(score_id, created, state, changed, note)


def _apply_state(db: Database, job_id: int, result: ScoreResult, *, allow_filtered: bool,
                 actor: str) -> tuple[str, bool, str | None]:
    row = db.query_one("SELECT pipeline_state, listing_status FROM jobs WHERE id = ?", (job_id,))
    if row is None:
        raise PermanentError(f"job {job_id} does not exist")
    current = PipelineState(row["pipeline_state"])
    if repo.get_application_for_job(db, job_id) is not None:
        return current.value, False, "job has an application; its state is managed by the application"
    if current not in JOB_STATES or current == PipelineState.CLOSED:
        return current.value, False, f"job is {current.value}"
    if current == PipelineState.FILTERED_OUT and not allow_filtered:
        return current.value, False, "job was filtered out; rescore it explicitly to change that"
    active = row["listing_status"] == "active"
    target = PipelineState.SHORTLISTED if result.shortlisted and active else PipelineState.SCORED
    if current == target == PipelineState.SHORTLISTED:
        return current.value, False, None
    if can_transition(current, target):
        repo.set_job_state(db, job_id, target, actor=actor)
    elif can_transition(current, PipelineState.SCORED) and can_transition(PipelineState.SCORED, target):
        repo.set_job_state(db, job_id, PipelineState.SCORED, actor=actor)
        repo.set_job_state(db, job_id, target, actor=actor)
    else:  # pragma: no cover - every JOB_STATE except CLOSED can reach SCORED
        return current.value, False, f"{current.value} -> {target.value} is not an allowed transition"
    return target.value, target != current, None


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #
def current_score_row(db: Database, job_id: int) -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM job_scores WHERE job_id = ? AND is_current = 1 ORDER BY id DESC LIMIT 1",
                        (job_id,))


def current_score(db: Database, job_id: int) -> dict[str, Any] | None:
    row = current_score_row(db, job_id)
    return decode_score(row) if row is not None else None


def current_score_keys(db: Database) -> dict[int, sqlite3.Row]:
    """job_id -> (scoring_version, profile_hash, config_hash, content_hash, used_llm) of its current score."""
    rows = db.query_all(
        "SELECT id, job_id, scoring_version, profile_hash, config_hash, content_hash, used_llm, overall_score "
        "FROM job_scores WHERE is_current = 1 ORDER BY id"
    )
    return {int(r["job_id"]): r for r in rows}


def score_history(db: Database, job_id: int, limit: int = 20) -> list[dict[str, Any]]:
    rows = db.query_all(
        "SELECT id, scoring_version, overall_score, category, shortlisted, used_llm, llm_model, is_current, scored_at "
        "FROM job_scores WHERE job_id = ? ORDER BY id DESC LIMIT ?", (job_id, limit))
    return [decode_score(r) for r in rows]


_TOP_SQL = """
SELECT j.*,
       s.id AS score_id, s.overall_score, s.category, s.shortlisted, s.factors, s.matching_qualifications,
       s.missing_qualifications, s.potential_disqualifiers, s.eligibility_flags, s.evidence, s.reasons_to_apply,
       s.reasons_not_to_apply, s.recommended_next_action, s.scoring_version, s.used_llm, s.llm_model, s.scored_at,
       s.profile_hash, s.config_hash, s.content_hash AS score_content_hash,
       a.application_uid, a.status AS application_status
FROM jobs j
JOIN job_scores s ON s.job_id = j.id AND s.is_current = 1
LEFT JOIN applications a ON a.job_id = j.id
WHERE (:category IS NULL OR s.category = :category)
  AND (:include_filtered = 1 OR (j.pipeline_state != 'filtered_out' AND j.duplicate_of_job_id IS NULL
       AND (j.user_relevance IS NULL OR j.user_relevance != 'irrelevant')))
  AND (:include_closed = 1 OR (j.listing_status = 'active' AND j.pipeline_state != 'closed'))
ORDER BY s.overall_score DESC, j.discovered_at DESC, j.id ASC
LIMIT :limit
"""


def top_jobs(db: Database, limit: int = 20, category: str | MatchCategory | None = None,
             include_filtered: bool = False, include_closed: bool = False) -> list[dict[str, Any]]:
    """Highest-scoring jobs with a current score, best first.

    Each item is the job row (without ``description_text`` / ``raw_metadata``)
    plus the current score's fields (JSON decoded; score id as ``score_id``)
    and ``application_uid`` / ``application_status`` when an application exists.
    By default filtered-out, duplicate and user-marked-irrelevant jobs
    (``include_filtered``) and closed listings (``include_closed``) are excluded.
    """
    cat = None
    if category is not None:
        try:
            cat = MatchCategory(category).value
        except ValueError:
            raise PermanentError(
                f"unknown category {category!r}; use one of {', '.join(c.value for c in MatchCategory)}") from None
    rows = db.query_all(_TOP_SQL, {"category": cat, "include_filtered": int(include_filtered),
                                   "include_closed": int(include_closed), "limit": max(0, int(limit))})
    return [decode_score(_decode_job(r, light=True)) for r in rows]


def find_job(db: Database, ref: int | str) -> sqlite3.Row | None:
    """Look a job up by numeric id or by job_uid (e.g. ``J-1a2b3c4d5e``)."""
    if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
        return db.query_one("SELECT * FROM jobs WHERE id = ?", (int(ref),))
    text = str(ref).strip()
    row = db.query_one("SELECT * FROM jobs WHERE job_uid = ?", (text,))
    if row is None:
        row = db.query_one("SELECT * FROM jobs WHERE lower(job_uid) = lower(?)", (text,))
    return row


def job_detail(db: Database, ref: int | str) -> dict[str, Any] | None:
    """``{"job": ..., "score": ... | None, "application": ... | None}`` or None if the job does not exist."""
    row = find_job(db, ref)
    if row is None:
        return None
    job = _decode_job(row)
    app_row = repo.get_application_for_job(db, int(row["id"]))
    application = None
    if app_row is not None:
        application = dict(app_row)
        application["confirmation_evidence"] = from_json(application.get("confirmation_evidence"), {})
    return {"job": job, "score": current_score(db, int(row["id"])), "application": application}


# --------------------------------------------------------------------------- #
# User annotations
# --------------------------------------------------------------------------- #
def _require_job(db: Database, ref: int | str) -> sqlite3.Row:
    row = find_job(db, ref)
    if row is None:
        raise PermanentError(f"job {ref} not found", human_action="Check the id with `job-agent jobs`.")
    return row


def set_user_relevance(db: Database, ref: int | str, relevance: str | None, *, actor: str = "cli") -> dict[str, Any]:
    """Record the user's relevant/irrelevant judgement (None clears it). Audited."""
    if relevance is not None and relevance not in RELEVANCE_VALUES:
        raise PermanentError(f"relevance must be one of {RELEVANCE_VALUES} or None, not {relevance!r}")
    with db.transaction():
        row = _require_job(db, ref)
        db.update("jobs", int(row["id"]), {"user_relevance": relevance, "updated_at": utcnow_iso()})
        repo.audit(db, actor=actor, action="job_relevance_marked", entity_type="job", entity_id=row["job_uid"],
                   outcome="success", details={"relevance": relevance, "previous": row["user_relevance"]})
        updated = db.query_one("SELECT * FROM jobs WHERE id = ?", (int(row["id"]),))
    return _decode_job(updated)  # type: ignore[arg-type]


def set_user_note(db: Database, ref: int | str, note: str | None, *, actor: str = "cli",
                  append: bool = False) -> dict[str, Any]:
    """Replace (or append to) the user's free-text note on a job. Audited (length only, not content)."""
    text = (note or "").strip()
    with db.transaction():
        row = _require_job(db, ref)
        if append and row["user_notes"]:
            text = f"{row['user_notes']}\n{text}" if text else row["user_notes"]
        db.update("jobs", int(row["id"]), {"user_notes": text or None, "updated_at": utcnow_iso()})
        repo.audit(db, actor=actor, action="job_note_updated", entity_type="job", entity_id=row["job_uid"],
                   outcome="success", details={"length": len(text), "append": append})
        updated = db.query_one("SELECT * FROM jobs WHERE id = ?", (int(row["id"]),))
    return _decode_job(updated)  # type: ignore[arg-type]
