"""Eligibility flags from verbatim listing text.

A flag records that the *listing states* something (sponsorship, work
authorization, citizenship, clearance, export control, graduation/start timing)
and quotes the sentence verbatim. Flags are prompts for human review: they never
decide, and their messages never claim, whether the candidate is or is not
eligible. Legal questions are always answered by the human (CLAUDE.md rule 2).

Text is folded with ``security.textnorm.normalize_text`` first (full-width and
look-alike letters, invisible characters), and the excerpt is cut from that same
normalized text, so what is stored is exactly what was checked.

Exceptions (negations such as "not required", equal-opportunity wording) are
scoped to the clause / list item that holds the match, so they cannot hide a
requirement stated elsewhere in the same sentence.

The specific rules are heuristics and cannot list every paraphrase. A
conservative fallback (``eligibility_statement``, severity ``review``) therefore
flags any sentence that *mentions* an eligibility topic (sponsorship, visas,
immigration, work authorization, citizenship, nationality, permanent residency,
clearance, polygraph, classified access, export control, U.S. person) that no
specific rule explained, unless the mention is an item of an equal-opportunity
list, a "we sponsor" statement, Form I-9 onboarding wording, a whole-sentence
"X is not required", or a non-eligibility sense of the word ("Visa and
Mastercard", "customs clearance", "U.S. national accounts").
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
from job_agent.security.textnorm import normalize_text

if TYPE_CHECKING:  # pragma: no cover
    from job_agent.profile import CandidateProfile

INFO, REVIEW, BLOCKER = "info", "review", "blocker"
SEVERITY_RANK = {BLOCKER: 0, REVIEW: 1, INFO: 2}

#: Code of the fallback flag for eligibility wording that no specific rule explained.
FALLBACK_CODE = "eligibility_statement"

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
#: "sponsorship", tolerating the common misspellings "sponser", "sponsor ship".
_SPONSORSHIP = r"spons[eo]r[\s\-]?ships?"

#: Immigration / visa vocabulary. "visa status" / "immigration status" are excluded because they are the
#: wording of equal-opportunity statements ("we will not consider your immigration status").
_IMM = (
    r"(?:\b(?:visa|employer|immigration|employment|h-?1-?b)[\s\-]+)?\bspons[eo]r(?:[\s\-]?ships?|ing|ed)?\b"
    r"|\bvisas?\b(?!\s+status)|\bh-?1-?bs?\b|\bimmigration\b(?!\s+status)|\bnon[\s\-]?immigrants?\b|\bwork\s+permits?\b"
    r"|\bemployment[\s\-]based\s+(?:immigration|green\s+card|petition)\w*"
    r"|(?-i:\b(?:OPT|CPT|EAD)\b)|\bstem[\s\-]opt\b"
    r"|(?-i:\b(?:F|J|H|L|O)-1[AB]?\b|\bH-4\b|\bE-3\b)|\b[fj]-?1\s+(?:visa|student|status)\w*"
    r"|\btn\s+(?:visa|status)\b"
    r"|\bpetitions?\s+(?:[\w\-]+\s+){0,3}?on\s+(?:[\w’']+\s+){0,2}?behalf\b|\bi-?129\b|\bi-?140\b"
    r"|\blabor\s+certification\b"
)
#: What a candidate might need help with to be employable ("assistance with work authorization").
_AUTH_TERMS = (
    r"\b(?:work|employment)\s+(?:authori[sz]ation|permits?|visas?|status|eligibility|rights?)\b"
    r"|\bimmigration\b|\bvisas?\b|\bh-?1-?bs?\b|\bgreen\s+cards?\b|\bemployab\w*|\bright\s+to\s+work\b"
    r"|\b(?:legal|immigration)\s+status\b"
)
#: "... will not be considered" / "... are not eligible" / "... need not apply".
_EXCL = (
    rf"\b(?:(?:will|would|shall|can|may|could)\s*{_NOT}|cannot|can\s+not|not)\s+be\s+"
    r"(?:considered|eligible|accepted|hired|reviewed)\b"
    rf"|\bneed\s+not\s+apply\b|\b(?:should|must|do|please)\s*{_NOT}\s+apply\b"
    r"|\bnot\s+eligible\b|\bineligible\b|\bdisqualif\w*|\b(?:are|is|be|being|get)\s+excluded\b"
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
    r"\bno\s+(?:[\w\-]+[\s/\-]+){0,2}?(?:clearances?|polygraphs?)\b",
    r"\b(?:clearances?|polygraphs?)\s+(?:is\s+|are\s+)?not\s+(?:required|needed|necessary|mandatory)",
    rf"\b(?:does|do|did|will|would)\s*{_NOT}\s+require\s+(?:a\s+|an\s+|any\s+)?(?:\w+\s+)?(?:clearances?|polygraphs?)",
    r"\b(?:clearances?|polygraphs?)\s*[:\-–]\s*(?:not\s+required|none|n/?a)\b",
)
#: Requirement wording right after a match: it is a requirement, not an EEO category.
_REQ_AFTER = re.compile(r"[^,;]{0,30}?\b(?:required|mandatory|requirement|only)\b", re.I)
#: Word right before "clearance" that makes it a non-security clearance ("customs clearance").
_NON_SECURITY_BEFORE = re.compile(
    r"\b(?:customs|medical|regulatory|tax|fda|ceiling|credit|airspace|flight|drug|import|export|trade|legal|music|"
    r"rights|copyright|financial|height|road|check|cheque|payment|bank|title|environmental|site|land|vendor|ethics|"
    r"conflicts?|irb|editorial|sample|insurance|pharmacy|pre[\s\-]?flight|baggage|border|port|cargo|freight|"
    r"shipments?|shipping|snow|ground|vertical|overhead|minimum|wheel)[\s\-]+$", re.I)
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
            rf"\bno\s+(?:visa\s+|immigration\s+|h-?1b\s+|employment\s+)?{_SPONSORSHIP}",
            r"\bnot\s+(?:currently\s+)?(?:offering|providing)\s+(?:[\w\-]+\s+){0,2}sponsorship",
            r"\bnot\s+(?:currently\s+)?sponsoring\b",
            # "we are unable to support visa applications", "does not provide relocation or visa support"
            rf"{_NEG}\s+(?:be\s+)?(?:able\s+to\s+|in\s+a\s+position\s+to\s+)?(?:currently\s+)?"
            rf"(?:provid|offer|support|sponsor|process|assist|accommodat|file|petition|transfer|facilitat)\w*"
            rf"\s+(?:[\w\-]+[\s/]+){{0,4}}?(?:{_IMM})",
            # "sponsorship will not be provided", "sponsorship is unavailable", "Visa sponsorship: none"
            rf"\b{_SPONSORSHIP}\s+(?:[\w\-]+\s+){{0,3}}?(?:will|can|could|shall|is|are|would|may)\s*{_NOT}\s+(?:be\s+)?"
            r"(?:provided|offered|available|possible|supported|considered|granted|extended)",
            rf"\b{_SPONSORSHIP}\s+(?:is\s+|are\s+)?(?:not\s+|un)(?:available|offered|provided|possible|supported)",
            rf"\b{_SPONSORSHIP}\s*(?:available|offered|provided)?\s*[:\-–]\s*"
            r"(?:no|none|not\s+(?:available|offered|provided))\b",
            # "visas are not sponsored", "H-1B transfers cannot be processed"
            rf"\b(?:visas?|h-?1-?bs?|work\s+permits?|green\s+cards?|immigration\s+(?:petitions?|filings?|support))"
            rf"\s+(?:[\w\-]+\s+){{0,2}}?(?:will|can|could|shall|is|are|would|may)\s*{_NOT}\s+(?:be\s+)?"
            r"(?:sponsored|supported|provided|offered|available|possible|processed|transferred|filed)\b",
            # "does not qualify for sponsorship", "not eligible / ineligible for visa sponsorship"
            r"\b(?:not|n['’]t|never)\s+qualify\s+for\s+(?:[\w\-]+\s+){0,2}?sponsorship",
            r"\b(?:not\s+eligible|ineligible)\s+for\s+(?:[\w\-]+\s+){0,2}?sponsorship",
            # "candidates requiring H-1B ... will not be considered", "F-1 students are not eligible",
            # "applicants who need an employer to file a petition on their behalf cannot be considered"
            rf"(?:{_IMM})[^;]{{0,100}}?(?:{_EXCL})",
            # "not open to candidates on student visas", "we cannot consider applicants who need sponsorship"
            rf"(?:{_EXCL_BEFORE})[^;]{{0,80}}?(?:{_IMM})",
            # "must not require sponsorship now or in the future"
            rf"\b(?:not|never)\s+(?:now\s+or\s+in\s+the\s+future\s+)?(?:require|need)\s+"
            rf"(?:[\w\-]+[\s/]+){{0,4}}?(?:{_IMM})",
            # "must not now or in the future need assistance with work authorization",
            # "can work without our support for immigration paperwork"
            rf"(?:\b(?:not|never|without|no)\b|n['’]t\b)[^;]{{0,60}}?\b(?:assistance|help|support|aid)\b"
            rf"[^;]{{0,40}}?(?:{_AUTH_TERMS})",
            # "No visa support available", "no immigration assistance"
            r"\bno\s+(?:(?:visa|immigration|h-?1-?bs?|work[\s\-]permits?|relocation)(?:\s*(?:,|/|&|or|and)\s*|\s+))+"
            r"(?:support|assistance|help|processing|transfers?|sponsorship|petitions?|filings?|paperwork)\b",
            r"\b(?:visa|immigration|work[\s\-]permit)\s+(?:support|assistance|help|processing|transfers?)\s+"
            r"(?:is\s+|are\s+)?(?:not\s+|un)(?:available|offered|provided|possible|supported)\b",
            r"\b(?:visa|immigration)\s+(?:support|assistance)\s*[:\-–]\s*(?:no|none|not\s+(?:available|offered|provided))\b",
            # "authorized to work ... without (the need for) sponsorship / a visa"
            r"\bwithout\s+(?:the\s+need\s+(?:for|of)\s+|(?:requiring|needing)\s+|"
            r"(?:any\s+)?(?:need|requirement)\s+(?:for|of)\s+)?(?:an?\s+|any\s+|the\s+)?"
            r"(?:current\s+or\s+future\s+|future\s+|now\s+or\s+in\s+the\s+future\s+|further\s+|additional\s+)?"
            r"(?:(?:visa|employer|immigration|company|employment|work|h-?1-?b)[\s\-]+)*"
            rf"(?:{_SPONSORSHIP}|sponsoring|visas?|h-?1-?bs?)\b",
            r"\bmust\s+be\s+(?:legally\s+)?authori[sz]ed\s+to\s+work\b[^;\n]{0,160}?\bwithout\b",
            r"\bauthori[sz](?:ed|ation)\s+to\s+work\b[^;]{0,100}?\bfor\s+any\s+(?:\w+\s+)?employer\b",
            # "must be able to work for any employer without restriction"
            r"\b(?:work|employment)\b[^;]{0,40}?\bfor\s+any\s+(?:[\w\-]+\s+)?employer\b",
            r"\b(?:able|authori[sz]ed|eligible|permitted|right|legally|allowed|entitled)\s+to\s+(?:legally\s+)?work\b"
            r"[^;]{0,60}?\bwithout\s+(?:any\s+)?(?:\w+\s+)?restrictions?\b",
            rf"\bwork\s+in\s+{_USA}[^;]{{0,30}}?\bwithout\s+(?:any\s+)?(?:\w+\s+)?restrictions?\b",
            r"\bunrestricted\s+(?:\w+\s+){0,2}?(?:work|employment)\s+(?:authori[sz]ation|rights?|status|eligibility|permits?)\b",
            r"\bunrestricted\s+(?:legal\s+)?(?:right|authori[sz]ation|ability|permission|eligibility)\s+to\s+work\b",
            # "permanent work authorization that does not expire", "lifetime right to work in the US"
            r"\b(?:permanent|lifetime|indefinite|non[\s\-]?expiring|unlimited|perpetual|open[\s\-]ended)\s+(?:\w+\s+){0,2}?"
            r"(?:work|employment)\s+(?:authori[sz]ation|rights?|permits?|eligibility)\b",
            r"\b(?:permanent|lifetime|indefinite|unlimited|perpetual)\s+(?:legal\s+)?(?:right|authori[sz]ation|permission|"
            r"eligibility)\s+to\s+(?:live\s+and\s+)?work\b",
            r"\b(?:work|employment)\s+authori[sz]ation\s+(?:that|which)\s+(?:does\s*(?:not|n['’]t)|will\s+not|won['’]t|"
            r"cannot|never)\s+expire",
            # Spanish: "No ofrecemos patrocinio de visa", "sin patrocinio"
            r"\b(?:no|sin)\s+(?:[\w\-]+\s+){0,3}?patrocin\w*",
            r"\bpatrocin\w*[^.;]{0,40}?\bno\s+(?:est[aá]\s+disponible|disponible|se\s+ofrece|ofrecemos|es\s+posible)",
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
            # a U.S. passport is issued only to U.S. nationals
            rf"\b(?:hold|have|possess|obtain|carry|present)\s+(?:an?\s+)?(?:valid\s+|current\s+|active\s+|unexpired\s+)?"
            rf"(?:{_US}|american|united\s+states)\s+passports?\b",
            rf"\b(?:valid|current|unexpired)\s+(?:{_US}|american|united\s+states)\s+passports?\b",
            rf"\b(?:{_US}|american|united\s+states)\s+passports?\s+(?:holders?|required|is\s+required|only|mandatory)\b",
            rf"\b{_US}\s+birth\s+certificates?\b",
            # "USC" (U.S. citizen) in recruiter shorthand, not the university
            r"\b(?:be|are|is)\s+(?:an?\s+)?(?-i:USCs?)\b(?![\s\-]+(?:students?|graduates?|grads?|alum\w*|faculty|staff|"
            r"employees?|affiliat\w*|trojans?|campus|degree)\b)",
            r"(?-i:\bUSCs?\b)\s*(?:/|,|&|or|and)\s*(?-i:(?:GCs?|LPRs?|green)\b)",
            r"(?-i:\bUSCs?\b)\s+(?:only|required|holders?)\b",
            r"\bonly\s+(?-i:USCs?)\b",
            # place of birth
            r"\b(?:must|should|need\s+to|required\s+to|have\s+to)\s+(?:have\s+been\s+|be\s+)?born\s+in\b",
            r"\b(?:native|natural)[\s\-]born\b",
            rf"\bborn\s+in\s+{_USA}[^;]{{0,30}}?\b(?:only|required|requirement)\b",
            r"\bonly\s+(?:\w+\s+){0,2}?born\s+in\b",
            r"\bamericans?\s+only\b", r"\bonly\s+(?:hire\s+|hiring\s+|consider\s+|accept\s+)?americans\b",
            r"\bmust\s+be\s+(?:an\s+)?american\s*(?=$|[.,;:)!?])",
            # Spanish: "Se requiere ciudadanía estadounidense"
            r"\bciudadan[íi]a\s+(?:estadounidense|americana|de\s+(?:los\s+)?(?:ee\.?\s?uu\.?|estados\s+unidos))",
            r"\b(?:se\s+requiere|requiere|requisito|debe\s+(?:ser|tener|poseer)|deben\s+(?:ser|tener|poseer))\s+"
            r"(?:la\s+|una?\s+)?(?:\w+\s+)?ciudadan\w*",
            r"\bciudadan[oa]s?\s+(?:estadounidenses?|american[oa]s?|de\s+(?:los\s+)?(?:ee\.?\s?uu\.?|estados\s+unidos))",
        ),
        "Review: the listing states a citizenship requirement. Read the exact wording above. " + _NOT_LEGAL,
        unless=_rx(r"\bnot\s+(?:required|necessary|needed|mandatory|a\s+requirement)\b", r"\bneed\s+not\b(?!\s+apply)",
                   rf"\b(?:does|do|did)\s*{_NOT}\s+(?:need|require|have\s+to)\b",
                   r"\bno\s+(?:\w+\s+)?citizenship\s+requirement"),
        eeo_guard=True,
    ),
    _Rule(
        "permanent_residency_required", BLOCKER,
        _rx(r"\bgreen[\s\-]+cards?\b", r"\b(?:lawful\s+)?permanent\s+residen(?:t|ts|ce|cy)\b", r"(?-i:\bLPRs?\b)",
            r"(?-i:\bGCs?\b)\s*(?:holders?|only|required|/|or|and)", r"(?-i:\bUSCs?\b)\s*(?:/|,|&|or)\s*(?-i:GCs?\b)",
            r"\bresidencia\s+permanente\b", r"\bresidentes?\s+permanentes?\b"),
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
            r"\bposition\s+of\s+public\s+trust\b",
            r"\bsuitability\s+(?:determination|investigation|adjudication)s?\b",
            r"\bpolygraph\b", r"\b(?:full[\s\-]scope|ci|counter[\s\-]?intelligence|lifestyle)\s+poly\b",
            r"\bclearable\b", r"\bclearance[\s\-]+eligib\w*",
            r"\beligib\w*\s+(?:for|to\s+(?:obtain|receive|hold))\s+(?:an?\s+|the\s+)?"
            r"(?:[\w\-]+[\s/]+){0,3}?clearances?\b",
            r"\b(?:obtain|maintain|acquire|attain|receive|hold|possess)\s+(?:and\s+maintain\s+)?(?:an?\s+|the\s+)?"
            r"(?:[\w\-]+[\s/]+){0,3}?clearances?\b",
            # investigations: "federal government security investigation", "Tier 3 investigation", SF-86
            r"\b(?:federal|government|dod|doe|opm|dcsa|single[\s\-]scope|tier\s*[1-5][rs]?)\s+(?:[\w\-]+\s+){0,2}?"
            r"(?:background|security|suitability)\s+(?:re)?investigations?\b",
            r"\btier\s*[1-5][rs]?\s+(?:background\s+|security\s+|suitability\s+)?(?:re)?(?:investigation|clearance|"
            r"adjudication)s?\b",
            r"\b(?:pass|obtain|complete|undergo|adjudicat\w*\s+(?:of\s+)?)\s*(?:an?\s+|the\s+)?tier\s*[1-5][rs]?\b",
            r"(?-i:\bT[35]R?\b)\s+(?:re)?investigation", r"\bssbi\b", r"\bsf[\s\-]?86\b", r"\bsf[\s\-]?85p?\b",
            r"\be-?qip\b",
            # classified access, DOE Q / L access authorization, special access programs
            r"\bclassified\s+(?:information|materials?|data|documents?|networks?|systems?|programs?|environments?|"
            r"spaces?|facilit\w*|work|projects?|contracts?|missions?|level)\b",
            r"\b(?:access\s+to|handl\w*)\s+classified\b",
            r"(?-i:\b(?:DOE\s+)?[QL]\b)[\s\-]+(?:access\s+authori[sz]ation|clearance|cleared)\b",
            r"(?-i:\bDOE\b)[\s\-]+(?:(?-i:[QL])\s+)?(?:access\s+authori[sz]ation|(?:security\s+)?clearance)\b",
            r"(?-i:\bDOE\s+[QL]\b)",
            r"\bspecial\s+access\s+programs?\b",
            r"\b(?:currently\s+|actively\s+)?cleared\s+(?:candidates?|professionals?|personnel|individuals?|applicants?|"
            r"engineers?|analysts?|staff)\b",
            r"\b(?:ts|top\s+secret|secret|sci)[\s\-/]+cleared\b",
        ),
        "Review: the listing states a security-clearance requirement. Read the exact wording above. " + _NOT_LEGAL,
        unless=_rx(*_CLEARANCE_GUARD_NEG),
        override=_rx(
            # "candidates with no active clearance will not be considered"
            rf"\b(?:no|without|lack(?:s|ing)?)\s+(?:an?\s+|the\s+|any\s+)?(?:[\w\-]+[\s/]+){{0,2}}?clearances?\b"
            rf"[^;]{{0,60}}?(?:{_EXCL})",
            # "a clearance is not required, but you must be able to obtain one"
            r"\bclearances?\b[^;.]{0,80}?\b(?:but|however|though|although)\b[^;.]{0,80}?"
            r"(?:\b(?:obtain|acquire|attain|get|be\s+granted|eligib\w*\s+(?:for|to\s+(?:obtain|receive|hold)))\s+"
            r"(?:one|it|(?:an?\s+|the\s+)?(?:[\w\-]+\s+){0,2}?clearances?)\b|\bclearable\b)",
        ),
        veto=_non_security_clearance,
    ),
    _Rule(
        "export_control", BLOCKER,
        _rx(
            r"\bitar\b|(?-i:\bEAR\b)|\bexport[\s\-]+(?:control(?:s|led)?|restrict(?:ed|ions?)|regulations?)\b|"
            r"\bexport\s+administration\s+regulations?\b|\binternational\s+traffic\s+in\s+arms\b|\bdeemed\s+exports?\b|"
            rf"(?<![A-Za-z]){_US}\s+persons?\b|\b(?:22|15)\s*c\.?f\.?r\.?(?=\s*(?:part\s*)?\d)",
            # OFAC / embargo: "candidates must not be nationals of embargoed countries"
            r"\b(?:nationals?|citizens?|residents?|persons?)\s+(?:of|from|in)\s+(?:an?\s+|any\s+|the\s+)?"
            r"(?:[\w\-]+[\s,]+){0,3}?(?:embargoed|sanctioned|ofac)\b",
            r"\b(?:embargoed|sanctioned)\s+(?:countr\w*|nations?|jurisdictions?|destinations?)\b[^;]{0,60}?"
            r"\b(?:nationals?|citizens?|residents?)\b",
            r"\bofac\b[^;]{0,80}?\b(?:nationals?|citizens?)\b",
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
        _rx(r"\b(?:visa\s+|immigration\s+|h-?1-?b\s+|green\s+card\s+)?sponsorship\s+(?:is\s+|will\s+be\s+|can\s+be\s+|"
            r"may\s+be\s+)?(?:available|offered|provided|possible|supported)\b",
            r"\bwe\s+(?:will\s+|can\s+|do\s+)?sponsor\b", r"\bwill\s+sponsor\b", r"\bable\s+to\s+sponsor\b",
            r"\b(?:offer|offers|offering|provide|provides|providing)\s+(?:(?:visa|immigration|h-?1-?b|green\s+card|work|"
            r"employment[\s\-]based|employer)\s+){1,2}sponsorship",
            r"\b(?:support|sponsor|process|accept)\w*\s+(?:h-?1-?b|visa)\s+transfers?\b",
            r"\bopen\s+to\s+sponsor(?:ing|ship)\b", r"\beligible\s+for\s+(?:visa\s+|h-?1b\s+)sponsorship"),
        "Review: the listing states that visa sponsorship may be available. Confirm the details with the "
        "employer. " + _NOT_LEGAL,
        unless=_rx(r"\b(?:no|not|unable|cannot|without|never)\b", r"n['’]t\b"),
    ),
)


# --------------------------------------------------------------------------- #
# Fallback: eligibility wording that no specific rule explained
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Topic:
    label: str
    #: "work" (sponsorship / visas / immigration / work authorization), "residency", "citizenship",
    #: "clearance" or "export".
    family: str
    pattern: re.Pattern[str]
    #: ``veto(sentence, match)`` -> True when the word is used in a non-eligibility sense.
    veto: Callable[[str, re.Match[str]], bool] | None = None


_IMM_CONTEXT = re.compile(
    r"\b(?:visas?|immigra\w*|h-?1-?bs?|green\s+cards?|work\s+(?:authori[sz]ation|permits?)|employment[\s\-]based|"
    r"petitions?|non[\s\-]?immigrant|authori[sz]ed\s+to\s+work|right\s+to\s+work)\b|(?-i:\b(?:OPT|CPT|EAD)\b)", re.I)
_BUSINESS_SPONSORSHIP_BEFORE = re.compile(
    r"\b(?:executive|business|leadership|stakeholder|project|program|programme|event|corporate|brand|sports?|team|"
    r"community|athlete|title|media|marketing|commercial|partnership|charity|charitable|nonprofit|conference|senior|"
    r"c-suite|management|internal|cross[\s\-]functional)(?:\s+(?:and|or|&)\s+\w+)?[\s\-]+$", re.I)
_BUSINESS_SPONSORSHIP_AFTER = re.compile(
    r"[\s\-]*(?:deals?|agreements?|contracts?|packages?|revenue|sales|activations?|assets?|opportunit\w*|programs?|"
    r"partners?|partnerships?|inventory|rights|marketing|portfolio|proposals?|decks?|pipeline|renewals?|for\s+(?:the|our|"
    r"this|an?)\s+(?:event|program|project|initiative|team|conference|summit))\b", re.I)


def _business_sponsorship(sentence: str, m: re.Match[str]) -> bool:
    """"executive sponsorship", "sponsorship deals": a business sense, unless immigration words are nearby."""
    if _IMM_CONTEXT.search(sentence):
        return False
    before = sentence[max(0, m.start() - 40):m.start()]
    return bool(_BUSINESS_SPONSORSHIP_BEFORE.search(before) or _BUSINESS_SPONSORSHIP_AFTER.match(sentence, m.end()))


_CARD_AFTER = re.compile(
    r"[\s\-]*(?:(?:and|or|&|/|,)\s*(?:mastercard|master\s*card|amex|american\s+express|discover|paypal)|card|cards|"
    r"credit|debit|network|payments?|inc\b|checkout|prepaid|direct|net\b)", re.I)
_CARD_BEFORE = re.compile(r"(?:mastercard|master\s*card|amex|american\s+express|discover|paypal)\s*(?:,|and|or|/|&)\s*$",
                          re.I)
_VISA_CONTEXT_AFTER = re.compile(
    r"\s*(?:[:(\-–]|sponsor\w*|status|support|assistance|help|holders?|requirements?|applications?|processing|"
    r"transfers?|petitions?|types?|categor\w*|restrictions?|issues?|stamps?|renewals?|extensions?|expir\w*|needs?|"
    r"required|candidates?|workers?|employees?|students?|interviews?|appointments?|paperwork|filings?|costs?|fees?|"
    r"policy|policies|options?|is\s+required|will\s+not|cannot|not\b)", re.I)
_VISA_CONTEXT_BEFORE = re.compile(
    r"(?:\b(?:h-?1-?b|h1b|work|student|employment|employer|immigrant|non-?immigrant|immigration|tourist|business|tn|"
    r"e-?3|o-?1|l-?1|j-?1|f-?1|dependent|employment[\s\-]based|sponsored|sponsor|no)\s+|"
    r"\b(?:on|hold|holds|holding|need|needs|needing|require|requires|requiring|without|any|valid|current|temporary)\s+"
    r"(?:an?\s+)?)$", re.I)


def _visa_brand(sentence: str, m: re.Match[str]) -> bool:
    """"Visa and Mastercard", "clients include Visa": the card brand, not an immigration visa."""
    word = m.group(0)
    if not word.lower().startswith("visa"):
        return False
    before = sentence[max(0, m.start() - 40):m.start()]
    if _CARD_AFTER.match(sentence, m.end()) or _CARD_BEFORE.search(before):
        return True
    if word.lower() != "visa" or word == "visa":
        return False  # lowercase "visa", or the plural "visas" (the brand is singular)
    # "Visa" / "VISA": the company unless the words around it are about immigration
    return not (_VISA_CONTEXT_AFTER.match(sentence, m.end()) or _VISA_CONTEXT_BEFORE.search(before))


_RETAIL_AFTER = re.compile(
    r"[\s\-]*(?:sales?|items?|merchandise|inventory|racks?|pric\w*|markdowns?|events?|performance|stock|products?|"
    r"goods|rates?|strateg\w*|centers?|outlets?|sections?|cadence|optimi[sz]\w*|planning|forecast\w*|velocity|"
    r"sell[\s\-]?through|aisles?|shelf|shelves|bins?|tags?|deals?|discounts?|and\s+settlement|(?:and|&|/)\s*markdowns?|"
    r"of\s+(?:goods|shipments?|cargo|checks?|payments?|funds|trades?|inventory|merchandise|snow|debris))\b", re.I)
_RETAIL_BEFORE = re.compile(
    r"\b(?:markdowns?|promo\w*|pricing|seasonal|sale|end[\s\-]of[\s\-]season|inventory|liquidation|outlet|"
    r"discount\w*)\s*(?:and|&|/|,|or)?\s*$", re.I)
_SECURITY_CUE = re.compile(
    r"\b(?:security|secret|sci|dod|doe|government|federal|polygraph|classified|public\s+trust|investigation)\b"
    r"|(?-i:\bTS\b)", re.I)


def _non_security_clearance_word(sentence: str, m: re.Match[str]) -> bool:
    """"customs clearance", "markdown and clearance", "clearance merchandise"."""
    before = sentence[max(0, m.start() - 40):m.start()]
    if _NON_SECURITY_BEFORE.search(before):
        return True
    if _SECURITY_CUE.search(sentence):
        return False
    return bool(_RETAIL_AFTER.match(sentence, m.end()) or _RETAIL_BEFORE.search(before))


_CITIZEN_OTHER_BEFORE = re.compile(
    r"\b(?:corporate|good|global|digital|responsible|community|model|senior|private|ordinary|fellow|everyday|average|"
    r"engaged|informed|first[\s\-]class|second[\s\-]class|world)[\s\-]+$"
    r"|\bserv(?:e|es|ed|ing)\s+(?:(?:the|our|its|local|all|\w+['’]s)\s+)*(?:\w+\s+(?:and|&)\s+)?$", re.I)
_CITIZEN_OTHER_AFTER = re.compile(
    r"[\s\-]*(?:developers?|data\s+scien\w*|scien\w*|journalis\w*|engagement|services?|experiences?|facing|portals?|"
    r"centric|participation|feedback|satisfaction|requests?|inquir\w*|complaints?|outreach|advisory|panels?|"
    r"budget\w*|reporting|apps?|insights?|surveys?|journeys?|analysts?)\b", re.I)


def _other_citizen_sense(sentence: str, m: re.Match[str]) -> bool:
    """"citizen developers", "corporate citizenship", "serve the citizens"."""
    before = sentence[max(0, m.start() - 40):m.start()]
    return bool(_CITIZEN_OTHER_BEFORE.search(before) or _CITIZEN_OTHER_AFTER.match(sentence, m.end()))


def _other_public_trust(sentence: str, m: re.Match[str]) -> bool:
    """"maintain public trust in our institutions", "the public trust"."""
    before = sentence[max(0, m.start() - 20):m.start()]
    return bool(re.search(r"\b(?:the|our|their|its|of|public['’]s)\s+$", before, re.I)
                or re.match(r"\s+(?:in|of|and|with|through|that|we)\b", sentence[m.end():], re.I))


_PERSON_ORIGIN = re.compile(r"\b(?:nationals?|nationality|citizens?|citizenship|residents?|born|country\s+of)\b", re.I)


def _sanctions_duty(sentence: str, m: re.Match[str]) -> bool:
    """"Perform OFAC screening": sanctions as job duties, unless the sentence is about who may be hired."""
    return not _PERSON_ORIGIN.search(sentence)


def _sports_team(sentence: str, m: re.Match[str]) -> bool:
    before = sentence[max(0, m.start() - 20):m.start()]
    return bool(re.search(r"\bwashington\s+$", before, re.I))


_TOPICS: tuple[_Topic, ...] = (
    _Topic("sponsorship", "work", _rx(
        rf"\b{_SPONSORSHIP}\b", r"\bpatrocin\w*",
        r"\b(?:need|needs|needing|require|requires|requiring)\s+(?:[\w’']+\s+){0,3}?to\s+spons[eo]r\b",
        r"\bspons[eo]r(?:ing|ed)?\s+(?:you|them|candidates?|applicants?|individuals?|employees?|foreign\s+\w+|"
        r"international\s+\w+|anyone|workers?|new\s+hires?)\b",
        r"(?:\b(?:not|never|cannot|unable\s+to)|n['’]t)\s+(?:\w+\s+){0,2}?spons[eo]r\b(?!\s+(?:the|our|events?|"
        r"meetups?|hackathons?|conferences?|teams?|projects?|initiatives?|programs?)\b)",
    ), _business_sponsorship),
    _Topic("visas", "work", _rx(
        r"\bvisas?\b", r"\bh-?1-?bs?\b", r"(?-i:\b(?:OPT|CPT|EAD)\b)", r"\bstem[\s\-]opt\b",
        r"(?-i:\b(?:F|J|H|L|O)-1[AB]?\b|\bH-4\b|\bE-3\b)", r"\btn\s+(?:visa|status)\b",
    ), _visa_brand),
    _Topic("immigration", "work", _rx(
        r"\bimmigra\w*", r"\bnon[\s\-]?immigrants?\b", r"\bi-?129\b", r"\bi-?140\b", r"\bi-?485\b",
        r"\blabor\s+certification\b", r"\bperm\s+(?:process|labor|application|filing)\b",
        r"\bpetitions?\s+(?:[\w\-]+\s+){0,3}?on\s+(?:[\w’']+\s+){0,2}?behalf\b",
    )),
    _Topic("work authorization", "work", _rx(
        r"\b(?:work|employment)\s+(?:authori[sz]ation|permits?|eligibility)\b", r"\bwork\s+rights\b",
        r"\bright\s+to\s+(?:live\s+and\s+)?work\b",
        r"\b(?:authori[sz]ed|eligible|permitted|entitled|legally\s+(?:able|allowed|permitted))\s+to\s+(?:legally\s+)?"
        r"(?:work|be\s+employed|accept\s+employment)\b(?!\s+(?:overtime|remotely|from\s+home|hybrid|flexibl\w*|"
        r"part[\s\-]time|full[\s\-]time|on\s+(?:site|weekends?)|weekends?|nights?|shifts?|evenings?|holidays?|extra|"
        r"additional)\b)",
        r"\b(?:eligibility|authori[sz]ation|permission)\s+to\s+(?:legally\s+)?work\b",
        r"\bemployab\w*\s+in\b", r"\b(?:legal|lawful)\s+(?:right|status|permission)\s+to\s+(?:work|remain|reside|stay)\b",
        r"\b(?:legal|lawful|immigration)\s+status\b",
        r"\bpermiso\s+de\s+trabajo\b", r"\bautorizaci[oó]n\s+(?:de|para)\s+trabaj\w*",
    )),
    _Topic("permanent residency", "residency", _rx(
        r"\bgreen[\s\-]?cards?\b", r"\bpermanent\s+residen\w*", r"(?-i:\bLPRs?\b)",
        r"(?-i:\bGCs?\b)\s*(?:holders?|only|required|/|or)", r"\bresidencia\s+permanente\b",
        r"\bresidentes?\s+permanentes?\b",
    )),
    _Topic("citizenship", "citizenship", _rx(r"\bcitizen(?:s|ship|ships)?\b", r"\bciudadan\w*"), _other_citizen_sense),
    _Topic("nationality", "citizenship", _rx(
        r"\bnationality\b", r"\bnationalities\b", r"\bnacionalidad\w*",
        r"\b(?:foreign|dual|third[\s\-]country|non[\s\-]?u\.?s\.?|united\s+states|american)\s+nationals?\b",
        rf"\b{_US}\s+national\b(?=\s*(?:$|[.,;:)!?]|(?:or|and|only|status)\b))",
        r"\bnational\s+of\b", r"\bnationals\b", r"\bamericans?\s+only\b", r"\bonly\s+americans\b",
    ), _sports_team),
    _Topic("place of birth", "citizenship", _rx(
        r"\b(?:place|country)\s+of\s+birth\b", r"\bbirthplace\b", r"\b(?:native|natural)[\s\-]born\b",
        rf"\bborn\s+in\s+{_USA}\b", r"\bbirth\s+certificates?\b",
    )),
    _Topic("a passport", "citizenship", _rx(r"\bpassports?\b")),
    _Topic("a security clearance", "clearance", _rx(
        r"\bclearances?\b", r"\bclearable\b",
        r"\bcleared\s+(?:candidates?|professionals?|personnel|individuals?|applicants?|engineers?|analysts?|staff)\b",
        r"\bspecial\s+access\s+programs?\b",
    ), _non_security_clearance_word),
    _Topic("a polygraph", "clearance", _rx(
        r"\bpolygraph\w*", r"\b(?:full[\s\-]scope|ci|counter[\s\-]?intelligence|lifestyle)\s+poly\b")),
    _Topic("classified information", "clearance", _rx(
        r"\bclassified\s+(?:information|materials?|data|documents?|networks?|systems?|programs?|environments?|"
        r"spaces?|facilit\w*|work|projects?|contracts?|missions?|level)\b",
        r"\b(?:access\s+to|handl\w*)\s+classified\b",
    )),
    _Topic("a background investigation", "clearance", _rx(
        r"\b(?:obtain|pass|passing|undergo|maintain|complete|submit\s+to|subject\s+to|favorabl\w*)\b[^.;]{0,40}?"
        r"\b(?:background|security|suitability)\s+(?:investigation|adjudication|determination)s?\b",
        r"\b(?:federal|government)\s+(?:[\w\-]+\s+){0,2}?(?:background|security|suitability)\s+investigations?\b",
        r"\bsuitability\s+(?:determination|investigation|adjudication)s?\b",
        r"\btier\s*[1-5][rs]?\s+(?:background\s+)?(?:re)?investigations?\b",
        r"\bsf[\s\-]?8[56]p?\b", r"\be-?qip\b", r"\bssbi\b",
    )),
    _Topic("public trust", "clearance", _rx(r"\bpublic\s+trust\b"), _other_public_trust),
    _Topic("export control", "export", _rx(
        r"\bexport[\s\-]+(?:control\w*|restrict\w*|regulat\w*|licen[cs]\w*|administration|complian\w*|classif\w*|"
        r"authori[sz]ation)",
        r"\bdeemed\s+exports?\b", r"\binternational\s+traffic\s+in\s+arms\b",
    )),
    _Topic("ITAR / EAR", "export", _rx(r"\bitar\b", r"(?-i:\bEAR\b)", r"\b(?:22|15)\s*c\.?f\.?r\.?(?=\s*(?:part\s*)?\d)")),
    _Topic("U.S. person status", "export", _rx(rf"(?<![A-Za-z]){_US}\s+persons?\b", r"\bunited\s+states\s+persons?\b")),
    _Topic("sanctions / embargoes", "export", _rx(r"\b(?:embargo(?:ed|es)?|sanctioned|ofac)\b"), _sanctions_duty),
)

#: Equal-opportunity / diversity wording that introduces a list of protected categories.
_EEO_LIST_TRIGGER = re.compile(
    _EEO_TRIGGER.pattern + r"|\bequal\s+(?:employment\s+)?opportunit\w*|\baffirmative\s+action\b|\beeo\b"
    r"|\b(?:of|from)\s+(?:all|any|every)\b|\bwelcom\w*|\bdivers\w*|\binclusi\w*", re.I)
#: For the fallback, exclusion words also end the list ("applicants of any nationality other than ...").
_EEO_LIST_BREAK = re.compile(
    _LIST_BREAK.pattern + r"|\b(?:other\s+than|except\w*|exclud\w*|unless|not|no|non|never|cannot|provided|if)\b"
    r"|n['’]t\b", re.I)
#: Requirement / exclusion wording right after a mention: not an EEO list item ("..., applicants on H-1B are excluded").
_EEO_REQ_AFTER = re.compile(
    rf"[^,;]{{0,40}}?(?:{_EXCL}|\b(?:excluded|required|mandatory|requirement|only|must|need\s+not)\b)", re.I)
#: "We will not consider your immigration status in hiring decisions" (anti-discrimination wording).
_EEO_STATUS = _rx(
    rf"\b(?:will|shall|do|does|would)\s*{_NOT}\s+(?:consider|use|ask\s+about|inquire\s+about|take\s+into\s+account)\s+"
    r"(?:your|their|an?\s+(?:applicant|candidate)['’]s|(?:applicants|candidates)['’])\s+(?:immigration|visa|citizenship)"
    r"\s+status\b",
    rf"^\s*(?:(?:your|their|an?\s+(?:applicant|candidate)['’]s)\s+)?(?:immigration|visa|citizenship)\s+status\s+"
    rf"(?:will|shall|is|does)\s*{_NOT}\s+(?:be\s+)?(?:considered|used|a\s+factor)\s+(?:in|when|for|during)\s+"
    r"(?:\w+\s+){0,2}?(?:hiring|employment|selection)\s+decisions?\b",
)
#: Form I-9 / E-Verify onboarding wording (only excuses the "work authorization" topic).
_ONBOARDING = _rx(
    r"\b(?:form\s+)?i-?9\b", r"\be-?verify\b", r"\bemployment\s+eligibility\s+verification\b",
    r"\bverify\s+(?:your\s+|their\s+|his\s+or\s+her\s+)?(?:identity\s+and\s+)?(?:employment\s+)?eligibility\s+to\s+work\b",
)
#: A whole sentence that only says "X is not required" ("No security clearance required.").
_SUBJ_WORD = (r"(?!(?:must|only|required?|requires|requirement|need\w*|but|however|unless|except|without|if|"
              r"preferred)\b)[A-Za-z][\w.\-/’']*")
_SUBJ = rf"(?:{_SUBJ_WORD}\s+){{0,5}}?{_SUBJ_WORD}"
_FOR_ROLE = r"(?:\s+(?:for|to)\s+(?:this|the)\s+(?:role|position|job|opening|opportunity))?"
_PURE_NEGATION = (
    re.compile(rf"(?:an?\s+|the\s+|any\s+)?{_SUBJ}\s+(?:is\s+|are\s+)?(?:(?:preferred|desired|helpful|a\s+plus)\s*,?\s*"
               rf"(?:but\s+)?)?not\s+(?:required|needed|necessary|mandatory|a\s+requirement){_FOR_ROLE}\s*[.!]?", re.I),
    re.compile(rf"no\s+{_SUBJ}\s+(?:is\s+|are\s+)?(?:required|needed|necessary){_FOR_ROLE}\s*[.!]?", re.I),
    re.compile(rf"{_SUBJ}\s*[:\-–]\s*(?:not\s+required|none|no|n/?a|not\s+applicable)\s*[.!]?", re.I),
    re.compile(rf"(?:this|the)\s+(?:role|position|job|opening)\s+(?:does\s*{_NOT}|will\s*{_NOT})\s+require\s+"
               rf"(?:an?\s+|any\s+)?{_SUBJ}\s*[.!]?", re.I),
)
#: Families for which a whole-sentence "not required" is accepted; for sponsorship wording it is ambiguous
#: ("Sponsorship: not required" can mean the candidate must not require it).
_NEGATABLE_FAMILIES = frozenset({"citizenship", "residency", "clearance", "export"})
#: Families a "we sponsor ..." statement explains.
_SPONSOR_FAMILIES = frozenset({"work", "residency"})
#: Words that turn a "we sponsor ..." clause into something more than a pure statement of sponsorship.
_CONDITION = re.compile(
    r"\b(?:must|only|requir\w*|need\w*|mandatory|not|no|never|cannot|unable|without|exclud\w*|ineligible|"
    r"disqualif\w*|restrict\w*|limited|unless|except|considered)\b|n['’]t\b", re.I)


def _mentions(sentence: str) -> list[tuple[int, int, _Topic]]:
    found: list[tuple[int, int, _Topic]] = []
    for topic in _TOPICS:
        for m in topic.pattern.finditer(sentence):
            if topic.veto is not None and topic.veto(sentence, m):
                continue
            found.append((m.start(), m.end(), topic))
    return sorted(found, key=lambda t: (t[0], t[1]))


def _explained(sentence: str, breaks: list[tuple[int, int]], start: int, end: int, topic: _Topic,
               info: list[tuple[_Rule, re.Match[str]]], pure_negation: bool) -> bool:
    """True when a topic mention needs no fallback flag (see the module docstring)."""
    for rule, m in info:
        if m.start() <= start < m.end() or start <= m.start() < end:
            return True  # the info flag quotes this very wording
        if rule.code == "sponsorship_available" and topic.family in _SPONSOR_FAMILIES:
            lo, hi = _clause_at(breaks, len(sentence), m.start())
            if lo <= start < hi and not _CONDITION.search(sentence[lo:m.start()] + " " + sentence[m.end():hi]):
                return True  # a pure "we sponsor H-1B visas" statement
    if not _EEO_REQ_AFTER.match(sentence, end) and _in_eeo_list(
            sentence, start, end, trigger=_EEO_LIST_TRIGGER, max_gap=600, list_break=_EEO_LIST_BREAK):
        return True
    if any(e.start() <= start < e.end() for e in _EEO_STATUS.finditer(sentence)):
        return True
    if topic.label == "work authorization" and _ONBOARDING.search(sentence):
        return True
    return pure_negation and topic.family in _NEGATABLE_FAMILIES


def _fallback_flag(sentence: str, breaks: list[tuple[int, int]],
                   matched: list[tuple[_Rule, re.Match[str]]]) -> EligibilityFlag | None:
    if any(rule.severity in {REVIEW, BLOCKER} for rule, _ in matched):
        return None  # the sentence is already quoted for review
    info = [(rule, m) for rule, m in matched if rule.severity == INFO]
    stripped = sentence.strip()
    pure_negation = any(p.fullmatch(stripped) for p in _PURE_NEGATION)
    open_mentions = [(s, e, t) for s, e, t in _mentions(sentence)
                     if not _explained(sentence, breaks, s, e, t, info, pure_negation)]
    if not open_mentions:
        return None
    labels: list[str] = []
    for _, _, topic in open_mentions:
        if topic.label not in labels:
            labels.append(topic.label)
    what = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]
    first_start, first_end, _ = open_mentions[0]
    return _flag(FALLBACK_CODE, REVIEW, excerpt_around(sentence, first_start, first_end),
                 f"Review: the listing mentions {what} — read the exact wording. " + _NOT_LEGAL)


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


def _in_eeo_list(sentence: str, start: int, end: int, *, trigger: re.Pattern[str] = _EEO_TRIGGER,
                 max_gap: int = 250, list_break: re.Pattern[str] = _LIST_BREAK) -> bool:
    """True when ``sentence[start:end]`` is an item of an equal-opportunity list, not a requirement.

    "without regard to race, color, ..., U.S. citizenship status" is a list item; "U.S. citizenship is
    required and all applicants are considered without regard to race" is a requirement.
    """
    if _REQ_AFTER.match(sentence, end):
        return False
    last = None
    for last in trigger.finditer(sentence, 0, start):
        pass
    if last is None:
        return False
    gap = sentence[last.end():start]
    return len(gap) <= max_gap and not list_break.search(gap)


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
    """Specific-rule flags for one (normalized) sentence, plus the fallback flag when wording is unexplained."""
    breaks = _clause_breaks(sentence)
    matched: list[tuple[_Rule, re.Match[str]]] = []
    for rule in RULES:
        m = _rule_match(rule, sentence, breaks)
        if m is not None:
            matched.append((rule, m))
    codes = {rule.code for rule, _ in matched}
    redundant = {r.code for r in RULES if codes.intersection(r.suppressed_by)}
    matched = [(rule, m) for rule, m in matched if rule.code not in redundant]
    found = [_flag(rule.code, rule.severity, excerpt_around(sentence, m.start(), m.end()), rule.message)
             for rule, m in matched]
    fallback = _fallback_flag(sentence, breaks, matched)
    if fallback is not None:
        found.append(fallback)
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
    validated ``JobAnalysis.sponsorship_statements``). Every text is normalized
    with ``normalize_text`` before it is checked, and excerpts are cut from the
    normalized text.
    """
    raw = [str(job.get(f) or "") for f in SOURCE_FIELDS] + [str(t) for t in extra_texts if t]
    texts = [normalize_text(t) for t in raw]
    internship = str(job.get("employment_type") or "").lower() == "internship" or bool(
        re.search(r"\bintern(?:ship)?s?\b", normalize_text(str(job.get("title") or "")), re.I))
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
