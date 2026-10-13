"""Safe updates to the local ``.env`` file (0600 permissions, values single-quoted)."""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def set_env_var(env_path: Path, key: str, value: str) -> None:
    """Set ``key`` in the .env file, preserving other lines. The value is single-quoted
    so python-dotenv does not interpolate ``$`` sequences (e.g. in password hashes)."""
    if not _KEY_RE.match(key):
        raise ValueError(f"invalid env var name {key!r}")
    if "'" in value or "\n" in value:
        raise ValueError("value may not contain single quotes or newlines")
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    new_line = f"{key}='{value}'"
    replaced = False
    for i, line in enumerate(lines):
        if re.match(rf"^\s*(export\s+)?{re.escape(key)}\s*=", line):
            lines[i] = new_line
            replaced = True
    if not replaced:
        lines.append(new_line)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = env_path.with_name(env_path.name + ".tmp")
    # O_BINARY: on Windows a text-mode fd would turn each \n written below into \r\r\n.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, env_path)
    try:
        os.chmod(env_path, 0o600)
    except OSError:  # pragma: no cover
        pass
    os.environ[key] = value


def generate_secret(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)
