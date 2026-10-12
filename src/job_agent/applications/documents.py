"""Résumé / cover-letter files referenced by an application.

Only the ``document_versions`` schema is relied on (owned by the resume
module): ``relative_path`` is relative to ``config.documents_dir``. Every path
is resolved with ``ensure_within`` and every upload re-verifies the file's
SHA-256 against the database, so a file swapped on disk is never uploaded.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from job_agent.errors import HumanActionRequired, JobAgentError
from job_agent.security.sanitize import ensure_within

if TYPE_CHECKING:  # pragma: no cover
    import sqlite3

    from job_agent.database import Database

#: Upload preference: PDF first (renders identically everywhere), then DOCX.
FORMAT_PREFERENCE = ("pdf", "docx", "md")
_MIME = {"pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         "md": "text/markdown"}


@dataclass(frozen=True)
class DocumentRef:
    id: int
    job_id: int
    doc_type: str
    version: int
    format: str
    file_name: str
    relative_path: str
    sha256: str
    status: str

    @classmethod
    def from_row(cls, row: "sqlite3.Row") -> "DocumentRef":
        return cls(id=int(row["id"]), job_id=int(row["job_id"]), doc_type=str(row["doc_type"]),
                   version=int(row["version"]), format=str(row["format"]), file_name=str(row["file_name"]),
                   relative_path=str(row["relative_path"]), sha256=str(row["sha256"]), status=str(row["status"]))

    def path(self, documents_dir: Path) -> Path:
        return ensure_within(documents_dir, Path(self.relative_path))

    def summary(self) -> dict[str, object]:
        return {"document_id": self.id, "file": self.file_name, "format": self.format, "sha256": self.sha256,
                "status": self.status, "version": self.version}


def get_document(db: "Database", document_id: int | None) -> DocumentRef | None:
    if document_id is None:
        return None
    row = db.query_one("SELECT * FROM document_versions WHERE id = ?", (int(document_id),))
    return DocumentRef.from_row(row) if row else None


def variants(db: "Database", doc: DocumentRef) -> list[DocumentRef]:
    """The same document version in every stored format, PDF first."""
    rows = db.query_all("SELECT * FROM document_versions WHERE job_id = ? AND doc_type = ? AND version = ?",
                        (doc.job_id, doc.doc_type, doc.version))
    refs = [DocumentRef.from_row(r) for r in rows] or [doc]
    return sorted(refs, key=lambda d: (FORMAT_PREFERENCE.index(d.format) if d.format in FORMAT_PREFERENCE else 99,
                                       d.id))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_file(doc: DocumentRef, documents_dir: Path) -> Path:
    """Resolved path of an existing file whose hash matches the database."""
    try:
        path = doc.path(documents_dir)
    except JobAgentError as exc:
        raise HumanActionRequired(f"document {doc.file_name}: {exc}") from exc
    if not path.is_file():
        raise HumanActionRequired(
            f"{doc.doc_type.replace('_', ' ')} file {doc.file_name} is missing from {documents_dir}",
            human_action="Regenerate the documents with `job-agent prepare <job-uid>`.",
        )
    actual = sha256_file(path)
    if actual != doc.sha256:
        raise HumanActionRequired(
            f"{doc.file_name} changed on disk since it was generated (sha256 mismatch); it will not be uploaded",
            human_action="Regenerate the documents with `job-agent prepare <job-uid>` and review them again.",
        )
    return path


def accepts(accept: str | None, doc: DocumentRef) -> bool:
    """Does an ``<input type=file accept=...>`` allow this document's format?"""
    if not accept or not accept.strip():
        return True
    ext = f".{doc.format}"
    mime = _MIME.get(doc.format, "")
    for token in (t.strip().lower() for t in accept.split(",")):
        if not token:
            continue
        if token in {"*", "*/*"} or token == ext or token == mime:
            return True
        if token.endswith("/*") and mime.startswith(token[:-1]):
            return True
    return False


def pick_variant(candidates: list[DocumentRef], accept: str | None) -> DocumentRef | None:
    return next((d for d in candidates if accepts(accept, d)), None)


def read_text(doc: DocumentRef, documents_dir: Path) -> str | None:
    """Plain text of a cover letter (md/docx); None when not extractable (PDF)."""
    path = verify_file(doc, documents_dir)
    if doc.format == "md":
        return path.read_text(encoding="utf-8").strip() or None
    if doc.format == "docx":
        try:
            import docx  # python-docx
        except ImportError:  # pragma: no cover - dependency is required by the package
            return None
        text = "\n\n".join(p.text for p in docx.Document(str(path)).paragraphs if p.text.strip())
        return text.strip() or None
    return None
