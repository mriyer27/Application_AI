"""Logging with rotation and redaction.

Every handler carries a ``RedactingFilter`` so secrets (tokens, passwords,
password hashes, auth headers) and PII (emails, phone numbers) are scrubbed
before anything is written to disk or the console.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from job_agent.security.redaction import RedactingFilter

_CONFIGURED_MARK = "_job_agent_handler"


def setup_logging(logs_dir: Path | None, level: str = "INFO", *, max_bytes: int = 5_000_000,
                  backup_count: int = 5, console: bool = True, console_level: str = "WARNING") -> None:
    root = logging.getLogger("job_agent")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False
    # Idempotent: remove handlers we installed previously.
    for h in list(root.handlers):
        if getattr(h, _CONFIGURED_MARK, False):
            root.removeHandler(h)
            h.close()

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S%z")
    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            Path(logs_dir) / "job_agent.log", maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        fh.addFilter(RedactingFilter(pii=True))
        setattr(fh, _CONFIGURED_MARK, True)
        root.addHandler(fh)
    if console:
        ch = logging.StreamHandler()
        ch.setLevel(getattr(logging, console_level.upper(), logging.WARNING))
        ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        ch.addFilter(RedactingFilter(pii=True))
        setattr(ch, _CONFIGURED_MARK, True)
        root.addHandler(ch)


def get_logger(name: str) -> logging.Logger:
    if not name.startswith("job_agent"):
        name = f"job_agent.{name}"
    return logging.getLogger(name)
