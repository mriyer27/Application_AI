"""Document files and ``document_versions`` rows.

Files live in ``config.documents_dir / <job_uid> /`` (git-ignored) and are named
with ``security.sanitize.document_filename``. A file is never overwritten: each
generation gets a new version number (shared by its docx/pdf/md files) and files
are created with exclusive-create mode.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from job_agent.database import repo
from job_agent.database.connection import Database, from_json, to_json
from job_agent.errors import PermanentError, PermissionDenied
from job_agent.models import DocumentStatus, DocumentType
from job_agent.resume.models import Claim, DocumentInfo, DocumentRecord, Issue, JobListing
from job_agent.security.sanitize import document_filename, ensure_within, safe_component
from job_agent.utils.time import utcnow_iso

if TYPE_CHECKING:  # pragma: no cover
    from job_agent.context import AppContext

#: Preferred format for the application's attachment (PDF first).
ATTACHMENT_PREFERENCE = ("pdf", "docx", "md")
KIND_LABEL = {DocumentType.RESUME: "Resume", DocumentType.COVER_LETTER: "Cover-Letter"}
HUMAN_ACTORS = frozenset({"user", "cli", "dashboard"})
_MAX_VERSION_ATTEMPTS = 50


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def job_dir_name(job_uid: str) -> str:
    return safe_component(job_uid, max_len=64, fallback="job")


def next_version(db: Database, job_id: int, doc_type: DocumentType) -> int:
    current = db.scalar("SELECT MAX(version) FROM document_versions WHERE job_id = ? AND doc_type = ?",
                        (job_id, doc_type.value))
    return int(current or 0) + 1


def _relative_path_taken(db: Database, relative_path: str) -> bool:
    return db.scalar("SELECT 1 FROM document_versions WHERE relative_path = ?", (relative_path,)) is not None


def store_document_set(ctx: "AppContext", *, job: JobListing, doc_type: DocumentType, files: dict[str, bytes],
                       status: DocumentStatus, claims: Sequence[Claim], issues: Sequence[Issue], generator: str,
                       profile_hash: str, on: date, created: list[Path]) -> list[DocumentInfo]:
    """Write all formats of one new version and insert their rows.

    Call inside ``db.transaction()`` so the version number is allocated under
    the write lock. Every file this call creates is appended to ``created`` so
    the caller can remove them if the surrounding transaction fails.
    """
    db = ctx.db
    base = ctx.config.documents_dir
    base.mkdir(parents=True, exist_ok=True)
    dir_name = job_dir_name(job.job_uid)
    job_dir = ensure_within(base, Path(dir_name))
    job_dir.mkdir(parents=True, exist_ok=True)
    kind = KIND_LABEL[doc_type]
    version = next_version(db, job.id, doc_type)
    for _ in range(_MAX_VERSION_ATTEMPTS):
        names = {fmt: document_filename(job.employer, job.title, kind, on, fmt, version) for fmt in files}
        paths = {fmt: ensure_within(base, job_dir / name) for fmt, name in names.items()}
        rels = {fmt: f"{dir_name}/{name}" for fmt, name in names.items()}
        if any(p.exists() for p in paths.values()) or any(_relative_path_taken(db, r) for r in rels.values()):
            version += 1
            continue
        written: list[Path] = []
        try:
            for fmt, data in files.items():
                _write_new(paths[fmt], data)
                written.append(paths[fmt])
        except FileExistsError:  # lost a race for this name: discard our partial set, try the next version
            for p in written:
                p.unlink(missing_ok=True)
            version += 1
            continue
        except BaseException:
            for p in written:
                p.unlink(missing_ok=True)
            raise
        created.extend(written)
        break
    else:
        raise PermanentError(f"could not find a free file name for {kind} of job {job.job_uid}")

    now = utcnow_iso()
    claims_json = to_json([c.model_dump(mode="json", exclude_none=True) for c in claims])
    issues_json = to_json([i.model_dump(mode="json", exclude_none=True) for i in issues])
    infos: list[DocumentInfo] = []
    for fmt, data in files.items():
        digest = sha256_bytes(data)
        doc_id = db.insert(
            "document_versions",
            {
                "job_id": job.id,
                "doc_type": doc_type.value,
                "version": version,
                "format": fmt,
                "file_name": names[fmt],
                "relative_path": rels[fmt],
                "sha256": digest,
                "status": status.value,
                "claims": claims_json,
                "validation_issues": issues_json,
                "generator": generator,
                "profile_hash": profile_hash,
                "created_at": now,
            },
        )
        infos.append(DocumentInfo(
            id=doc_id, job_id=job.id, doc_type=doc_type, version=version, format=fmt,  # type: ignore[arg-type]
            file_name=names[fmt], relative_path=rels[fmt], path=str(paths[fmt]), sha256=digest, status=status,
            generator=generator, profile_hash=profile_hash, created_at=now,
        ))
    return infos


def _write_new(path: Path, data: bytes) -> None:
    """Exclusive create (never overwrites); owner-only permissions (personal data)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def preferred(infos: Sequence[DocumentInfo]) -> DocumentInfo | None:
    for fmt in ATTACHMENT_PREFERENCE:
        for info in infos:
            if info.format == fmt:
                return info
    return None


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #
def _record(row: sqlite3.Row, documents_dir: Path | None = None) -> DocumentRecord:
    path = None
    if documents_dir is not None:
        path = str(ensure_within(documents_dir, Path(row["relative_path"])))
    return DocumentRecord(
        id=row["id"], job_id=row["job_id"], doc_type=DocumentType(row["doc_type"]), version=row["version"],
        format=row["format"], file_name=row["file_name"], relative_path=row["relative_path"], path=path,
        sha256=row["sha256"], status=DocumentStatus(row["status"]), generator=row["generator"],
        profile_hash=row["profile_hash"], created_at=row["created_at"], reviewed_at=row["reviewed_at"],
        claims=from_json(row["claims"], []), validation_issues=from_json(row["validation_issues"], []),
    )


