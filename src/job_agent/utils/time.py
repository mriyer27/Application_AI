"""UTC-first timestamp helpers.

All timestamps are stored in SQLite as ISO-8601 strings in UTC with an explicit
``+00:00`` offset, second precision. Local time is only used for display and for
report dates (see ``local_today``).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("naive datetime passed to to_iso(); attach a timezone first")
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def utcnow_iso() -> str:
    return to_iso(utcnow())  # type: ignore[return-value]


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string. Naive values are assumed to be UTC."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def local_today(tz_name: str) -> date:
    return datetime.now(ZoneInfo(tz_name)).date()


def to_local(dt: datetime | None, tz_name: str) -> datetime | None:
    if dt is None:
        return None
    return dt.astimezone(ZoneInfo(tz_name))
