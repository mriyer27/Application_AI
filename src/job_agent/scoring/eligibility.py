"""Eligibility flags from verbatim listing text.

A flag records that the *listing states* something (sponsorship, citizenship,
clearance, export control, graduation/start timing) and quotes the sentence
verbatim. Flags are prompts for human review: they never decide, and their
messages never claim, whether the candidate is or is not eligible. Legal
questions are always answered by the human (CLAUDE.md rule 2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from job_agent.models import EligibilityFlag
from job_agent.scoring.text import (
    DEGREE_NAMES,
    degree_level_of,
    degree_levels,
    excerpt_around,
    parse_month_year,
    sentences,
    upper_bound_dates,
)

if TYPE_CHECKING:  # pragma: no cover
    from job_agent.profile import CandidateProfile

INFO, REVIEW, BLOCKER = "info", "review", "blocker"
SEVERITY_RANK = {BLOCKER: 0, REVIEW: 1, INFO: 2}

_NOT_LEGAL = "This is not a legal determination; decide yourself or ask the employer."

#: Fields scanned, in priority order (discovery stores verbatim excerpts in the first two).
SOURCE_FIELDS = ("sponsorship_info", "work_authorization_requirements", "description_text")


@dataclass(frozen=True)
class _Rule:
    code: str
    severity: str
    pattern: re.Pattern[str]
    message: str
    #: Sentence-level guard: when it matches, the sentence does not raise this flag.
    unless: re.Pattern[str] | None = None


def _rx(*alternatives: str, flags: int = re.I) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{a})" for a in alternatives), flags)


_US = r"u\.?\s?s\.?(?:a\.?)?"
_EEO_GUARD = r"regardless\s+of|without\s+regard|on\s+the\s+basis\s+of|discriminat\w*|protected\s+(?:class|status|characteristic)"

RULES: tuple[_Rule, ...] = (
    _Rule(
        "no_sponsorship", REVIEW,
        _rx(
            r"\bwill\s+not\s+(?:be\s+able\s+to\s+)?(?:provide\s+|offer\s+)?(?:visa\s+)?sponsor(?:ship)?",
            r"\b(?:won['’]t|cannot|can['’]t|can\s+not)\s+(?:provide\s+|offer\s+)?(?:visa\s+)?sponsor(?:ship)?",
            r"\b(?:unable|not\s+able)\s+to\s+(?:provide\s+|offer\s+|support\s+)?(?:visa\s+|immigration\s+)?sponsor(?:ship)?",
            r"\b(?:does|do|did|is|are)\s*(?:not|n['’]t)\s+(?:currently\s+)?(?:(?:offer|provide|support)(?:ing)?\s+"
            r"(?:[\w\-]+\s+){0,2}sponsorship|sponsor(?:ing)?\b)",
            r"\bno\s+(?:visa\s+|immigration\s+|h-?1b\s+|employment\s+)?sponsorship",
            r"\bwithout\s+(?:the\s+need\s+for\s+)?(?:current\s+or\s+future\s+|future\s+|any\s+)?"
            r"(?:visa\s+|employer\s+|immigration\s+|company\s+)?sponsorship",
            r"\bmust\s+be\s+(?:legally\s+)?authori[sz]ed\s+to\s+work\b[^;\n]{0,160}?\bwithout\b",
            r"\bnot\s+(?:currently\s+)?(?:offering|providing)\s+(?:[\w\-]+\s+){0,2}sponsorship",
            r"\bnot\s+(?:currently\s+)?sponsoring\b",
            r"\bsponsorship\s+(?:is\s+)?not\s+(?:available|offered|provided|possible)",
            r"\bnot\s+eligible\s+for\s+(?:visa\s+|immigration\s+)?sponsorship",
        ),
        "Review: the listing states that visa sponsorship is not offered, or that candidates must be authorized "
        "to work without sponsorship. Compare this with your own work-authorization situation. " + _NOT_LEGAL,
    ),
    _Rule(
        "citizenship_required", BLOCKER,
        _rx(
            rf"\b{_US}\s+citizens?(?:hip)?\b",
            r"\bunited\s+states\s+citizens?(?:hip)?\b",
            r"\bamerican\s+citizens?(?:hip)?\b",
            r"\bmust\s+(?:be|hold)\s+(?:a\s+)?(?:\w+\s+)?citizen",
            r"\bcitizenship\s+(?:is\s+)?required",
            r"\brequires?\s+(?:\w+\s+)?citizenship",
        ),
        "Review: the listing states a citizenship requirement. Read the exact wording above. " + _NOT_LEGAL,
        unless=_rx(r"not\s+(?:required|necessary|needed)", r"need\s+not", r"does\s+not\s+(?:need|require)",
                   _EEO_GUARD),
    ),
    _Rule(
        "permanent_residency_required", BLOCKER,
        _rx(r"\bgreen\s+card", r"\b(?:lawful\s+)?permanent\s+residen(?:t|ts|ce|cy)\b"),
        "Review: the listing states a permanent-residency (green card) requirement. Read the exact wording "
        "above. " + _NOT_LEGAL,
        unless=_rx(r"(?:green\s+card|permanent\s+residen\w*)\s+(?:sponsorship|process|application|petition|support)",
                   r"sponsor\w*\s+(?:for\s+|of\s+)?(?:a\s+|your\s+|the\s+)?(?:green\s+card|permanent\s+residen)",
                   r"not\s+(?:required|necessary|needed)", _EEO_GUARD),
    ),
    _Rule(
        "clearance_required", BLOCKER,
        _rx(r"\bsecurity\s+clearance", r"\bts\s*/\s*sci\b", r"\b(?:top\s+)?secret\s+clearance",
            r"\bclearance\s+(?:is\s+)?required", r"\b(?:active|current)\s+(?:\w+\s+)?clearance",
            r"\bpublic\s+trust\s+clearance",
            r"\b(?:obtain|maintain)\s+(?:and\s+maintain\s+)?(?:a\s+|an\s+)?(?:\w+\s+)?clearance"),
        "Review: the listing states a security-clearance requirement. Read the exact wording above. " + _NOT_LEGAL,
        unless=_rx(r"\bno\s+(?:\w+\s+)?clearance", r"clearance\s+(?:is\s+)?not\s+(?:required|needed|necessary)",
                   r"(?:does\s+not|doesn['’]t|won['’]t)\s+require\s+(?:a\s+|an\s+)?(?:\w+\s+)?clearance"),
    ),
    _Rule(
        "export_control", BLOCKER,
        re.compile(
            r"\bitar\b|(?-i:\bEAR\b)|\bexport[\s\-]control(?:s|led)?\b|\bexport\s+administration\s+regulations?\b|"
            r"\binternational\s+traffic\s+in\s+arms\b|\bdeemed\s+exports?\b|"
            rf"(?<![A-Za-z]){_US}\s+persons?\b",
            re.I,
        ),
        "Review: the listing states export-control conditions (ITAR / EAR / U.S. person). Read the exact "
        "wording above. " + _NOT_LEGAL,
        unless=_rx(r"not\s+subject\s+to\s+export"),
    ),
    _Rule(
        "sponsorship_available", INFO,
        _rx(r"\b(?:visa\s+|immigration\s+|h-?1b\s+)?sponsorship\s+(?:is\s+|may\s+be\s+)?available",
            r"\bwe\s+(?:will\s+|can\s+|do\s+)?sponsor\b", r"\bwill\s+sponsor\b", r"\bable\s+to\s+sponsor\b",
            r"\b(?:offer|offers|provide|provides)\s+(?:visa\s+|immigration\s+|h-?1b\s+)sponsorship",
            r"\bopen\s+to\s+sponsor(?:ing|ship)\b", r"\beligible\s+for\s+(?:visa\s+|h-?1b\s+)sponsorship"),
        "Review: the listing states that visa sponsorship may be available. Confirm the details with the "
        "employer. " + _NOT_LEGAL,
        unless=_rx(r"\b(?:no|not|unable|cannot|without|never)\b", r"n['’]t\b"),
    ),
)

_GRAD_KEY = _rx(
    r"\bgraduat(?:e|es|ed|ing|ion)\b", r"\bgrads?\b", r"\bdegree\s+(?:completion|conferr\w*)",
    r"\bcomplet(?:e|ed|ing|ion)\s+(?:of\s+)?(?:your\s+|a\s+|an\s+|the\s+|their\s+)?(?:[\w'’]+\s+){0,2}"
    r"(?:degree|program|studies)",
    r"\bclass\s+of\b",
)
_START_KEY = _rx(
    r"\bstart(?:ing)?\s+dates?\b", r"\bstart(?:s|ing)?\s+(?:full[\s\-]time\s+)?(?:in|on|by|from|no\s+later\s+than)\b",
    r"\b(?:able|available)\s+to\s+(?:start|begin)\b", r"\bbegin(?:ning)?\s+(?:in|on)\b",
)
_MUST_COMPLETED = _rx(r"\bmust\s+have\s+(?:already\s+)?(?:completed|graduated|earned|obtained|received|finished)\b",
                      r"\bdegree\s+(?:must\s+be|is)\s+(?:completed|conferred)\b")


# --------------------------------------------------------------------------- #
def expected_graduation(profile: "CandidateProfile | None") -> tuple[tuple[int, int], str, int | None] | None:
    """Latest *expected* degree completion in the profile: ``((year, month), label, level)``."""
    if profile is None:
        return None
    best: tuple[tuple[int, int], str, int | None] | None = None
    for edu in profile.education:
        if not edu.expected:
            continue
        when = parse_month_year(edu.end)
        if when is None:
            continue
        label = f"{edu.degree}{' in ' + edu.field if edu.field else ''}, expected {edu.end}"
        if best is None or when > best[0]:
            best = (when, label, degree_level_of(edu.degree))
    return best


def completed_degree_levels(profile: "CandidateProfile | None") -> set[int]:
    if profile is None:
        return set()
    return {lvl for e in profile.education if not e.expected and (lvl := degree_level_of(e.degree))}


def _flag(code: str, severity: str, excerpt: str, message: str) -> EligibilityFlag:
    return EligibilityFlag(code=code, severity=severity, excerpt=excerpt, message=message)


def _rule_flags(sentence: str) -> list[EligibilityFlag]:
    found: list[EligibilityFlag] = []
    for rule in RULES:
        m = rule.pattern.search(sentence)
        if not m:
            continue
        if rule.unless is not None and rule.unless.search(sentence):
            continue
        found.append(_flag(rule.code, rule.severity, excerpt_around(sentence, m.start(), m.end()), rule.message))
    return found


def _timing_flag(sentence: str, expected: tuple[tuple[int, int], str, int | None] | None,
                 completed_levels: set[int], *, internship: bool) -> EligibilityFlag | None:
    if expected is None:
        return None
    (when, label, expected_level) = expected
    grad = _GRAD_KEY.search(sentence)
    start = None if internship else _START_KEY.search(sentence)
    anchor = grad or start
    if anchor is not None:
        spans = [m.span() for m in _GRAD_KEY.finditer(sentence)]
        if not internship:
            spans += [m.span() for m in _START_KEY.finditer(sentence)]
        dates = upper_bound_dates(sentence, near=spans)
        if dates:
            latest, text = max(dates, key=lambda d: d[0])
            if latest < when:
                kind = "graduation" if grad else "start-date"
                message = (
                    f"Review: the listing states a {kind} timing requirement ({text}); your profile lists "
                    f"{label}. Confirm the timing with the employer before investing time."
                )
                return _flag("graduation_timing", REVIEW, excerpt_around(sentence, anchor.start(), anchor.end()),
                             message)
            return None
    must = _MUST_COMPLETED.search(sentence)
    if must is None:
        return None
    levels = degree_levels(sentence)
    required = max(levels) if levels else expected_level
    if required is None:
        return None
    if completed_levels and max(completed_levels) >= required:
        return None
    if expected_level is None or expected_level < required:
        return None  # a degree the profile does not list at all is a qualification gap, handled by the engine
    message = (
        f"Review: the listing states the degree must already be completed ({DEGREE_NAMES.get(required, 'degree')}); "
        f"your profile lists {label}. Confirm the timing with the employer before investing time."
    )
    return _flag("graduation_timing", REVIEW, excerpt_around(sentence, must.start(), must.end()), message)


def detect_eligibility_flags(job: Mapping[str, Any], profile: "CandidateProfile | None" = None, *,
                             extra_texts: Iterable[str] = ()) -> list[EligibilityFlag]:
    """Flags for every eligibility statement in the listing (verbatim excerpts, deduplicated).

    ``extra_texts`` may carry further *verbatim listing excerpts* (e.g. the
    validated ``JobAnalysis.sponsorship_statements``).
    """
    texts = [str(job.get(f) or "") for f in SOURCE_FIELDS] + [str(t) for t in extra_texts if t]
    internship = str(job.get("employment_type") or "").lower() == "internship" or bool(
        re.search(r"\bintern(?:ship)?s?\b", str(job.get("title") or ""), re.I))
    expected = expected_graduation(profile)
    completed = completed_degree_levels(profile)

    flags: list[EligibilityFlag] = []
    for text in texts:
        for sentence in sentences(text):
            candidates = _rule_flags(sentence)
            timing = _timing_flag(sentence, expected, completed, internship=internship)
            if timing is not None:
                candidates.append(timing)
            for flag in candidates:
                if not _duplicate(flag, flags):
                    flags.append(flag)
    order = {id(f): i for i, f in enumerate(flags)}
    return sorted(flags, key=lambda f: (SEVERITY_RANK.get(f.severity, 9), order[id(f)]))


def _duplicate(flag: EligibilityFlag, existing: list[EligibilityFlag]) -> bool:
    new = flag.excerpt.lower()
    for other in existing:
        if other.code != flag.code:
            continue
        old = other.excerpt.lower()
        if new == old or new in old or old in new:
            return True
    return False


def has_blocker(flags: Iterable[EligibilityFlag]) -> bool:
    return any(f.severity == BLOCKER for f in flags)


def needs_review(flags: Iterable[EligibilityFlag]) -> bool:
    return any(f.severity in {REVIEW, BLOCKER} for f in flags)
