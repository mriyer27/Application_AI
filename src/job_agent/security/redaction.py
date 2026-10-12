"""Redaction of secrets and sensitive values in logs, audit records and errors."""

from __future__ import annotations

import logging
import re
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_KEY_RE = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|authorization|cookie|session|private[_-]?key|"
    r"client[_-]?secret|refresh|credential|csrf)",
    re.I,
)

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Authorization headers / bearer tokens
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9\-._~+/]{8,}=*"), r"\1 " + REDACTED),
    # Google OAuth tokens
    (re.compile(r"\bya29\.[A-Za-z0-9\-_]{10,}"), REDACTED),
    (re.compile(r"\b1//[A-Za-z0-9\-_]{20,}"), REDACTED),
    (re.compile(r"\bGOCSPX-[A-Za-z0-9\-_]{10,}"), REDACTED),
    # Generic key=value / "key": "value" secrets
    (
        re.compile(
            r"(?i)([\"']?(?:password|passwd|secret|token|api[_-]?key|client_secret|refresh_token|"
            r"access_token|session_secret)[\"']?\s*[:=]\s*)([\"']?)[^\s\"',}]+\2"
        ),
        r"\1\2" + REDACTED + r"\2",
    ),
    # PEM private keys
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), REDACTED),
    # scrypt password hashes produced by job_agent.security.passwords
    (re.compile(r"scrypt\$[0-9]+\$[0-9]+\$[0-9]+\$[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+"), REDACTED),
]

_EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]*@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")
_PHONE_RE = re.compile(r"(?<!\d)(\+?\d{1,2}[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d)")


def redact_text(text: str, *, pii: bool = False) -> str:
    """Remove secrets from free text. With ``pii=True`` also mask emails/phones."""
    if not text:
        return text
    out = text
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    if pii:
        out = _EMAIL_RE.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", out)
        out = _PHONE_RE.sub("[PHONE]", out)
    return out


def redact(value: Any, *, pii: bool = False, by_key: bool = True, _depth: int = 0) -> Any:
    """Recursively redact a JSON-like structure.

    With ``by_key`` (default) values under keys that look sensitive (``token``,
    ``password``...) are blanked; secret-looking substrings are always removed.
    """
    if _depth > 20:
        return REDACTED
    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            if (by_key and isinstance(k, str) and _SENSITIVE_KEY_RE.search(k)
                    and not k.endswith(("_path", "_present", "_configured"))):
                result[k] = REDACTED if v not in (None, "", False) else v
            else:
                result[k] = redact(v, pii=pii, by_key=by_key, _depth=_depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [redact(v, pii=pii, by_key=by_key, _depth=_depth + 1) for v in value]
    if isinstance(value, str):
        return redact_text(value, pii=pii)
    return value


class RedactingFilter(logging.Filter):
    """Logging filter that scrubs secrets (and optionally PII) from every record."""

    def __init__(self, pii: bool = True) -> None:
        super().__init__()
        self.pii = pii

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed %-args: keep the raw msg, still redact
            message = str(record.msg)
        record.msg = redact_text(message, pii=self.pii)
        record.args = None
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text, pii=self.pii)
        return True
