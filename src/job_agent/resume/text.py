"""Deterministic text helpers shared by keyword extraction, selection and validation."""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

STOPWORDS: frozenset[str] = frozenset(
    """
    a about above across after again against all also am an and any are as at be because been before being below
    between both but by can could did do does doing down during each either etc few for from further had has have
    having he her here hers him his how i if in into is it its itself just like may me might more most must my no nor
    not of off on once only or other our ours out over own per same she should so some such than that the their them
    then there these they this those through to too under until up upon us very via was we well were what when where
    which while who whom why will with within without would you your yours yourself
    """.split()
)

#: Words too generic to count as a job keyword on their own.
GENERIC_TERMS: frozenset[str] = frozenset(
    """
    ability able experience experiences experienced strong excellent good great team teams work working works role
    roles candidate candidates requirement requirements required preferred qualification qualifications responsibility
    responsibilities including include includes job position company opportunity opportunities years year plus skills
    skill knowledge understanding familiarity proficiency proficient demonstrated proven track record new using use
    across ensure help helping support supporting provide providing make making within level related relevant equivalent
    degree bachelor master field environment fast paced paced business-critical day days time based ideal ideally etc
    one two three four five six seven eight nine ten we're you'll you're us our join apply benefits salary range
    """.split()
)

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#&/'.-]*")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])|\n+")


def normalize_space(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def words(text: str) -> list[str]:
    """Word tokens with trailing punctuation removed (case preserved)."""
    out = []
    for m in _WORD_RE.finditer(text or ""):
        w = m.group(0).rstrip(".'-/&")
        if w:
            out.append(w)
    return out


def stem(word: str) -> str:
    """A tiny, deterministic suffix stripper (enough to match analyze/analyzed/analyzing)."""
    w = word.lower()
    for suffix in ("ations", "ation", "ings", "ing", "edly", "ied", "ies", "ed", "es", "ly", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 4:
            w = w[: -len(suffix)]
            break
    if w.endswith("e") and len(w) > 4:
        w = w[:-1]
    return w


def content_words(text: str) -> list[str]:
    """Lowercased, stemmed non-stopword tokens (length >= 3), in order."""
    out = []
    for w in words(text):
        lw = w.lower()
        if len(lw) < 3 or lw in STOPWORDS:
            continue
        out.append(stem(lw))
    return out


def content_set(text: str) -> set[str]:
    return set(content_words(text))


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text or "") if s and s.strip()]


_WS = r"\s+"


@lru_cache(maxsize=4096)
def _phrase_rx(needle: str) -> re.Pattern[str]:
    body = _WS.join(re.escape(p) for p in needle.split(" "))
    return re.compile("(?<![A-Za-z0-9])" + body + "(?![A-Za-z0-9])", re.IGNORECASE)


def mentions(haystack: str, needle: str) -> bool:
    """Case-insensitive, word-boundary phrase search (tolerates flexible whitespace)."""
    needle = normalize_space(needle)
    if not needle or not haystack:
        return False
    return bool(_phrase_rx(needle).search(haystack))


def to_latin1(text: str) -> str:
    """Transliterate to latin-1 for base-14 PDF fonts; never raises.

    Common typographic characters get ASCII equivalents; accented letters lose
    their accents through *canonical* decomposition (NFD: "ő" -> "o"); anything
    else becomes "?". Compatibility folding (NFKD) is deliberately not used: it
    would turn full-width letters, circled / superscript / roman numerals and
    similar look-alikes ("Ｌｅｄ", "⑤⓪⓪") into plain ASCII claims that no
    validator saw. Generated text reaches the renderer already normalized and
    validated (``validation.claim_text``).
    """
    out: list[str] = []
    for ch in text or "":
        if ord(ch) < 256:
            out.append(ch)
            continue
        if ch in _TYPOGRAPHIC:
            out.append(_TYPOGRAPHIC[ch])
            continue
        decomposed = unicodedata.normalize("NFD", ch)
        kept = "".join(c for c in decomposed if ord(c) < 256 and not unicodedata.combining(c))
        out.append(kept if kept else "?")
    return "".join(out)


_TYPOGRAPHIC = {
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-",
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "•": "-", "‣": "-", "⁃": "-", "…": "...", "™": "(TM)", "€": "EUR",
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
    "​": "", "‌": "", "‍": "", "⁠": "", "﻿": "",
}
