"""Small deterministic text helpers shared by the scoring rules.

Everything here is pure (no I/O, no clock) so scores are reproducible.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any, Iterable

#: Boundaries that treat letters/digits as "inside a word". Unlike ``\b`` they
#: work next to symbols such as ``+`` / ``#`` / ``/`` (C++, C#, A/B testing).
LEFT = r"(?<![A-Za-z0-9])"
RIGHT = r"(?![A-Za-z0-9])"


def bounded(fragment: str) -> str:
    return f"{LEFT}(?:{fragment}){RIGHT}"


@lru_cache(maxsize=4096)
def phrase_regex(phrase: str) -> re.Pattern[str]:
    """Case-insensitive, word-boundary regex for a literal phrase.

    Words may be separated by whitespace or hyphens ("AI-assisted" == "AI assisted").
    """
    words = [re.escape(w) for w in re.split(r"[\s\-]+", phrase.strip()) if w]
    if not words:
        return re.compile(r"(?!x)x")  # matches nothing
    return re.compile(bounded(r"[\s\-]+".join(words)), re.I)


def contains_phrase(text: str | None, phrase: str) -> bool:
    return bool(text) and bool(phrase.strip()) and phrase_regex(phrase).search(text or "") is not None


def as_list(value: Any) -> list[str]:
    """Decode a JSON list column (or pass a list through); always returns strings."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v is not None and str(v).strip()]
    return [str(value)]


def dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def short_list(items: list[str], limit: int = 8) -> str:
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f" (+{len(items) - limit} more)"


# --------------------------------------------------------------------------- #
# Sentences
# --------------------------------------------------------------------------- #
_ABBREVIATIONS = {
    "e.g", "i.e", "etc", "inc", "no", "vs", "approx", "dept", "mr", "ms", "mrs", "dr", "jr", "sr", "st",
    "co", "corp", "ltd", "llc", "a.m", "p.m", "min", "max", "incl", "esp", "ca", "jan", "feb", "mar", "apr",
    "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
}
_BOUNDARY = re.compile(r"[.!?]+[\"'”’)\]]*\s+|\s*\n+\s*|\s*[•·▪●■◦]\s*")
_TOKEN_BEFORE = re.compile(r"([A-Za-z][A-Za-z.]*)$")
_INITIALISM = re.compile(r"^(?:[A-Za-z]\.)+[A-Za-z]$|^[A-Za-z]$")


def _is_abbreviation(token: str) -> bool:
    return token.lower() in _ABBREVIATIONS or bool(_INITIALISM.match(token))


def sentences(text: str | None) -> list[str]:
    """Split text into sentences/bullets. Each returned item is a verbatim substring.

    Periods after abbreviations and initialisms ("U.S. citizen", "e.g. SQL") do
    not end a sentence, nor does a period followed by a lowercase word.
    """
    if not text:
        return []
    out: list[str] = []
    start = 0
    for m in _BOUNDARY.finditer(text):
        token = m.group(0)
        end = m.start()
        if token.lstrip()[:1] in {".", "!", "?"}:
            before = _TOKEN_BEFORE.search(text[start:m.start()])
            following = text[m.end():m.end() + 1]
            if token.lstrip().startswith(".") and before and _is_abbreviation(before.group(1)):
                continue
            if following.islower():
                continue
            end = m.start() + len(token.rstrip())
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        start = m.end()
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def excerpt_around(sentence: str, start: int, end: int, *, max_len: int = 400) -> str:
    """Return the sentence, or a verbatim window of it around ``[start, end)`` if it is long."""
    if len(sentence) <= max_len:
        return sentence
    pad = max(0, (max_len - (end - start)) // 2)
    lo = max(0, start - pad)
    hi = min(len(sentence), end + pad)
    # snap to whitespace so we do not cut words in half (still a verbatim substring)
    if lo > 0:
        space = sentence.find(" ", lo, start)
        lo = space + 1 if space != -1 else lo
    if hi < len(sentence):
        space = sentence.rfind(" ", end, hi)
        hi = space if space != -1 else hi
    return sentence[lo:hi].strip()


# --------------------------------------------------------------------------- #
# Dates ("May 2027", "Fall 2026", "12/2026", "2026")
# --------------------------------------------------------------------------- #
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
    "nov": 11, "dec": 12,
}
#: Seasons map to the month a term typically ends (an upper bound).
_SEASONS = {"spring": 5, "summer": 8, "fall": 12, "autumn": 12, "winter": 12}

DATE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|spring|summer|fall|autumn|winter)\.?,?\s+"
    r"(?:of\s+)?)?(?P<year>20\d{2})(?![0-9])"
    r"|(?<![0-9/])(?P<mnum>0?[1-9]|1[0-2])/(?P<y2>20\d{2})(?![0-9])",
    re.I,
)


