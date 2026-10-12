"""Cache of validated local-LLM outputs (``llm_analyses`` table).

Rows are idempotent by ``UNIQUE(purpose, model, prompt_version, input_hash)``:
the same input analyzed by the same model with the same prompt version is
never sent to the model twice. Only *validated* output is stored.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from pydantic import BaseModel, ValidationError

from job_agent.database.connection import Database, from_json, to_json
from job_agent.logging_config import get_logger
from job_agent.models import JobAnalysis
from job_agent.utils.time import utcnow_iso

log = get_logger(__name__)

PURPOSE_JOB_ANALYSIS = "job_analysis"


def input_hash(*parts: str) -> str:
    """Stable hash of the exact inputs given to the model (order matters)."""
    h = hashlib.sha256()
    for part in parts:
        data = part.encode("utf-8")
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()


def get_cached(db: Database, *, purpose: str, model: str, prompt_version: str,
               input_hash: str) -> dict[str, Any] | None:
    """The cached validated output (as a dict), or ``None``."""
    row = db.query_one(
        "SELECT output FROM llm_analyses WHERE purpose = ? AND model = ? AND prompt_version = ? AND input_hash = ?",
        (purpose, model, prompt_version, input_hash),
    )
    if row is None:
        return None
    value = from_json(row["output"])
    return value if isinstance(value, dict) else None


def save(db: Database, *, purpose: str, model: str, prompt_version: str, input_hash: str,
         output: BaseModel | dict[str, Any], job_id: int | None = None, duration_ms: int | None = None) -> int:
    """Insert or refresh a cached output. Returns the row id."""
    payload = output.model_dump(mode="json") if isinstance(output, BaseModel) else output
    with db.transaction():
        db.execute(
            "INSERT INTO llm_analyses (job_id, purpose, model, prompt_version, input_hash, output, duration_ms,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(purpose, model, prompt_version, input_hash) DO UPDATE SET"
            " output = excluded.output, duration_ms = excluded.duration_ms, created_at = excluded.created_at,"
            " job_id = COALESCE(excluded.job_id, llm_analyses.job_id)",
            (job_id, purpose, model, prompt_version, input_hash, to_json(payload), duration_ms, utcnow_iso()),
        )
        row_id = db.scalar(
            "SELECT id FROM llm_analyses WHERE purpose = ? AND model = ? AND prompt_version = ? AND input_hash = ?",
            (purpose, model, prompt_version, input_hash),
        )
    return int(row_id)


def get_cached_analysis(db: Database, *, model: str, prompt_version: str, input_hash: str) -> JobAnalysis | None:
    data = get_cached(db, purpose=PURPOSE_JOB_ANALYSIS, model=model, prompt_version=prompt_version,
                      input_hash=input_hash)
    return _as_analysis(data)


def latest_analysis_for_job(db: Database, job_id: int) -> JobAnalysis | None:
    """Most recent cached job analysis for a job (any model / prompt version)."""
    row: sqlite3.Row | None = db.query_one(
        "SELECT output FROM llm_analyses WHERE job_id = ? AND purpose = ? ORDER BY created_at DESC, id DESC LIMIT 1",
        (job_id, PURPOSE_JOB_ANALYSIS),
    )
    return _as_analysis(from_json(row["output"]) if row else None)


def _as_analysis(data: Any) -> JobAnalysis | None:
    if not isinstance(data, dict):
        return None
    try:
        return JobAnalysis.model_validate(data)
    except ValidationError:
        # Stored by an older, incompatible version; treat as a cache miss.
        log.info("ignoring cached job analysis that no longer validates")
        return None
