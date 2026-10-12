"""Normalization of raw listings into ``models.NormalizedJob``.

Everything here is deterministic and conservative: fields are set only from
explicit signals in the listing. Work arrangement comes only from structured
workplace fields or the words "Remote"/"Hybrid"/"On-site" in the location;
sponsorship / work-authorization text is copied verbatim, never inferred;
salary is never invented (see ``salary.py``).
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from job_agent.discovery.salary import SalaryInfo, salary_from_text
from job_agent.models import EmploymentType, NormalizedJob, SalaryPeriod, WorkArrangement
from job_agent.utils.time import parse_iso, to_iso

MAX_DESCRIPTION_CHARS = 60_000
MAX_QUALIFICATIONS = 40
MAX_EXCERPT_CHARS = 2_000
#: Bump when normalization output changes, so re-derived fields are not reported as listing changes.
NORMALIZER_VERSION = 1


class InvalidListing(ValueError):
    """A listing that cannot be normalized (e.g. missing id or title). Skipped and counted."""


# --------------------------------------------------------------------------- #
# Text cleanup
# --------------------------------------------------------------------------- #
# Control characters, zero-width and bidi-override characters (display spoofing).
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩﻿]")
_SPACE_RE = re.compile(r"[ \t  -   　]+")
_BULLET_RE = re.compile(r"^\s*(?:[-*•·▪◦‣–—]|\d{1,2}[.)])\s+")
_LINKEDIN_TAG_RE = re.compile(r"^#?LI-[A-Za-z0-9]+$")


def clean_inline(value: Any) -> str:
    """Single-line cleanup for titles, locations, employer names."""
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = _CONTROL_RE.sub("", text)
    text = _SPACE_RE.sub(" ", text.replace("\r", " ").replace("\n", " "))
    return text.strip()


class _TextExtractor(HTMLParser):
    _BLOCK = {
        "p", "div", "section", "article", "header", "footer", "main", "aside", "blockquote", "pre", "table",
        "tr", "ul", "ol", "dl", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "form", "fieldset",
        "figure", "figcaption", "address", "nav", "details", "summary",
    }
    _SKIP = {"script", "style", "noscript", "template", "head", "svg", "iframe", "object"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0
        # "- " is emitted with the first text inside an <li>, so <li><p>text</p></li> keeps its bullet.
        self._bullet_pending = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n")
            self._bullet_pending = True
        elif tag in self._BLOCK:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.parts.append("\n")
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK or tag == "li":
            self.parts.append("\n")
            if tag == "li":
                self._bullet_pending = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._bullet_pending and data.strip():
            self.parts.append("\n- ")
            self._bullet_pending = False
        self.parts.append(data)


def _tidy_lines(text: str) -> str:
    text = _CONTROL_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    out: list[str] = []
    for raw in text.split("\n"):
        line = _SPACE_RE.sub(" ", raw).strip()
        if _BULLET_RE.match(line):
            line = "- " + _BULLET_RE.sub("", line, count=1).strip()
        if line in {"-", "- "} or _LINKEDIN_TAG_RE.match(line):
            continue
        if not line:
            if out and out[-1] != "":
                out.append("")
            continue
        if line.startswith("- ") and len(out) >= 2 and out[-1] == "" and out[-2].startswith("- "):
            out.pop()  # keep consecutive bullets together
        out.append(line)
    result = "\n".join(out).strip()
    if len(result) > MAX_DESCRIPTION_CHARS:
        result = result[:MAX_DESCRIPTION_CHARS].rstrip() + "\n[...truncated...]"
    return result


def html_to_text(markup: str | None) -> str:
    """Convert (untrusted) HTML to plain text with ``- `` bullets and one blank line between blocks."""
    if not markup:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except (AssertionError, ValueError):  # pragma: no cover - HTMLParser is very lenient
        return _tidy_lines(re.sub(r"<[^>]+>", " ", markup))
    return _tidy_lines("".join(parser.parts))


def plain_to_text(text: str | None) -> str:
    return _tidy_lines(text or "")


# --------------------------------------------------------------------------- #
# Keys for fingerprints / matching
# --------------------------------------------------------------------------- #
_CORPORATE_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "plc", "gmbh", "ag", "sa",
    "lp", "llp", "pbc", "pty", "bv", "nv", "srl", "sas",
}


def normalize_key(text: str | None) -> str:
    """Lowercase, accents folded, punctuation removed, whitespace collapsed.

    Periods and apostrophes are deleted (``"Sr."`` -> ``sr``, ``"Ph.D."`` -> ``phd``);
    other punctuation becomes a space (``"AI/ML"`` -> ``ai ml``).
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", clean_inline(text))
    folded = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()
    folded = re.sub(r"[.'’`]", "", folded)
    folded = re.sub(r"[\W_]+", " ", folded)
    return " ".join(folded.split())


