"""Eligibility flags from verbatim listing text.

A flag records that the *listing states* something (sponsorship, work
authorization, citizenship, clearance, export control, graduation/start timing)
and quotes the sentence verbatim. Flags are prompts for human review: they never
decide, and their messages never claim, whether the candidate is or is not
eligible. Legal questions are always answered by the human (CLAUDE.md rule 2).

Exceptions (negations such as "not required", equal-opportunity wording) are
scoped to the clause / list item that holds the match, so they cannot hide a
requirement stated elsewhere in the same sentence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

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
    #: Clause-level guard: a match whose *clause* also matches this does not raise the flag.
    #: Clauses are split on ";", ", but", " however ", ... so a negation elsewhere in the
    #: sentence ("..., but prior experience is not required") cannot hide the requirement.
    unless: re.Pattern[str] | None = None
    #: Statements that raise the flag even when the guard matches
    #: ("candidates with no active clearance will not be considered").
    override: re.Pattern[str] | None = None
    #: ``veto(sentence, match)`` -> True when the match is really something else ("customs clearance").
    veto: Callable[[str, re.Match[str]], bool] | None = None
    #: Skip a match that sits inside an equal-opportunity list ("without regard to race, ... citizenship").
    eeo_guard: bool = False
    #: Codes that make this flag redundant when raised by the same sentence.
    suppressed_by: tuple[str, ...] = ()


def _rx(*alternatives: str, flags: int = re.I) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{a})" for a in alternatives), flags)


_US = r"u\.?\s?s\.?(?:a\.?)?"
_USA = rf"(?:the\s+)?(?:{_US}|united\s+states(?:\s+of\s+america)?|america)"
_NOT = r"(?:not|n['’]t)"

#: Immigration / visa vocabulary. "visa status" / "immigration status" are excluded because they are the
#: wording of equal-opportunity statements ("we will not consider your immigration status").
_IMM = (
    r"(?:\b(?:visa|employer|immigration|employment|h-?1-?b)[\s\-]+)?\bsponsor(?:ship|ships|ing|ed)?\b"
    r"|\bvisas?\b(?!\s+status)|\bh-?1-?bs?\b|\bimmigration\b(?!\s+status)|\bwork\s+permits?\b"
    r"|\bemployment[\s\-]based\s+(?:immigration|green\s+card|petition)\w*"
    r"|(?-i:\b(?:OPT|CPT|EAD)\b)|\bstem[\s\-]opt\b"
    r"|(?-i:\b(?:F|J|H|L|O)-1[AB]?\b|\bH-4\b|\bE-3\b)|\b[fj]-?1\s+(?:visa|student|status)\w*"
    r"|\btn\s+(?:visa|status)\b"
)
#: "... will not be considered" / "... are not eligible" / "... need not apply".
_EXCL = (
    rf"\b(?:(?:will|would|shall|can|may|could)\s*{_NOT}|cannot|can\s+not|not)\s+be\s+"
    r"(?:considered|eligible|accepted|hired|reviewed)\b"
    rf"|\bneed\s+not\s+apply\b|\b(?:should|must|do|please)\s*{_NOT}\s+apply\b"
    r"|\bnot\s+eligible\b|\bineligible\b|\bdisqualif\w*"
)
#: "not open to ..." / "we cannot consider ..." (the exclusion comes first).
_EXCL_BEFORE = (
    rf"\bnot\s+open\s+to\b|\b(?:will|can|do|does)\s*{_NOT}\s+(?:consider|accept)\b"
    r"|\b(?:unable\s+to|not\s+able\s+to|cannot|can\s+not)\s+(?:consider|accept|hire|employ)\b"
)
_NEG = (
    rf"\b(?:will\s+not|won['’]t|cannot|can['’]t|can\s+not|unable\s+to|not\s+able\s+to|"
    rf"(?:do|does|did|is|are|am|was|were)\s*{_NOT}|no\s+longer|never)"
)
_CLEARANCE_GUARD_NEG = (
    r"\bno\s+(?:\w+[\s/\-]+){0,2}?clearances?\b",
    r"\bclearances?\s+(?:is\s+|are\s+)?not\s+(?:required|needed|necessary|mandatory)",
    rf"\b(?:does|do|did|will|would)\s*{_NOT}\s+require\s+(?:a\s+|an\s+|any\s+)?(?:\w+\s+)?clearances?",
    r"\bclearances?\s*[:\-–]\s*(?:not\s+required|none|n/?a)\b",
)
#: Requirement wording right after a match: it is a requirement, not an EEO category.
_REQ_AFTER = re.compile(r"[^,;]{0,30}?\b(?:required|mandatory|requirement|only)\b", re.I)
#: Word right before "clearance" that makes it a non-security clearance ("customs clearance").
_NON_SECURITY_BEFORE = re.compile(
    r"\b(?:customs|medical|regulatory|tax|fda|ceiling|credit|airspace|flight|drug|import|export|trade|legal|music|"
    r"rights|copyright|financial|height|road)[\s\-]+$", re.I)
_EEO_TRIGGER = re.compile(
    r"\b(?:regardless\s+of|without\s+regard\s+(?:to|for)|on\s+the\s+basis\s+of|discriminat\w*|"
    r"protected\s+(?:class|status|characteristic|categor)\w*)", re.I)
#: Words that end an EEO list ("... without regard to race. U.S. citizenship *is required*").
_LIST_BREAK = re.compile(
    r"[;:]|\b(?:is|are|be|must|shall|will|requir\w*|need\w*|mandatory|only|hold|possess|but|however|although|though)\b",
    re.I)
#: Clause boundaries for the guards. "preferred but not required" stays one clause.
_CLAUSE_BREAK = re.compile(
    r"\s*;\s*|,\s+and\s+|,\s+while\s+|,?\s+(?:but|however|although|though|whereas|except\s+that)\b(?!,?\s+not\b),?\s*",
    re.I)
#: A bare "and" also ends a clause that already has its own predicate ("X is required and Y is not required"),
#: but not a compound subject ("U.S. citizenship and clearance are not required").
_AND = re.compile(r"\s+and\s+", re.I)
_PREDICATE = re.compile(r"\b(?:required|requires?|requirement|must|mandatory|only|needed|necessary)\b", re.I)


def _non_security_clearance(sentence: str, m: re.Match[str]) -> bool:
    """Every "clearance" in the match is a customs / medical / ... clearance."""
    hits = [c.start() + m.start() for c in re.finditer(r"clearances?\b", m.group(0), re.I)]
    return bool(hits) and all(_NON_SECURITY_BEFORE.search(sentence, max(0, h - 40), h) for h in hits)


#: Form I-9 / E-Verify onboarding boilerplate: paperwork every hire completes, not a listing condition.
_I9_BOILERPLATE = re.compile(r"\b(?:verify|verif(?:ied|ication)|i-?9|e-?verify)\b", re.I)


def _onboarding_paperwork(sentence: str, m: re.Match[str]) -> bool:
    return bool(_I9_BOILERPLATE.search(sentence))


RULES: tuple[_Rule, ...] = (
    _Rule(
        "no_sponsorship", REVIEW,
        _rx(
            r"\bwill\s+not\s+(?:be\s+able\s+to\s+)?(?:provide\s+|offer\s+)?(?:visa\s+)?sponsor(?:ship)?",
            r"\b(?:won['’]t|cannot|can['’]t|can\s+not)\s+(?:provide\s+|offer\s+)?(?:visa\s+)?sponsor(?:ship)?",
            r"\b(?:unable|not\s+able)\s+to\s+(?:provide\s+|offer\s+|support\s+)?(?:visa\s+|immigration\s+)?sponsor(?:ship)?",
            rf"\b(?:does|do|did|is|are)\s*{_NOT}\s+(?:currently\s+)?(?:(?:offer|provide|support)(?:ing)?\s+"
            r"(?:[\w\-]+\s+){0,2}sponsorship|sponsor(?:ing)?\b)",
            r"\bno\s+(?:visa\s+|immigration\s+|h-?1b\s+|employment\s+)?sponsorship",
            r"\bnot\s+(?:currently\s+)?(?:offering|providing)\s+(?:[\w\-]+\s+){0,2}sponsorship",
            r"\bnot\s+(?:currently\s+)?sponsoring\b",
            # "we are unable to support visa applications", "does not provide relocation or visa support"
            rf"{_NEG}\s+(?:be\s+)?(?:able\s+to\s+|in\s+a\s+position\s+to\s+)?(?:currently\s+)?"
            rf"(?:provid|offer|support|sponsor|process|assist|accommodat|file|petition|transfer|facilitat)\w*"
            rf"\s+(?:[\w\-]+[\s/]+){{0,4}}?(?:{_IMM})",
            # "sponsorship will not be provided", "sponsorship is unavailable", "Visa sponsorship: none"
            rf"\bsponsorships?\s+(?:[\w\-]+\s+){{0,3}}?(?:will|can|could|shall|is|are|would|may)\s*{_NOT}\s+(?:be\s+)?"
            r"(?:provided|offered|available|possible|supported|considered|granted|extended)",
            r"\bsponsorships?\s+(?:is\s+|are\s+)?(?:not\s+|un)(?:available|offered|provided|possible|supported)",
            r"\bsponsorship\s*(?:available|offered|provided)?\s*[:\-–]\s*"
            r"(?:no|none|not\s+(?:available|offered|provided))\b",
            # "does not qualify for sponsorship", "not eligible / ineligible for visa sponsorship"
            r"\b(?:not|n['’]t|never)\s+qualify\s+for\s+(?:[\w\-]+\s+){0,2}?sponsorship",
            r"\b(?:not\s+eligible|ineligible)\s+for\s+(?:[\w\-]+\s+){0,2}?sponsorship",
            # "candidates requiring H-1B ... will not be considered", "F-1 students are not eligible"
            rf"(?:{_IMM})[^;]{{0,100}}?(?:{_EXCL})",
            # "not open to candidates on student visas", "we cannot consider applicants who need sponsorship"
            rf"(?:{_EXCL_BEFORE})[^;]{{0,80}}?(?:{_IMM})",
            # "must not require sponsorship now or in the future"
            rf"\b(?:not|never)\s+(?:now\s+or\s+in\s+the\s+future\s+)?(?:require|need)\s+"
            rf"(?:[\w\-]+[\s/]+){{0,4}}?(?:{_IMM})",
            # "authorized to work ... without (the need for) sponsorship / a visa"
            r"\bwithout\s+(?:the\s+need\s+(?:for|of)\s+|(?:requiring|needing)\s+|"
            r"(?:any\s+)?(?:need|requirement)\s+(?:for|of)\s+)?(?:an?\s+|any\s+|the\s+)?"
            r"(?:current\s+or\s+future\s+|future\s+|now\s+or\s+in\s+the\s+future\s+|further\s+|additional\s+)?"
            r"(?:(?:visa|employer|immigration|company|employment|work|h-?1-?b)[\s\-]+)*"
            r"(?:sponsorship|sponsoring|visas?|h-?1-?bs?)\b",
            r"\bmust\s+be\s+(?:legally\s+)?authori[sz]ed\s+to\s+work\b[^;\n]{0,160}?\bwithout\b",
            r"\bauthori[sz](?:ed|ation)\s+to\s+work\b[^;]{0,100}?\bfor\s+any\s+(?:\w+\s+)?employer\b",
            r"\bunrestricted\s+(?:\w+\s+){0,2}?(?:work|employment)\s+authori[sz]ation\b",
            r"\bunrestricted\s+(?:legal\s+)?(?:right|authori[sz]ation)\s+to\s+work\b",
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
            rf"\bcitizens?\s+of\s+{_USA}",
            rf"\bcitizenship\s+(?:in|of)\s+{_USA}",
            r"\bmust\s+(?:be|hold|have|possess)\s+(?:an?\s+)?(?:\w+\s+)?citizen(?:ship)?\b",
            r"\bcitizenship\s+(?:is\s+)?(?:required|mandatory)",
            r"\brequires?\s+(?:\w+\s+)?citizenship",
            r"\bcitizens?\s+only\b",
            r"\bonly\s+(?:\w+\s+){0,2}?citizens\b",
            rf"\b(?:{_US}|united\s+states)\s+nationals\b",
            rf"\b(?:{_US}|united\s+states)\s+national\b(?=\s*(?:$|[.,;:)!?]|(?:or|and|only|status)\b))",
            r"\bcitizens?\s*(?:or|and|/)\s*nationals?\b",
        ),
        "Review: the listing states a citizenship requirement. Read the exact wording above. " + _NOT_LEGAL,
        unless=_rx(r"\bnot\s+(?:required|necessary|needed|mandatory|a\s+requirement)\b", r"\bneed\s+not\b(?!\s+apply)",
                   rf"\b(?:does|do|did)\s*{_NOT}\s+(?:need|require|have\s+to)\b",
                   r"\bno\s+(?:\w+\s+)?citizenship\s+requirement"),
        eeo_guard=True,
    ),
    _Rule(
        "permanent_residency_required", BLOCKER,
        _rx(r"\bgreen[\s\-]+cards?\b", r"\b(?:lawful\s+)?permanent\s+residen(?:t|ts|ce|cy)\b", r"(?-i:\bLPRs?\b)"),
        "Review: the listing states a permanent-residency (green card) requirement. Read the exact wording "
        "above. " + _NOT_LEGAL,
        unless=_rx(r"(?:green[\s\-]+cards?|permanent\s+residen\w*)\s+"
                   r"(?:sponsorship|process|application|petition|support)",
                   r"sponsor\w*\s+(?:for\s+|of\s+)?(?:a\s+|your\s+|the\s+)?(?:green\s+card|permanent\s+residen)",
                   r"\bnot\s+(?:required|necessary|needed|mandatory)\b", r"\bneed\s+not\b(?!\s+apply)",
                   rf"\b(?:does|do|did)\s*{_NOT}\s+(?:need|require|have\s+to)\b"),
        eeo_guard=True,
    ),
    _Rule(
        "clearance_required", BLOCKER,
        _rx(
            r"\bsecurity\s+clearances?\b",
            r"\bts\s*[/\-]\s*sci\b", r"\bsci\s+(?:eligib\w*|access|clearance)\b",
            r"\b(?:top\s+)?secret\s+(?:security\s+|level\s+)?clearances?\b", r"\btop\s+secret\b",
            r"\bclearances?\s+(?:is\s+|are\s+)?(?:required|mandatory|needed|necessary)\b",
            r"\b(?:active|current|interim|existing|valid)\s+(?:[\w\-]+[\s/]+){0,2}?clearances?\b",
            r"\b(?:active|current|interim)\s+(?:dod\s+)?(?:secret|ts)\b",
            r"\b(?:hold|possess)\s+(?:an?\s+)?(?:active\s+|current\s+|interim\s+)?(?:dod\s+)?secret\b",
            r"\bsecret[\s\-]level\b",
            r"\bpublic\s+trust\s+(?:clearance|position|level|designation|background|investigation|eligib\w*|"
            r"suitability|is\s+required|required)\b",
            r"\b(?:obtain|maintain|hold|possess|active|current|pass|attain|requires?|eligib\w*\s+for)\s+"
            r"(?:an?\s+|the\s+)?(?:\w+\s+)?public\s+trust\b(?!\s+(?:in|of|and|with|through)\b)",
            r"\bpolygraph\b", r"\b(?:full[\s\-]scope|ci|counter[\s\-]?intelligence|lifestyle)\s+poly\b",
            r"\bclearable\b", r"\bclearance[\s\-]+eligib\w*",
            r"\beligib\w*\s+(?:for|to\s+(?:obtain|receive|hold))\s+(?:an?\s+|the\s+)?"
            r"(?:[\w\-]+[\s/]+){0,3}?clearances?\b",
            r"\b(?:obtain|maintain|acquire|attain|receive|hold|possess)\s+(?:and\s+maintain\s+)?(?:an?\s+|the\s+)?"
            r"(?:[\w\-]+[\s/]+){0,3}?clearances?\b",
            r"\b(?:federal|government)\s+background\s+investigation\b",
        ),
        "Review: the listing states a security-clearance requirement. Read the exact wording above. " + _NOT_LEGAL,
        unless=_rx(*_CLEARANCE_GUARD_NEG),
        override=_rx(
            # "candidates with no active clearance will not be considered"
            rf"\b(?:no|without|lack(?:s|ing)?)\s+(?:an?\s+|the\s+|any\s+)?(?:[\w\-]+[\s/]+){{0,2}}?clearances?\b"
            rf"[^;]{{0,60}}?(?:{_EXCL})",
            # "a clearance is not required, but you must be able to obtain one"
            r"\bclearances?\b[^;.]{0,80}?\b(?:but|however|though|although)\b[^;.]{0,80}?"
            r"(?:\b(?:obtain|acquire|attain|be\s+granted|eligib\w*\s+(?:for|to\s+(?:obtain|receive|hold)))\s+"
            r"(?:one|it|(?:an?\s+|the\s+)?(?:[\w\-]+\s+){0,2}?clearances?)\b|\bclearable\b)",
        ),
        veto=_non_security_clearance,
    ),
    _Rule(
        "export_control", BLOCKER,
        re.compile(
            r"\bitar\b|(?-i:\bEAR\b)|\bexport[\s\-]+(?:control(?:s|led)?|restrict(?:ed|ions?)|regulations?)\b|"
            r"\bexport\s+administration\s+regulations?\b|\binternational\s+traffic\s+in\s+arms\b|\bdeemed\s+exports?\b|"
            rf"(?<![A-Za-z]){_US}\s+persons?\b|\b(?:22|15)\s*c\.?f\.?r\.?(?=\s*(?:part\s*)?\d)",
            re.I,
        ),
        "Review: the listing states export-control conditions (ITAR / EAR / U.S. person). Read the exact "
        "wording above. " + _NOT_LEGAL,
        unless=_rx(r"not\s+subject\s+to\s+(?:\w+\s+)?export", r"\bno\s+export[\s\-]+control"),
    ),
    _Rule(
        "work_authorization_required", INFO,
        _rx(
            r"\b(?:must|should|needs?\s+to|required\s+to|have\s+to)\s+(?:already\s+)?be\s+(?:currently\s+|legally\s+|"
            r"fully\s+|already\s+|permanently\s+)*(?:authori[sz]ed|eligible|permitted)\s+to\s+(?:legally\s+)?"
            r"(?:work|be\s+employed|accept\s+employment)\b",
            rf"\b(?:authori[sz]ed|eligible|permitted|legally\s+(?:able|allowed))\s+to\s+(?:legally\s+)?"
            rf"(?:work|be\s+employed)\s+in\s+{_USA}",
            rf"\b(?:legal\s+)?(?:right|eligibility|authori[sz]ation)\s+to\s+work\s+in\s+{_USA}",
            r"\b(?:work|employment)\s+authori[sz]ation\s+(?:\w+\s+){0,3}?(?:required|mandatory|needed)\b",
            r"\brequires?\s+(?:\w+\s+){0,2}?(?:work|employment)\s+authori[sz]ation\b",
        ),
        "Review: the listing states a work-authorization requirement. Compare this with your own "
        "work-authorization situation. " + _NOT_LEGAL,
        veto=_onboarding_paperwork,
        eeo_guard=True,
        suppressed_by=("no_sponsorship",),
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


def _clause_at(breaks: list[tuple[int, int]], length: int, pos: int) -> tuple[int, int]:
    """Span of the clause containing ``pos`` (``breaks`` are the clause-separator spans)."""
    lo, hi = 0, length
    for b_start, b_end in breaks:
        if b_end <= pos:
            lo = b_end
        elif b_start > pos:
            hi = b_start
            break
        else:  # the match starts inside a separator ("but you must ...")
            lo = b_start
    return lo, hi


def _in_eeo_list(sentence: str, start: int, end: int) -> bool:
    """True when ``sentence[start:end]`` is an item of an equal-opportunity list, not a requirement.

    "without regard to race, color, ..., U.S. citizenship status" is a list item; "U.S. citizenship is
    required and all applicants are considered without regard to race" is a requirement.
    """
    if _REQ_AFTER.match(sentence, end):
        return False
    last = None
    for last in _EEO_TRIGGER.finditer(sentence, 0, start):
        pass
    if last is None:
        return False
    gap = sentence[last.end():start]
    return len(gap) <= 250 and not _LIST_BREAK.search(gap)


def _rule_match(rule: _Rule, sentence: str, breaks: list[tuple[int, int]]) -> re.Match[str] | None:
    """First match of ``rule`` that survives its veto, its clause-level guard and the EEO-list check."""
    if rule.override is not None:
        m = rule.override.search(sentence)
        if m is not None and not (rule.veto is not None and rule.veto(sentence, m)):
            return m
    pos = 0
    while (m := rule.pattern.search(sentence, pos)) is not None:
        pos = m.start() + 1
        if rule.veto is not None and rule.veto(sentence, m):
            continue
        if rule.unless is not None:
            lo, hi = _clause_at(breaks, len(sentence), m.start())
            if rule.unless.search(sentence, lo, hi):
                continue
        if rule.eeo_guard and _in_eeo_list(sentence, m.start(), m.end()):
            continue
        return m
    return None


def _clause_breaks(sentence: str) -> list[tuple[int, int]]:
    breaks = [m.span() for m in _CLAUSE_BREAK.finditer(sentence)]
    extra = []
    for m in _AND.finditer(sentence):
        if any(b_start < m.end() and m.start() < b_end for b_start, b_end in breaks):
            continue
        lo, _ = _clause_at(breaks, len(sentence), m.start())
        if _PREDICATE.search(sentence, lo, m.start()):
            extra.append(m.span())
    return sorted(breaks + extra)


def _rule_flags(sentence: str) -> list[EligibilityFlag]:
    breaks = _clause_breaks(sentence)
    found: list[EligibilityFlag] = []
    for rule in RULES:
        m = _rule_match(rule, sentence, breaks)
        if m is not None:
            found.append(_flag(rule.code, rule.severity, excerpt_around(sentence, m.start(), m.end()), rule.message))
    codes = {f.code for f in found}
    redundant = {r.code for r in RULES if codes.intersection(r.suppressed_by)}
    return [f for f in found if f.code not in redundant]


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
