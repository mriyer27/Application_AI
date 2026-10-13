"""SQLite connection management with explicit transactions.

The connection runs in autocommit mode (``isolation_level=None``); writes that
must be atomic go through ``Database.transaction()``, which issues
``BEGIN IMMEDIATE`` (taking the write lock up-front, so concurrent runs
serialize instead of deadlocking) and nests via SAVEPOINTs.

A ``Database`` object wraps one connection and must be used from one thread at
a time; create one per thread / per request.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from job_agent.errors import DatabaseError


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, Path):
        return str(obj)
    return str(obj)


def to_json(value: Any) -> str:
    return json.dumps(value, default=_json_default, sort_keys=True, ensure_ascii=False)


def from_json(text: str | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


class Database:
    def __init__(self, path: Path | str, *, busy_timeout_ms: int = 10_000, migrate: bool = True,
                 readonly: bool = False) -> None:
        self.path = Path(path) if str(path) != ":memory:" else path
        self._local_depth = 0
        self._lock = threading.RLock()
        try:
            if readonly:
                uri = f"file:{Path(path).resolve()}?mode=ro"
                self.conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=busy_timeout_ms / 1000,
                                            check_same_thread=False)
            else:
                if isinstance(self.path, Path):
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                self.conn = sqlite3.connect(str(path), isolation_level=None, timeout=busy_timeout_ms / 1000,
                                            check_same_thread=False)
        except sqlite3.Error as exc:
            raise DatabaseError(f"cannot open database {path}: {exc}") from exc
        if not readonly and isinstance(self.path, Path) and os.name == "posix":
            for candidate in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
                try:
                    if candidate.exists():
                        os.chmod(candidate, 0o600)  # holds your application history
                except OSError:
                    pass
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        if not readonly and str(path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")
        if migrate and not readonly:
            from job_agent.database.migrations import apply_migrations

            apply_migrations(self)

    # ------------------------------------------------------------------ #
    @contextmanager
    def transaction(self) -> Iterator["Database"]:
        """Atomic block. Nested calls become SAVEPOINTs."""
        with self._lock:
            if self._local_depth == 0:
                self.conn.execute("BEGIN IMMEDIATE")
                self._local_depth = 1
                try:
                    yield self
                except BaseException:
                    self.conn.execute("ROLLBACK")
                    raise
                else:
                    self.conn.execute("COMMIT")
                finally:
                    self._local_depth = 0
            else:
                name = f"sp_{uuid.uuid4().hex[:8]}"
                self.conn.execute(f"SAVEPOINT {name}")
                self._local_depth += 1
                try:
                    yield self
                except BaseException:
                    self.conn.execute(f"ROLLBACK TO {name}")
                    self.conn.execute(f"RELEASE {name}")
                    raise
                else:
                    self.conn.execute(f"RELEASE {name}")
                finally:
                    self._local_depth -= 1

    @property
    def in_transaction(self) -> bool:
        return self._local_depth > 0

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        try:
            return self.conn.execute(sql, params)
        except sqlite3.IntegrityError:
            raise
        except sqlite3.Error as exc:
            raise DatabaseError(f"database error: {exc}") from exc

    def executemany(self, sql: str, seq: Sequence[Sequence[Any]]) -> sqlite3.Cursor:
        try:
            return self.conn.executemany(sql, seq)
        except sqlite3.IntegrityError:
            raise
        except sqlite3.Error as exc:
            raise DatabaseError(f"database error: {exc}") from exc

    def query_all(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        return self.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Row | None:
        return self.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> Any:
        row = self.query_one(sql, params)
        return None if row is None else row[0]

    def insert(self, table: str, values: dict[str, Any]) -> int:
        _check_identifier(table)
        cols = list(values)
        for c in cols:
            _check_identifier(c)
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
        cur = self.execute(sql, [values[c] for c in cols])
        return int(cur.lastrowid)

    def update(self, table: str, row_id: int, values: dict[str, Any], *, id_column: str = "id") -> int:
        _check_identifier(table)
        _check_identifier(id_column)
        for c in values:
            _check_identifier(c)
        assignments = ", ".join(f"{c} = ?" for c in values)
        cur = self.execute(f"UPDATE {table} SET {assignments} WHERE {id_column} = ?", [*values.values(), row_id])
        return cur.rowcount

    # ------------------------------------------------------------------ #
    def integrity_check(self) -> str:
        return str(self.scalar("PRAGMA integrity_check"))

    def backup(self, dest: Path) -> Path:
        """Online, consistent backup using SQLite's backup API."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(dest))
        try:
            with target:
                self.conn.backup(target)
        finally:
            target.close()
        return dest

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:  # pragma: no cover
            pass

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


_IDENT_OK = set("abcdefghijklmnopqrstuvwxyz_0123456789")


def _check_identifier(name: str) -> None:
    if not name or not set(name) <= _IDENT_OK or name[0].isdigit():
        raise DatabaseError(f"unsafe SQL identifier {name!r}")
