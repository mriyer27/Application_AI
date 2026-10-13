"""Safe filenames and path containment (prevents path traversal)."""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from pathlib import Path

from job_agent.errors import PermanentError

_UNSAFE = re.compile(r"[^A-Za-z0-9]+")
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def safe_component(text: str, *, max_len: int = 40, fallback: str = "Unknown") -> str:
    """Reduce arbitrary (untrusted) text to a safe filename component.

    ASCII letters/digits only, words joined by ``-``; never empty, never ``.``/``..``.
    """
    normalized = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    words = [w for w in _UNSAFE.split(normalized) if w]
    out = "-".join(words)[:max_len].strip("-")
    if not out or out.lower() in _RESERVED:
        out = fallback
    return out


def document_filename(employer: str, role: str, kind: str, on: date, ext: str, version: int = 1) -> str:
    """``Employer_Role_Resume_YYYY-MM-DD[_vN].ext`` with every part sanitized."""
    ext = ext.lower().lstrip(".")
    if ext not in {"pdf", "docx", "md", "txt", "png", "json"}:
        raise PermanentError(f"unsupported document extension {ext!r}")
    parts = [safe_component(employer), safe_component(role, max_len=60), safe_component(kind, max_len=20),
             on.isoformat()]
    name = "_".join(parts)
    if version > 1:
        name += f"_v{version}"
    return f"{name}.{ext}"


def ensure_within(base: Path, candidate: Path) -> Path:
    """Resolve ``candidate`` and ensure it stays inside ``base``; raise otherwise."""
    base_resolved = base.resolve()
    target = (candidate if candidate.is_absolute() else base_resolved / candidate).resolve()
    if target != base_resolved and base_resolved not in target.parents:
        raise PermanentError(f"refusing path outside {base_resolved}: {candidate}")
    return target
