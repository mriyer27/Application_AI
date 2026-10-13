"""Versioned schema migrations.

Migrations are SQL files in ``database/sql`` named ``NNN_description.sql``.
Each is applied exactly once, inside a transaction, and recorded in
``schema_migrations``. Never edit an applied migration; add a new file.
"""

from __future__ import annotations

import hashlib
import re
from importlib import resources
from typing import TYPE_CHECKING

from job_agent.errors import DatabaseError
from job_agent.utils.time import utcnow_iso

if TYPE_CHECKING:  # pragma: no cover
    from job_agent.database.connection import Database

_NAME_RE = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")


def available_migrations() -> list[tuple[int, str, str]]:
    files = resources.files("job_agent.database").joinpath("sql")
    found = []
    for entry in files.iterdir():
        m = _NAME_RE.match(entry.name)
        if m:
            found.append((int(m.group(1)), m.group(2), entry.read_text(encoding="utf-8")))
    found.sort()
    versions = [v for v, _, _ in found]
    if versions != list(range(1, len(versions) + 1)):
        raise DatabaseError(f"migration versions must be contiguous from 001, found {versions}")
    return found


def current_version(db: "Database") -> int:
    db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    return int(db.scalar("SELECT COALESCE(MAX(version), 0) FROM schema_migrations") or 0)


def apply_migrations(db: "Database") -> list[int]:
    applied: list[int] = []
    current = current_version(db)
    migrations = available_migrations()
    if current > len(migrations):
        raise DatabaseError(
            f"database schema version {current} is newer than this code supports ({len(migrations)}); "
            "upgrade job-agent before using this database"
        )
    for version, name, sql in migrations:
        if version <= current:
            continue
        checksum = hashlib.sha256(sql.encode()).hexdigest()
        # executescript() commits implicitly, so wrap statements manually.
        statements = [s.strip() for s in _split_sql(sql) if s.strip()]
        with db.transaction():
            for stmt in statements:
                db.execute(stmt)
            db.execute(
                "INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                (version, name, checksum, utcnow_iso()),
            )
        applied.append(version)
    return applied


def _split_sql(sql: str) -> list[str]:
    """Split a migration into statements.

    Our migrations never put ``--`` or ``;`` inside string literals and use no
    triggers, so stripping comments and splitting on ``;`` is safe.
    """
    without_comments = re.sub(r"--[^\n]*", "", sql)
    return without_comments.split(";")
