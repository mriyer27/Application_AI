"""Text safety and display helpers shared by the workbook and Markdown renderers.

Everything that originates from the database is treated as untrusted display
text: job titles, employers, email subjects and error messages may contain
spreadsheet formulas, Markdown/HTML, control characters or secrets.
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime
from typing import Any, Iterable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from job_agent.logging_config import get_logger
from job_agent.security.redaction import redact_text
from job_agent.utils.time import parse_iso

log = get_logger(__name__)

#: Excel's hard limit for one cell is 32,767 characters.
EXCEL_CELL_LIMIT = 32_000
#: Excel ignores hyperlinks longer than ~2,080 characters.
MAX_URL_LENGTH = 2_000

# Characters that make a spreadsheet application interpret a cell as a formula
# (or DDE payload) when the value is typed, pasted or re-imported from CSV.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n", "＝", "＋", "－", "＠")
# Control characters that are illegal in XLSX XML (same set openpyxl rejects).
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]")
_WS = re.compile(r"\s+")


# --------------------------------------------------------------------------- #
# Spreadsheet cells
# --------------------------------------------------------------------------- #
def excel_text(value: str, *, limit: int = EXCEL_CELL_LIMIT) -> str:
    """Neutralize a string for a spreadsheet cell.

    Removes characters illegal in XLSX, truncates to Excel's cell limit and
    prefixes a single quote when the text starts with a formula trigger
    (``= + - @``, tab, CR/LF, or their full-width variants), so the value is
    stored and shown as text and stays text if exported to CSV and re-opened.
    """
    text = _ILLEGAL_XML.sub("", value)
    if len(text) > limit:
        text = text[: limit - 15] + " …[truncated]"
    if text.startswith(_FORMULA_PREFIXES):
        text = "'" + text
    return text


def safe_http_url(value: Any) -> str | None:
    """Return the URL if it is a plain absolute http(s) URL, else ``None``.

    Only such URLs become clickable hyperlinks; anything else (``javascript:``,
    ``file:``, ``mailto:``, relative paths, embedded whitespace/control
    characters, over-long values) is shown as plain text at most.
    """
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or len(url) > MAX_URL_LENGTH or _CONTROL.search(url) or any(c.isspace() for c in url):
        return None
    if any(c in url for c in '<>"`\\'):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return None
    return url


def clean_inline(value: Any, *, max_len: int = 300) -> str:
    """Collapse whitespace, drop control characters and truncate (no escaping)."""
    if value is None:
        return ""
    text = _CONTROL.sub("", str(value))
    text = _WS.sub(" ", text).strip()
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def redacted(value: Any, *, max_len: int = 500) -> str:
    """Secrets and PII removed (for error messages, audit details, email text)."""
    return clean_inline(redact_text(str(value), pii=True) if value else "", max_len=max_len)


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#|~!])")
_MD_LEADING = re.compile(r"^(\d+)([.)])")


def md_escape(value: Any, *, max_len: int = 200) -> str:
    """Escape untrusted text for inline use in Markdown.

    HTML is entity-escaped (so no raw tags reach a renderer), Markdown
    punctuation that could create links, emphasis, code, tables or headings is
    backslash-escaped, newlines are collapsed so text cannot start a new block.
    """
    text = clean_inline(value, max_len=max_len)
    text = html.escape(text, quote=False)
    text = _MD_SPECIAL.sub(r"\\\1", text)
    text = _MD_LEADING.sub(r"\1\\\2", text)
    if text[:1] in {"+", "-", "=", ">"}:
        text = "\\" + text
    return text


def md_url(value: Any) -> str:
    """An autolink for a safe http(s) URL, or an empty string."""
    url = safe_http_url(value)
    if url is None or any(c in url for c in "()[]"):
        return ""
    return f"<{url}>"


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #
def parse_ts(value: Any) -> datetime | None:
    """Parse a stored ISO timestamp (UTC-aware) or return ``None`` if missing/malformed."""
    if not value or not isinstance(value, str):
        return None
    try:
        return parse_iso(value)
    except ValueError:
        log.warning("ignoring malformed timestamp in report data: %r", value[:40])
        return None


def local_naive(value: Any, tz_name: str) -> datetime | None:
    """Stored UTC timestamp -> naive local datetime (Excel cannot store time zones)."""
    dt = parse_ts(value)
    if dt is None:
        return None
    return dt.astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)


def local_date(value: Any, tz_name: str) -> date | None:
    """Local calendar date of a stored timestamp. Date-only strings are kept as-is."""
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    dt = local_naive(value, tz_name)
    return dt.date() if dt else None


def local_text(value: Any, tz_name: str, *, with_time: bool = True) -> str:
    dt = local_naive(value, tz_name)
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M") if with_time else dt.strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# Labels
# --------------------------------------------------------------------------- #
CATEGORY_LABELS = {
    "strong_match": "Strong match",
    "possible_match": "Possible match",
    "weak_match": "Weak match",
    "needs_review": "Needs review",
}

EVIDENCE_LABELS = {
    "page_evidence": "confirmation page",
    "email_confirmed": "confirmation email",
    "user_reported": "reported by you",
    "none": "no evidence",
}


def humanize(value: Any) -> str:
    if value is None or value == "":
        return ""
    text = str(value).replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def status_label(value: Any) -> str:
    return humanize(value)


def category_label(value: Any) -> str:
    if value is None:
        return ""
    return CATEGORY_LABELS.get(str(value), humanize(value))


def evidence_label(value: Any) -> str:
    return EVIDENCE_LABELS.get(str(value or "none"), humanize(value))


def join_list(items: Iterable[Any], *, sep: str = "; ", max_items: int = 20, max_len: int = 200) -> str:
    out = [clean_inline(i, max_len=max_len) for i in list(items)[:max_items] if i not in (None, "")]
    return sep.join(x for x in out if x)


def flags_text(flags: Any, *, sep: str = "\n") -> str:
    """Eligibility flags (list of dicts) -> "code [severity]: message" lines."""
    if not isinstance(flags, list):
        return ""
    lines = []
    for flag in flags[:20]:
        if isinstance(flag, dict):
            code = clean_inline(flag.get("code") or "flag", max_len=60)
            sev = clean_inline(flag.get("severity") or "", max_len=20)
            msg = clean_inline(flag.get("message") or flag.get("excerpt") or "", max_len=300)
            lines.append(f"{code} [{sev}]: {msg}" if sev else f"{code}: {msg}")
        elif flag:
            lines.append(clean_inline(flag, max_len=300))
    return sep.join(lines)


def text_list(value: Any) -> list[str]:
    """JSON list column (already decoded) -> list of non-empty strings."""
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item)
        elif isinstance(item, dict):
            text = item.get("text") or item.get("requirement") or item.get("name")
            if isinstance(text, str) and text.strip():
                out.append(text)
    return out
