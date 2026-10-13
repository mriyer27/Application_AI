"""Salary extraction: structured fields first, then conservative text patterns.

Nothing here invents a number. A text match is accepted only when it has a
currency marker and sits in a pay context ("salary", "pay range", "base",
"OTE", "hourly", ...), and is rejected near benefit/business words ("HRA",
"401(k)", "stipend", "revenue", ...). ``is_base`` is ``True`` only when the
text explicitly says *base*, ``False`` only when it explicitly says OTE /
total compensation, otherwise ``None``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from job_agent.models import SalaryPeriod

#: Annual amounts are this large; used only when the posting states no period.
ANNUAL_MAGNITUDE_THRESHOLD = 20_000


@dataclass(frozen=True)
class SalaryInfo:
    min: float | None
    max: float | None
    currency: str | None
    period: SalaryPeriod
    is_base: bool | None
    text: str | None
    source: str  # "structured" | "text"
    period_inferred: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------- #
# Period / base helpers
# --------------------------------------------------------------------------- #
_INTERVAL_MAP: list[tuple[re.Pattern[str], SalaryPeriod]] = [
    (re.compile(r"hour|hr\b", re.I), SalaryPeriod.HOUR),
    (re.compile(r"week|wk\b", re.I), SalaryPeriod.WEEK),
    (re.compile(r"month|mo\b", re.I), SalaryPeriod.MONTH),
    (re.compile(r"year|yr\b|annum|annual", re.I), SalaryPeriod.YEAR),
]


def period_from_interval(value: str | None) -> SalaryPeriod:
    """Map structured interval labels ("1 YEAR", "per-hour-wage", "yearly") to a period."""
    if not value:
        return SalaryPeriod.UNKNOWN
    for pattern, period in _INTERVAL_MAP:
        if pattern.search(value):
            return period
    return SalaryPeriod.UNKNOWN


_BASE_RE = re.compile(r"\bbase\b", re.I)
_TOTAL_RE = re.compile(
    r"(\bOTE\b|on[- ]target|total (target )?(cash )?comp(ensation)?\b|total pay\b|total earnings|"
    r"total cash\b|inclusive of (bonus|commission)|\b\d{2}\s?/\s?\d{2} split\b|split between base)",
    re.I,
)


def base_flag(text: str | None) -> bool | None:
    if not text:
        return None
    has_base = bool(_BASE_RE.search(text))
    has_total = bool(_TOTAL_RE.search(text))
    if has_base and not has_total:
        return True
    if has_total and not has_base:
        return False
    return None


# --------------------------------------------------------------------------- #
# Text extraction
# --------------------------------------------------------------------------- #
_CURRENCY_SYMBOLS = {
    "US$": "USD", "$": "USD", "USD": "USD",
    "CA$": "CAD", "C$": "CAD", "CAD": "CAD",
    "A$": "AUD", "AU$": "AUD", "AUD": "AUD",
    "£": "GBP", "GBP": "GBP",
    "€": "EUR", "EUR": "EUR",
}


def _money(n: str) -> str:
    return (
        rf"(?P<{n}cur>US\$|CA\$|C\$|AU\$|A\$|\$|£|€|(?:USD|CAD|AUD|GBP|EUR)\s?)?\s?"
        rf"(?P<{n}num>\d{{1,3}}(?:,\d{{3}})+|\d{{1,3}}(?:\.\d{{3}})+(?!\d)|\d+)(?:\.(?P<{n}dec>\d{{1,2}})(?!\d))?"
        rf"(?P<{n}mult>[kKmMbB](?![A-Za-z]))?"
    )


_CODE = r"(?P<code>\s?(?:USD|CAD|AUD|GBP|EUR)\b)?"
_RANGE_RE = re.compile(_money("a") + r"\s*(?:-|–|—|to|and)\s*" + _money("b") + _CODE)
_SINGLE_RE = re.compile(_money("a") + _CODE)

_PERIOD_AFTER_RE = re.compile(
    r"^\s*(?:(?:USD|CAD|AUD|GBP|EUR)\s*)?(?:(?:/|per|an|a|each)\s*(?P<unit>hour|hr|year|yr|annum|month|mo|week|wk)\b|"
    r"(?P<adverb>hourly|annually|yearly|monthly|weekly)\b)",
    re.I,
)
_PERIOD_WORDS: list[tuple[re.Pattern[str], SalaryPeriod]] = [
    (re.compile(r"\b(hourly|per hour|an hour|/\s?h(ou)?r)\b", re.I), SalaryPeriod.HOUR),
    (re.compile(r"\b(annual|annually|annum|yearly|per year|a year|/\s?y(ea)?r)\b", re.I), SalaryPeriod.YEAR),
    (re.compile(r"\b(monthly|per month|a month|/\s?mo(nth)?)\b", re.I), SalaryPeriod.MONTH),
    (re.compile(r"\b(weekly|per week|a week|/\s?w(ee)?k)\b", re.I), SalaryPeriod.WEEK),
]
_PAY_CONTEXT_RE = re.compile(
    r"\b(salary|salaries|compensation|pay|pays|paid|wage|wages|base|ote|on[- ]target|earnings|hourly rate|pay rate|"
    r"rate of pay|remuneration)\b",
    re.I,
)
_NEGATIVE_CONTEXT_RE = re.compile(
    r"(\bhra\b|\bfsa\b|\bhsa\b|fertility|401\(?k\)?|reimburs|coverage|lifetime|relocation|sign[- ]on|signing|"
    r"referral|stipend|allowance|learning|wellness|gym|commuter|childcare|parental|donation|matching|"
    r"\bspend\b|annualized|revenue|funding|raised|valuation|\barr\b|customers|budget|billion|million|assets|"
    r"\bloans?\b|\bfees?\b|\bprice|\bcosts?\b|savings|\bgrants?\b|transactions|volume|processed|managed)",
    re.I,
)
_MIN_ONLY_RE = re.compile(r"\b(from|starting (at|from)|starts at|minimum( of)?|min\.?|at least|no less than)\s*$",
                          re.I)
_MAX_ONLY_RE = re.compile(r"\b(up to|maximum( of)?|max\.?|no more than)\s*$", re.I)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\"'“$£€])")


def _amount(m: re.Match[str], n: str) -> float | None:
    mult = (m.group(f"{n}mult") or "").lower()
    if mult in {"m", "b"}:
        return None  # millions/billions: company figures, not pay
    value = float(m.group(f"{n}num").replace(",", "").replace(".", ""))
    if m.group(f"{n}dec"):
        value += float(f"0.{m.group(f'{n}dec')}")
    if mult == "k":
        value *= 1000
    return value


def _currency(m: re.Match[str]) -> str | None:
    """Explicit codes / specific symbols win over a bare "$" ("$92,000-$118,000 CAD" is CAD)."""
    tokens = [t.strip() for t in (m.group("code"), m.group("acur"), m.groupdict().get("bcur")) if t and t.strip()]
    for token in tokens:
        if token != "$":
            return _CURRENCY_SYMBOLS.get(token)
    return "USD" if tokens else None


def _period_for(match_end_text: str, snippet: str) -> SalaryPeriod:
    after = _PERIOD_AFTER_RE.match(match_end_text)
    if after:
        word = (after.group("unit") or after.group("adverb") or "").lower()
        return period_from_interval(word)
    for pattern, period in _PERIOD_WORDS:
        if pattern.search(snippet):
            return period
    return SalaryPeriod.UNKNOWN


@dataclass(frozen=True)
class _Candidate:
    low: float | None
    high: float | None
    currency: str | None
    period: SalaryPeriod
    snippet: str
    explicit_period: bool


def _context_lines(lines: list[str], index: int) -> str:
    """The line itself plus up to three preceding non-empty lines (headings like "US Pay Range")."""
    picked = [lines[index]]
    j = index - 1
    while j >= 0 and len(picked) < 4:
        if lines[j].strip():
            picked.insert(0, lines[j])
        j -= 1
    return "\n".join(picked)


def _heading_before(lines: list[str], index: int) -> str | None:
    j = index - 1
    while j >= 0 and not lines[j].strip():
        j -= 1
    if j >= 0:
        prev = lines[j].strip()
        if len(prev) <= 80 and not prev.startswith("- ") and _PAY_CONTEXT_RE.search(prev):
            return prev
    return None


def salary_from_text(text: str | None) -> SalaryInfo | None:
    """Find explicitly stated pay in free text. Returns ``None`` when nothing qualifies."""
    if not text or not re.search(r"[$£€]|USD|CAD|GBP|EUR|AUD", text):
        return None
    lines = text.split("\n")
    candidates: list[_Candidate] = []
    for index, line in enumerate(lines):
        if not re.search(r"\d", line):
            continue
        context = _context_lines(lines, index)
        heading = _heading_before(lines, index)
        for sentence in _SENTENCE_SPLIT_RE.split(line):
            candidates.extend(_sentence_candidates(sentence, context, heading))
    if not candidates:
        return None
    first = candidates[0]
    same = [c for c in candidates if c.currency == first.currency and c.period == first.period]
    lows = [c.low for c in same if c.low is not None]
    highs = [c.high for c in same if c.high is not None]
    low = min(lows) if lows else None
    high = max(highs) if highs else None
    snippets: list[str] = []
    for c in same:
        if c.snippet not in snippets:
            snippets.append(c.snippet)
    salary_text = " | ".join(snippets[:4])[:600]
    period = first.period
    inferred = False
    if period == SalaryPeriod.UNKNOWN and (low or high or 0) >= ANNUAL_MAGNITUDE_THRESHOLD:
        period, inferred = SalaryPeriod.YEAR, True
    return SalaryInfo(
        min=low, max=high, currency=first.currency, period=period, is_base=base_flag(salary_text),
        text=salary_text, source="text", period_inferred=inferred,
        notes=(("period inferred from magnitude",) if inferred else ()),
    )


def _sentence_candidates(sentence: str, context: str, heading: str | None) -> list[_Candidate]:
    found: list[tuple[int, _Candidate]] = []
    taken: list[tuple[int, int]] = []
    stripped = sentence.strip().removeprefix("- ").strip()
    sentence_has_pay_word = bool(_PAY_CONTEXT_RE.search(sentence))
    for regex, is_range in ((_RANGE_RE, True), (_SINGLE_RE, False)):
        for m in regex.finditer(sentence):
            if any(m.start() < e and m.end() > s for s, e in taken):
                continue
            has_currency = bool(m.group("acur") or (is_range and m.group("bcur")) or m.group("code"))
            if not has_currency:
                continue
            low = _amount(m, "a")
            high = _amount(m, "b") if is_range else low
            if low is None or high is None:
                continue
            if is_range and high < low:
                continue
            if low < 7 or high > 3_000_000 or (low > 0 and high / low > 6):
                continue
            window = sentence[max(0, m.start() - 50): m.end() + 12]
            if _NEGATIVE_CONTEXT_RE.search(window):
                continue
            after = sentence[m.end(): m.end() + 30]
            explicit = bool(_PERIOD_AFTER_RE.match(after))
            if is_range:
                accepted = (sentence_has_pay_word or heading is not None or explicit
                            or bool(_PAY_CONTEXT_RE.search(context)))
            else:
                accepted = sentence_has_pay_word or (heading is not None and len(stripped) <= 40)
            if not accepted:
                continue
            snippet = f"{heading} {stripped}" if heading and heading not in stripped else stripped
            period = _period_for(after, snippet)
            taken.append((m.start(), m.end()))
            lo, hi = low, high
            if not is_range:
                before = sentence[max(0, m.start() - 30): m.start()]
                if _MIN_ONLY_RE.search(before):
                    hi = None  # "from EUR 3,500" / "starting at $60,000": a minimum, not a range
                elif _MAX_ONLY_RE.search(before):
                    lo = None
            found.append((m.start(), _Candidate(lo, hi, _currency(m), period, snippet[:400], explicit)))
    found.sort(key=lambda item: item[0])
    return [c for _, c in found]


# --------------------------------------------------------------------------- #
# Structured helpers
# --------------------------------------------------------------------------- #
def structured_salary(*, low: float | int | None, high: float | int | None, currency: str | None,
                      interval: str | None, text: str | None, extra_text: str | None = None) -> SalaryInfo | None:
    """Build a ``SalaryInfo`` from structured API fields. ``text`` is kept verbatim."""
    lo = float(low) if isinstance(low, (int, float)) and low > 0 else None
    hi = float(high) if isinstance(high, (int, float)) and high > 0 else None
    if lo is None and hi is None:
        return None
    if lo is not None and hi is not None and hi < lo:
        lo, hi = hi, lo
    flag_text = " ".join(t for t in (text, extra_text) if t)
    return SalaryInfo(
        min=lo, max=hi, currency=(currency or None) and str(currency).upper()[:3],
        period=period_from_interval(interval), is_base=base_flag(flag_text), text=(text or None),
        source="structured",
    )