def normalize_employer(name: str | None) -> str:
    tokens = normalize_key(name).split()
    if tokens and tokens[0] == "the" and len(tokens) > 1:
        tokens = tokens[1:]
    while len(tokens) > 1 and tokens[-1] in _CORPORATE_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def normalize_title(title: str | None) -> str:
    return normalize_key(title)


def normalize_location(location: str | None) -> str | None:
    key = normalize_key(location)
    return key or None


def contains_phrase(haystack_key: str, phrase: str) -> bool:
    """Word-boundary phrase match on normalized keys (``"us"`` does not match ``"austin"``)."""
    needle = normalize_key(phrase)
    if not needle or not haystack_key:
        return False
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack_key) is not None


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def description_similarity(a: str | None, b: str | None) -> float:
    """Token-set Jaccard similarity of two descriptions (0..1)."""
    ta = set(normalize_key(a).split())
    tb = set(normalize_key(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# --------------------------------------------------------------------------- #
# URLs
# --------------------------------------------------------------------------- #
_TRACKING_PARAMS = {
    "gh_src", "source", "src", "ref", "referrer", "referer", "lever-source", "lever-source[]", "lever-origin",
    "lever-via", "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "_hsenc", "_hsmi", "trk", "trackingid",
}


def canonicalize_url(url: str | None) -> str | None:
    """Canonical form for duplicate detection.

    Lowercases scheme/host, drops default ports, fragments, tracking parameters
    (``utm_*``, ``gh_src``, ``source``, ``ref``, ``lever-source``, ...) and the
    trailing slash. Parameters that identify the job (``gh_jid``, ``ashby_jid``,
    ``id``, ...) are kept and sorted.
    """
    if not url:
        return None
    text = clean_inline(url)
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not parts.hostname:
        return None
    host = parts.hostname.lower()
    try:
        port = parts.port
    except ValueError:
        return None
    netloc = host if port is None or (scheme, port) in {("http", 80), ("https", 443)} else f"{host}:{port}"
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query)), ""))


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #
def to_utc_iso(value: Any) -> str | None:
    """ISO-8601 UTC from an ISO string, a datetime, or epoch seconds/milliseconds."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 10_000_000_000 else value
            return to_iso(datetime.fromtimestamp(seconds, tz=timezone.utc))
        if isinstance(value, datetime):
            dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            return to_iso(dt)
        text = str(value).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            text += "T00:00:00+00:00"
        return to_iso(parse_iso(text))
    except (ValueError, OverflowError, OSError):
        return None


# --------------------------------------------------------------------------- #
# Work arrangement / employment type / experience
# --------------------------------------------------------------------------- #
_WORKPLACE_MAP = {
    "remote": WorkArrangement.REMOTE,
    "hybrid": WorkArrangement.HYBRID,
    "onsite": WorkArrangement.ONSITE,
    "on site": WorkArrangement.ONSITE,
    "on-site": WorkArrangement.ONSITE,
    "in office": WorkArrangement.ONSITE,
    "inoffice": WorkArrangement.ONSITE,
    "office": WorkArrangement.ONSITE,
}


def detect_work_arrangement(*, workplace_type: str | None = None, is_remote: bool | None = None,
                            locations: Iterable[str | None] = ()) -> WorkArrangement:
    """Explicit signals only: structured workplace type, then ``isRemote``, then location words."""
    if workplace_type:
        mapped = _WORKPLACE_MAP.get(clean_inline(workplace_type).lower().replace("_", " "))
        if mapped:
            return mapped
    if is_remote is True:
        return WorkArrangement.REMOTE
    keys = [normalize_key(loc) for loc in locations if loc]
    if any(contains_phrase(k, "remote") for k in keys):
        return WorkArrangement.REMOTE
    if any(contains_phrase(k, "hybrid") for k in keys):
        return WorkArrangement.HYBRID
    if any(contains_phrase(k, p) for k in keys for p in ("on site", "onsite", "in office")):
        return WorkArrangement.ONSITE
    return WorkArrangement.UNKNOWN


_EMPLOYMENT_PATTERNS: list[tuple[re.Pattern[str], EmploymentType]] = [
    (re.compile(r"\b(intern|interns|internship|co op|coop)\b"), EmploymentType.INTERNSHIP),
    (re.compile(r"\b(part time|parttime)\b"), EmploymentType.PART_TIME),
    (re.compile(r"\b(contract|contractor|freelance|consultant contract)\b"), EmploymentType.CONTRACT),
    (re.compile(r"\b(temporary|temp|fixed term|seasonal)\b"), EmploymentType.TEMPORARY),
    (re.compile(r"\b(full time|fulltime|permanent|regular)\b"), EmploymentType.FULL_TIME),
]
_INTERN_TITLE_RE = re.compile(r"\b(intern|interns|internship|internships|co op)\b")


def map_employment_value(value: str | None) -> EmploymentType:
    key = normalize_key(value)
    if not key:
        return EmploymentType.UNKNOWN
    for pattern, etype in _EMPLOYMENT_PATTERNS:
        if pattern.search(key):
            return etype
    return EmploymentType.UNKNOWN


def detect_employment_type(*, structured: Iterable[str | None] = (), title: str | None = None) -> EmploymentType:
    """Structured values (metadata / commitment / employmentType) first; an "Intern" title wins."""
    if _INTERN_TITLE_RE.search(normalize_key(title)):
        return EmploymentType.INTERNSHIP
    for value in structured:
        etype = map_employment_value(value)
        if etype != EmploymentType.UNKNOWN:
            return etype
    return EmploymentType.UNKNOWN


_LEVEL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(senior|sr|staff|principal|lead|director|head of|vp|vice president|chief)\b"), "senior"),
    (re.compile(r"\b(intern|interns|internship|co op)\b"), "internship"),
    (re.compile(r"\b(new grads?|new graduates?|graduate|graduates|university grad|college grad|campus)\b"),
     "new_grad"),
    (re.compile(r"\b(entry level|entry)\b"), "entry"),
    (re.compile(r"\b(early career)\b"), "early_career"),
    (re.compile(r"\b(junior|jr)\b"), "entry"),
    (re.compile(r"\b(associate)\b"), "associate"),
]
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
}
_NUM = r"(\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")"
_YEARS_RES = [
    re.compile(
        rf"\b{_NUM}\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*{_NUM}\s*\+?\s*)?(?:or more\s+)?(?:years?|yrs?)\b"
        rf"(?:'|’)?[^.\n;]{{0,60}}?\bexperience",
        re.I,
    ),
    re.compile(rf"\bexperience\b[^.\n;]{{0,30}}?\b(?:of\s+)?{_NUM}\s*\+?\s*(?:years?|yrs?)\b", re.I),
]


def _num(token: str) -> int:
    return int(token) if token.isdigit() else _NUMBER_WORDS[token.lower()]


def extract_years_experience(texts: Iterable[str]) -> tuple[int | None, str | None]:
    """Smallest explicitly stated minimum years of experience, with its verbatim snippet."""
    best: tuple[int, str] | None = None
    for text in texts:
        if not text:
            continue
        for regex in _YEARS_RES:
            for m in regex.finditer(text):
                years = _num(m.group(1))
                if years > 30:
                    continue
                if best is None or years < best[0]:
                    best = (years, m.group(0).strip()[:200])
    return (best[0], best[1]) if best else (None, None)


def detect_experience_level(title: str | None, years: int | None) -> str | None:
    key = normalize_key(title)
    for pattern, level in _LEVEL_PATTERNS:
        if pattern.search(key):
            return level
    if years is None:
        return None
    if years <= 1:
        return "entry"
    if years <= 4:
        return "mid"
    return "senior"


# --------------------------------------------------------------------------- #
# Qualifications
# --------------------------------------------------------------------------- #
_PREFERRED_HEADING_RE = re.compile(
    r"\b(preferred|nice to haves?|nice-to-haves?|bonus points?|bonus if|is a bonus|a plus|pluses|desired|"
    r"desirable|ideally|extra credit|great to have|good to have|it would be (great|nice)|what we value|"
    r"even better|additional qualifications)\b",
    re.I,
)
_REQUIRED_HEADING_RE = re.compile(
    r"\b(requirements?|qualifications?|what you(?:'ll| will)? need|what you need|"
    r"what (?:we're|we are|you're|you are) looking for|what we require|what you(?:'ll| will)? bring|"
    r"you (?:have|bring|should have|will have|might be a fit)|you'll have|must[- ]haves?|who you are|about you|"
    r"your (?:background|experience|skills|profile|qualifications)|skills (?:and|&) (?:experience|qualifications)|"
    r"experience (?:and|&) skills|key skills|minimum|basic|required|essential|what it takes)\b",
    re.I,
)
_OTHER_HEADING_RE = re.compile(
    r"\b(responsibilit|what you(?:'ll| will)? do|what you'll be doing|about|benefits?|perks|why|who we are|"
    r"the role|the team|role overview|overview|compensation|salary|pay|location|our |how we|equal|eeo|"
    r"day to day|day-to-day|what we offer|life at|culture|mission|values|in this role|the opportunity|"
    r"your impact|impact|team|join us|apply|interview|process|disclaimer|privacy|accommodation)",
    re.I,
)


def _classify_heading(line: str) -> str | None:
    if line.startswith("- "):
        return None
    text = line.replace("’", "'").strip()
    had_colon = text.endswith(":")
    text = text.rstrip(":").strip()
    words = text.split()
    if not words or len(text) > 80 or len(words) > 10 or text.endswith((".", "!", "?", ";", ",")):
        return None
    if _PREFERRED_HEADING_RE.search(text):
        return "preferred"
    if _REQUIRED_HEADING_RE.search(text):
        return "required"
    if _OTHER_HEADING_RE.search(text):
        return "other"
    if had_colon:
        return "subheading"  # e.g. "Technical skills:" inside a requirements section
    long_words = [w for w in words if len(w) > 3 and w[0].isalpha()]
    title_like = bool(long_words) and all(w[0].isupper() for w in long_words)
    if text.isupper() or (title_like and len(words) <= 6):
        return "other"
    return None


def extract_qualifications(text: str) -> tuple[list[str], list[str]]:
    """Bullets under required-style vs preferred-style headings. Nothing is guessed."""
    required: list[str] = []
    preferred: list[str] = []
    section: str | None = None
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        kind = _classify_heading(line)
        if kind == "subheading":
            continue
        if kind is not None:
            section = kind if kind in {"required", "preferred"} else None
            continue
        if section and line.startswith("- "):
            item = line[2:].strip()[:500]
            target = required if section == "required" else preferred
            if item and item not in target and len(target) < MAX_QUALIFICATIONS:
                target.append(item)
    return required, preferred


# --------------------------------------------------------------------------- #
# Legal / eligibility excerpts (verbatim)
# --------------------------------------------------------------------------- #
_VISA_RE = re.compile(r"\b(visas?|h-?1b|green card|work permits?|employment pass|e-?verify)\b", re.I)
_IMMIGRATION_RE = re.compile(r"\bimmigration\b", re.I)
_SPONSOR_RE = re.compile(r"\bsponsor(?:ship|ships|ing|ed|s)?\b", re.I)
# "sponsor" alone is often business language ("executive sponsors"); require eligibility context.
_SPONSOR_CONTEXT_RE = re.compile(
    r"(visa|immigra|h-?1b|employ|authori[sz]|status|legal|eligib|\b(not|no|unable|cannot|can't|won't|"
    r"unavailable)\b|future)",
    re.I,
)
_WORK_AUTH_RE = re.compile(
    r"(authori[sz]ed to work|work authori[sz]ation|authori[sz]ation to work|eligib(?:le|ility) to work|"
    r"right to work|legally (?:authori[sz]ed|eligible|able)|\bcitizens?(?:hip)?\b|\bu\.?s\.? persons?\b|"
    r"\bpermanent residen(?:t|ts|cy)\b|green card|security clearance|\bclearances?\b|export[- ]control|"
    r"export (?:administration )?regulations|export laws|\bitar\b|\bpublic trust\b)",
    re.I,
)
# "citizen developer" / "citizen data scientist" are technology terms, not eligibility requirements.
_CITIZEN_TECH_RE = re.compile(r"\bcitizens? (develop|data|scien|automat|integrat|journalis|led)", re.I)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\"'“])")


def iter_sentences(text: str) -> Iterable[str]:
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        for sentence in _SENTENCE_SPLIT_RE.split(line):
            sentence = sentence.strip().removeprefix("- ").strip()
            if sentence:
                yield sentence


def _join_excerpts(items: list[str]) -> str | None:
    if not items:
        return None
    out: list[str] = []
    total = 0
    for item in items:
        if total + len(item) > MAX_EXCERPT_CHARS:
            break
        out.append(item)
        total += len(item) + 1
    return "\n".join(out) if out else items[0][:MAX_EXCERPT_CHARS]


def extract_legal_excerpts(text: str) -> tuple[str | None, str | None]:
    """(sponsorship_info, work_authorization_requirements): verbatim sentences only."""
    sponsorship: list[str] = []
    work_auth: list[str] = []
    for sentence in iter_sentences(text):
        clipped = sentence[:500]
        auth_match = _WORK_AUTH_RE.search(_CITIZEN_TECH_RE.sub(" ", sentence))
        sponsor_match = (
            _VISA_RE.search(sentence)
            or (_SPONSOR_RE.search(sentence) and _SPONSOR_CONTEXT_RE.search(sentence))
            or (_IMMIGRATION_RE.search(sentence) and auth_match)
        )
        if sponsor_match and clipped not in sponsorship:
            sponsorship.append(clipped)
        if auth_match and clipped not in work_auth:
            work_auth.append(clipped)
    return _join_excerpts(sponsorship), _join_excerpts(work_auth)


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #
def _require(value: Any, what: str) -> str:
    text = clean_inline(value)
    if not text:
        raise InvalidListing(f"missing {what}")
    return text


def build_normalized_job(
    *,
    source_name: str,
    source_identifier: str,
    external_id: Any,
    employer: Any,
    title: Any,
    location_parts: Sequence[str | None] = (),
    description_html: str | None = None,
    description_plain: str | None = None,
    workplace_type: str | None = None,
    is_remote: bool | None = None,
    employment_hints: Sequence[str | None] = (),
    department: Any = None,
    requisition_id: Any = None,
    posted_at: Any = None,
    posted_at_source: str | None = None,
    closing_at: Any = None,
    job_url: Any = None,
    application_url: Any = None,
    structured_salary: SalaryInfo | None = None,
    raw_metadata: dict[str, Any] | None = None,
) -> NormalizedJob:
    """Normalize one listing. Raises ``InvalidListing`` when id/title/employer are missing."""
    ext_id = _require(external_id, "id")
    job_title = _require(title, "title")
    employer_name = _require(employer, "employer")

    seen_locations: list[str] = []
    for part in location_parts:
        loc = clean_inline(part)
        if loc and loc not in seen_locations:
            seen_locations.append(loc)
    location = "; ".join(seen_locations) or None

    if description_html:
        description = html_to_text(description_html)
    else:
        description = plain_to_text(description_plain)
    required, preferred = extract_qualifications(description)
    sponsorship, work_auth = extract_legal_excerpts(description)

    years, years_text = extract_years_experience(required or [_without_preferred(description)])
    meta: dict[str, Any] = {k: v for k, v in (raw_metadata or {}).items() if v not in (None, "", [], {})}
    meta["normalizer_version"] = NORMALIZER_VERSION
    if years is not None:
        meta["years_experience_min"] = years
        meta["years_experience_text"] = years_text
    if posted_at_source:
        meta["posted_at_source"] = posted_at_source

    salary = structured_salary or salary_from_text(description)
    if salary is not None:
        meta["salary_source"] = salary.source
        if salary.period_inferred:
            meta["salary_period_inferred"] = True

    job_url_text = clean_inline(job_url) or None
    app_url_text = clean_inline(application_url) or None
    return NormalizedJob(
        source_name=source_name,
        source_identifier=source_identifier,
        external_id=ext_id,
        requisition_id=clean_inline(requisition_id) or None,
        employer=employer_name,
        title=job_title,
        location=location,
        work_arrangement=detect_work_arrangement(workplace_type=workplace_type, is_remote=is_remote,
                                                 locations=seen_locations),
        employment_type=detect_employment_type(structured=employment_hints, title=job_title),
        experience_level=detect_experience_level(job_title, years),
        department=clean_inline(department) or None,
        description_text=description,
        required_qualifications=required,
        preferred_qualifications=preferred,
        salary_min=salary.min if salary else None,
        salary_max=salary.max if salary else None,
        salary_currency=salary.currency if salary else None,
        salary_period=salary.period if salary else SalaryPeriod.UNKNOWN,
        salary_is_base=salary.is_base if salary else None,
        salary_text=salary.text if salary else None,
        posted_at=to_utc_iso(posted_at),
        closing_at=to_utc_iso(closing_at),
        job_url=job_url_text,
        application_url=app_url_text,
        sponsorship_info=sponsorship,
        work_authorization_requirements=work_auth,
        raw_metadata=meta,
    )


def _without_preferred(text: str) -> str:
    """Description text minus bullets under preferred-style headings (for years-of-experience)."""
    out: list[str] = []
    in_preferred = False
    for line in text.split("\n"):
        kind = _classify_heading(line.strip()) if line.strip() else None
        if kind is not None and kind != "subheading":
            in_preferred = kind == "preferred"
        if not in_preferred:
            out.append(line)
    return "\n".join(out)