def list_documents(db: Database, job_id: int, *, documents_dir: Path | None = None) -> list[DocumentRecord]:
    """All document versions of a job, newest version first."""
    rows = db.query_all(
        "SELECT * FROM document_versions WHERE job_id = ? ORDER BY doc_type DESC, version DESC, "
        "CASE format WHEN 'pdf' THEN 0 WHEN 'docx' THEN 1 ELSE 2 END",
        (job_id,),
    )
    return [_record(r, documents_dir) for r in rows]


def get_document(db: Database, document_id: int, *, documents_dir: Path | None = None) -> DocumentRecord | None:
    row = db.query_one("SELECT * FROM document_versions WHERE id = ?", (document_id,))
    return None if row is None else _record(row, documents_dir)


def verify_file(documents_dir: Path, record: DocumentRecord) -> Path:
    """The document's file, checked to exist inside documents_dir and match its recorded sha256."""
    path = ensure_within(documents_dir, Path(record.relative_path))
    if not path.is_file():
        raise PermanentError(f"document file {record.relative_path} is missing",
                             human_action="Regenerate the documents with `job-agent prepare <job-uid>`.")
    if sha256_file(path) != record.sha256:
        raise PermanentError(f"document file {record.relative_path} changed after it was generated",
                             human_action="Regenerate the documents; edited files are not approved automatically.")
    return path


def set_document_status(ctx: "AppContext", document_id: int, status: DocumentStatus | str, actor: str, *,
                        note: str | None = None) -> list[DocumentRecord]:
    """Record the user's review of a document version (all formats of that version).

    Only a human actor (user / cli / dashboard) may approve or reject. Approving
    re-checks that every file still matches its recorded sha256.
    """
    target = DocumentStatus(status)
    if actor not in HUMAN_ACTORS:
        raise PermissionDenied("documents can only be approved or rejected by you (cli or dashboard)")
    db = ctx.db
    record = get_document(db, document_id)
    if record is None:
        raise PermanentError(f"document {document_id} not found")
    with db.transaction():
        siblings = [_record(r) for r in db.query_all(
            "SELECT * FROM document_versions WHERE job_id = ? AND doc_type = ? AND version = ?",
            (record.job_id, record.doc_type.value, record.version))]
        if target == DocumentStatus.APPROVED:
            for s in siblings:
                verify_file(ctx.config.documents_dir, s)
        now = utcnow_iso()
        reviewed_at: str | None = now if target in {DocumentStatus.APPROVED, DocumentStatus.REJECTED} else None
        for s in siblings:
            db.update("document_versions", s.id, {"status": target.value, "reviewed_at": reviewed_at})
        details: dict[str, Any] = {"job_id": record.job_id, "doc_type": record.doc_type.value,
                                   "version": record.version, "status": target.value, "note": note,
                                   "document_ids": [s.id for s in siblings]}
        repo.audit(db, actor=actor, action="document_reviewed", entity_type="document", entity_id=document_id,
                   outcome=target.value, details=details)
        app = repo.get_application_for_job(db, record.job_id)
        if app is not None:
            repo.add_application_event(db, int(app["id"]), "document_reviewed", actor=actor, details=details)
    return [r for r in (get_document(db, s.id, documents_dir=ctx.config.documents_dir) for s in siblings) if r]