def parse_month_year(text: str | None) -> tuple[int, int] | None:
    """First date in ``text`` as ``(year, month)``; a bare year counts as December."""
    if not text:
        return None
    m = DATE_RE.search(text)
    return _date_value(m) if m else None


def _date_value(m: re.Match[str]) -> tuple[int, int]:
    if m.group("y2"):
        return int(m.group("y2")), int(m.group("mnum"))
    year = int(m.group("year"))
    mon = (m.group("mon") or "").lower()
    if not mon:
        return year, 12
    if mon in _SEASONS:
        return year, _SEASONS[mon]
    return year, _MONTHS[mon[:3]]


_LOWER_BOUND_AFTER = re.compile(r"^\s*(?:or|and)\s+(?:later|after|beyond|onwards?)|^\s*onwards?|^\s*\+|^\s*or\s+more\s+recent",
                                re.I)
_LOWER_BOUND_BEFORE = re.compile(
    r"(?:after|since|from|later\s+than|founded\s+in|established\s+in|est\.|copyright|©)\s*$", re.I)


def upper_bound_dates(sentence: str, near: list[tuple[int, int]] | None = None,
                      window: int = 80) -> list[tuple[tuple[int, int], str]]:
    """Dates in a sentence that act as deadlines (not "2024 or later" / "since 2015" style bounds).

    With ``near`` (a list of spans), only dates within ``window`` characters of one of them count.
    """
    found: list[tuple[tuple[int, int], str]] = []
    for m in DATE_RE.finditer(sentence):
        if _LOWER_BOUND_AFTER.search(sentence[m.end():m.end() + 25]):
            continue
        if _LOWER_BOUND_BEFORE.search(sentence[max(0, m.start() - 16):m.start()]):
            continue
        if near is not None and not any(m.start() - window <= e and m.end() + window >= s for s, e in near):
            continue
        found.append((_date_value(m), m.group(0)))
    return found


def format_month_year(value: tuple[int, int]) -> str:
    names = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
             "November", "December"]
    return f"{names[value[1] - 1]} {value[0]}"


# --------------------------------------------------------------------------- #
# Degrees
# --------------------------------------------------------------------------- #
BACHELOR, MASTER, DOCTORATE = 1, 2, 3
DEGREE_NAMES = {BACHELOR: "Bachelor's degree", MASTER: "Master's degree", DOCTORATE: "PhD / doctorate"}

_DOCTORATE_RE = re.compile(r"(?<![A-Za-z])ph\.?\s?d\.?(?![A-Za-z])|doctora(?:te|l)", re.I)
_MASTER_RE = re.compile(
    r"master['’]?s?(?:\s+of|\s+degree|\s+in)|master['’]s|(?<![A-Za-z])m\.?b\.?a(?![A-Za-z])|"
    r"(?<![A-Za-z])(?-i:M\.?S\.?|M\.?A\.?|M\.?Sc\.?|M\.?Eng\.?)(?=\s+(?:degree|in)\b)|graduate\s+degree|advanced\s+degree",
    re.I,
)
_BACHELOR_RE = re.compile(
    r"bachelor['’]?s?|undergraduate\s+degree|(?<![A-Za-z])b\.?\s?tech(?![A-Za-z])|four[\s-]year\s+degree|"
    r"4[\s-]year\s+degree|(?<![A-Za-z])(?-i:B\.?S\.?|B\.?A\.?|B\.?Sc\.?|B\.?E\.?)(?=\s+(?:degree|in)\b)|college\s+degree|"
    r"(?<![A-Za-z])(?-i:B\.?S\.?|B\.?A\.?)(?=\s*/\s*(?-i:M))",
    re.I,
)


def degree_levels(text: str | None) -> set[int]:
    """Degree levels mentioned in text (BACHELOR / MASTER / DOCTORATE)."""
    if not text:
        return set()
    levels: set[int] = set()
    if _DOCTORATE_RE.search(text):
        levels.add(DOCTORATE)
    if _MASTER_RE.search(text):
        levels.add(MASTER)
    if _BACHELOR_RE.search(text):
        levels.add(BACHELOR)
    return levels


def degree_level_of(degree_name: str) -> int | None:
    """Level of a profile degree title such as "Master of Science"."""
    text = degree_name or ""
    if _DOCTORATE_RE.search(text):
        return DOCTORATE
    if re.search(r"master|(?<![A-Za-z])m\.?(?:s|sc|a|eng|ba)\.?(?![A-Za-z])", text, re.I):
        return MASTER
    if re.search(r"bachelor|(?<![A-Za-z])b\.?(?:s|sc|a|e|tech)\.?(?![A-Za-z])", text, re.I):
        return BACHELOR
    return None
