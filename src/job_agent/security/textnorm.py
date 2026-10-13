"""Unicode normalization for validators.

Validators that decide whether generated or untrusted text is truthful or
legal must see the same characters a reader (and the PDF renderer) will see.
Full-width letters ("Ｌｅｄ"), look-alike Cyrillic/Greek letters ("Lеd"),
circled/superscript/roman numerals ("⑤⓪⓪", "Ⅻ") and invisible characters
would otherwise slip past ASCII-oriented patterns and be rendered later as
plain ASCII claims.

Rules for callers:
  * validate ``normalize_text(text)``, and store/render that same normalized text;
  * treat ``suspicious_characters(candidate)`` as a rejection reason for
    generated text whose verified source contains no such characters.
"""

from __future__ import annotations

import re
import unicodedata

#: Look-alike letters (Cyrillic, Greek, others) mapped to the Latin letter they imitate.
CONFUSABLES: dict[str, str] = {
    # Cyrillic lower
    "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c",
    "т": "t", "у": "y", "х": "x", "ѕ": "s", "і": "i", "ї": "i", "ј": "j", "ԁ": "d", "һ": "h", "ԛ": "q",
    "ԝ": "w", "ь": "b", "г": "r", "п": "n",
    # Cyrillic upper
    "А": "A", "В": "B", "Е": "E", "Ё": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C",
    "Т": "T", "У": "Y", "Х": "X", "Ѕ": "S", "І": "I", "Ї": "I", "Ј": "J", "Ԁ": "D", "Һ": "H",
    # Greek
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u",
    "χ": "x", "ω": "w", "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M",
    "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
    # Other common tricks
    "ı": "i", "ȷ": "j", "ℓ": "l", "ƅ": "b", "ɡ": "g", "ɑ": "a", "ʟ": "L",
}
_CONFUSABLE_RE = re.compile("[" + re.escape("".join(CONFUSABLES)) + "]")

#: Zero-width, bidi-control and other invisible formatting characters.
_INVISIBLE_RE = re.compile(r"[­͏؜ᅟᅠ឴឵᠋-᠏​-‏"
                           r"‪-‮⁠-⁯ㅤ︀-️﻿ﾠ]")

#: Typographic punctuation that is fine in generated prose (kept as-is).
_ALLOWED_TYPOGRAPHY = set("‘’‚“”„–—‐‑‒•…·°×€£")


def normalize_text(text: str | None) -> str:
    """NFKC-fold, map look-alike letters to Latin, drop invisible characters.

    Full-width and compatibility forms become ASCII ("Ｌｅｄ" -> "Led",
    "①" -> "1", "Ⅻ" -> "XII", "²" -> "2"); Cyrillic/Greek look-alikes become
    the Latin letters they imitate. Accented Latin letters (é, ñ) are kept.
    """
    if not text:
        return ""
    out = _INVISIBLE_RE.sub("", text)
    out = unicodedata.normalize("NFKC", out)
    out = _CONFUSABLE_RE.sub(lambda m: CONFUSABLES[m.group(0)], out)
    return out


def suspicious_characters(text: str | None) -> list[str]:
    """Characters that suggest an attempt to evade validation or that the PDF cannot show faithfully.

    Returns the offending characters (deduplicated, in order): invisible
    characters, compatibility forms that NFKC changes (full-width, circled,
    superscript, roman numerals), look-alike letters, and letters/digits
    outside Latin-1 (e.g. a Cyrillic word inside an English bullet).
    """
    if not text:
        return []
    found: list[str] = []

    def add(ch: str) -> None:
        if ch not in found:
            found.append(ch)

    for ch in text:
        if ch.isascii():
            continue
        if _INVISIBLE_RE.match(ch) or ch in CONFUSABLES:
            add(ch)
            continue
        if ch in _ALLOWED_TYPOGRAPHY:
            continue
        if unicodedata.normalize("NFKC", ch) != ch:
            add(ch)
            continue
        if (ch.isalpha() or unicodedata.category(ch).startswith("N")) and ord(ch) > 0xFF:
            add(ch)
    return found


def has_non_ascii_numerals(text: str | None) -> bool:
    """True for numerals that are not ASCII digits ("⑤", "Ⅻ", "²", "٣")."""
    return any(not ch.isascii() and unicodedata.category(ch).startswith("N") for ch in text or "")
