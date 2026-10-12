"""Classify application form fields and decide what may be filled automatically.

Deterministic rules only (no LLM decides anything here):

* personal fields come from ``profile.personal`` -- never a ``<placeholder>`` value;
* résumé / cover-letter uploads use the application's stored documents (PDF
  preferred). A cover letter (file or pasted text) is used only once you have
  approved it; an unapproved résumé is attached but flagged for review;
* education fields use the first (most recent) profile education entry verbatim;
* LEGAL / ELIGIBILITY questions -- any field whose label, name, placeholder, help
  text or options mention a legal topic (``security.legal_text.is_legal_question``)
  -- are always flagged (``is_legal_declaration`` + ``requires_review``), never
  drafted by the LLM, and filled *only* from a ``verified_answers`` entry marked
  ``is_legal_declaration: true`` whose pattern covers the whole question (no
  extra clause, negation, condition or other country) and whose answer exactly
  matches an available option (or the field is free text). Non-legal verified
  answers and ``work_authorization`` booleans are never used to answer them;
* EEO / voluntary self-identification is left blank for you;
* agreements / certifications / consents are never checked automatically;
* salary / start date / relocation / notice period need review unless a
  non-legal verified answer matches;
* open-ended questions need review; an optional local-LLM draft is stored as a
  *suggestion* (confidence ``uncertain``) and never entered;
* anything unrecognized needs review.

Only answers with confidence ``verified`` are ever typed into a form.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Callable, Mapping

from job_agent.applications.documents import DocumentRef, pick_variant
from job_agent.browser.forms import TEXT_TYPES, FormField, match_option
from job_agent.profile import CandidateProfile, Education, VerifiedAnswer
from job_agent.security.legal_text import is_legal_question

_PLACEHOLDER_RE = re.compile(r"<[^<>]+>")


class FieldCategory(StrEnum):
    FIRST_NAME = "first_name"
    LAST_NAME = "last_name"
    FULL_NAME = "full_name"
    EMAIL = "email"
    PHONE = "phone"
    LOCATION = "location"
    CITY = "city"
    LINKEDIN = "linkedin"
    GITHUB = "github"
    WEBSITE = "website"
    RESUME = "resume"
    COVER_LETTER_FILE = "cover_letter_file"
    COVER_LETTER_TEXT = "cover_letter_text"
    OTHER_UPLOAD = "other_upload"
    SCHOOL = "school"
    DEGREE = "degree"
    DISCIPLINE = "discipline"
    GRADUATION = "graduation"
    GPA = "gpa"
    LEGAL = "legal"
    EEO = "eeo"
    AGREEMENT = "agreement"
    LOGISTICS = "logistics"
    REFERRAL_SOURCE = "referral_source"
    OPEN_ENDED = "open_ended"
    OTHER = "other"


PERSONAL = frozenset({FieldCategory.FIRST_NAME, FieldCategory.LAST_NAME, FieldCategory.FULL_NAME,
                      FieldCategory.EMAIL, FieldCategory.PHONE, FieldCategory.LOCATION, FieldCategory.CITY,
                      FieldCategory.LINKEDIN, FieldCategory.GITHUB, FieldCategory.WEBSITE})
EDUCATION = frozenset({FieldCategory.SCHOOL, FieldCategory.DEGREE, FieldCategory.DISCIPLINE,
                       FieldCategory.GRADUATION, FieldCategory.GPA})
#: Answer sources that count as verified for legal declarations.
VERIFIED_SOURCE_PREFIXES = ("verified_answer:", "user")

# --------------------------------------------------------------------------- #
# Patterns (applied to normalized text: ascii, lowercase, words separated by one space)
# --------------------------------------------------------------------------- #
_LEGAL = re.compile(
    r"\b(authori[sz]ed to work|authori[sz]ation to work|work authori[sz]ation|employment authori[sz]ation|"
    r"eligible to work|eligibility to work|right to work|legally (able|permitted|authori[sz]ed|eligible)|"
    r"sponsor\w*|visas?|h ?1 ?b|immigration|green card|permanent resident|citizen\w*|nationality|"
    r"security clearance|clearances?|export (control\w*|administration regulations)|itar|u ?s persons?|"
    r"deemed exports?|criminal|convicted|convictions?|felon\w*|misdemeanou?rs?|background (checks?|"
    r"investigation|screening)|(opt|cpt) (status|authori[sz]ation|eligib\w*)|stem opt|work permit)\b"
)
_EEO = re.compile(
    r"\b(gender|sex|race|racial|ethnicity|ethnic|hispanic|latin[oax]|veterans?|disabilit(y|ies)|disabled|"
    r"pronouns?|sexual orientation|transgender|lgbt\w*|self identif\w*|voluntary self|eeoc?|equal employment)\b"
)
_AGREEMENT = re.compile(
    r"\b(i certify|i hereby|i agree|agree to|i acknowledge|acknowledge|i consent|consent to|i attest|attest|"
    r"i understand|i confirm|i declare|by (checking|submitting|clicking)|privacy (policy|notice|statement)|"
    r"terms( of| and)? (use|service|conditions)|accept the)\b"
)
_EMAIL = re.compile(r"\be ?mail( address)?\b")
_PHONE = re.compile(r"\b(phone|mobile|cell|telephone)( number)?\b")
_LINKEDIN = re.compile(r"\blinked ?in\b")
_GITHUB = re.compile(r"\bgit ?hub\b")
_WEBSITE = re.compile(r"\b(website|web site|portfolio|personal (site|url|page)|blog)\b")
_FIRST = re.compile(r"\b((first|given|fore) ?name|fname)\b")
_LAST = re.compile(r"\b((last|family|sur) ?name|lname)\b")
_FULL = re.compile(r"^((your|full|legal|candidate) )*name$|\bfull name\b|\blegal name\b")
_NAME_EXCLUDE = re.compile(
    r"\b(company|employer|school|university|reference|referr\w*|emergency|manager|recruiter|preferred|nick ?name|"
    r"middle|organi[sz]ation|business|project|product|team|account|user ?name)\b"
)
_LOCATION = re.compile(
    r"^((current|your|home|present) )?(location|city|city state|city and state|city state country|"
    r"location city)( city)?$|\bwhere are you (currently )?(located|based)\b"
)
_CITY_ONLY = re.compile(r"^((current|your|home) )?city$")
_RESUME = re.compile(r"\b(resume|cv|curriculum vitae)\b")
_COVER = re.compile(r"\bcover ?letter\b")
_SCHOOL = re.compile(r"\b(school|university|college|institution)( name)?\b")
_HIGH_SCHOOL = re.compile(r"\bhigh school\b")
_DEGREE = re.compile(r"\bdegree( type| level)?\b")
_DEGREE_COMPLETED = re.compile(r"\b(highest|obtained|completed|earned|awarded|conferred)\b")
_DISCIPLINE = re.compile(r"\b(discipline|major|field of study|area of study|concentration|program of study)\b")
_GRADUATION = re.compile(
    r"\b(graduation|grad date|graduation date|graduation year|completion date|date of graduation|"
    r"(expected|anticipated) graduation|when (will|do) you graduate)\b"
)
_GPA = re.compile(r"\b(gpa|grade point average)\b")
_LOGISTICS = re.compile(
    r"\b(salary|compensation|pay (range|expectations?)|desired pay|expected pay|wage|start date|"
    r"available to start|availability|when can you (start|begin)|earliest start|notice period|"
    r"relocat\w*|willing to travel|travel requirements?|commute)\b"
)
_REFERRAL = re.compile(
    r"\b((how|where) did you (hear|learn|find out|find)|referr(al|ed) (source|by)|source of (application|referral)|"
    r"how were you referred)\b"
)
_OPEN_ENDED = re.compile(
    r"^(why|what|how|describe|tell us|explain|please (describe|share|explain|tell|provide)|share|in a few)\b"
)
#: Words that may surround a legal verified answer's pattern without changing what is asked
#: ("Will you now or in the future require visa sponsorship?" for "require visa sponsorship").
#: Anything else -- another clause, a condition, a past tense, another country -- means the
#: pattern does not answer the question.
_LEGAL_FILLER = frozenset("""
a an the are you your yourself do does will would could can shall should be is am have has need now or and in
of for to at on by with this that any please explain answer indicate select specify confirm currently presently
present future time point some work working employment employed employer job position role company us u s usa
united states america american legally lawfully visa status required
""".split())
#: Words that invert or qualify a question; a pattern never answers a question that adds one.
_NEGATIONS = frozenset({"not", "never", "no", "without", "t", "cannot", "nor", "neither", "unable", "unwilling",
                        "none", "nobody", "nothing"})
_LABEL_NOISE = re.compile(
    r"\((?:e\.?\s?g\.?|i\.?\s?e\.?|for example|such as|ex\.)[^)]*\)|\((?:optional|required|not required|if applicable|"
    r"yes ?/ ?no|yes or no)\)", re.I)
_YES_NO = re.compile(r"\byes (?:or )?no\b")
UPLOADS = frozenset({"resume", "cover_letter_file", "other_upload"})
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], start=1)}


def normalize(text: str | None) -> str:
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_text.lower()).split())


def classify_field(label: str, key: str = "", field_type: str = "text") -> FieldCategory:
    """Category of a form field from its label (primarily), key/name and type."""
    t = normalize(label)
    k = normalize(key)
    is_file = field_type == "file"
    if is_file:
        if _COVER.search(t) or _COVER.search(k):
            return FieldCategory.COVER_LETTER_FILE
        if _RESUME.search(t) or _RESUME.search(k):
            return FieldCategory.RESUME
        return FieldCategory.OTHER_UPLOAD
    if _LEGAL.search(t) or is_legal_question(label) or (key and is_legal_question(key)):
        return FieldCategory.LEGAL
    if _EEO.search(t) or (not t and _EEO.search(k)) or k.startswith("eeo "):
        return FieldCategory.EEO
    if _AGREEMENT.search(t):
        return FieldCategory.AGREEMENT
    if _REFERRAL.search(t):
        return FieldCategory.REFERRAL_SOURCE
    personal = _personal(t) or (_personal(k) if field_type in TEXT_TYPES and len(t.split()) <= 2 else None)
    if personal:
        if personal is FieldCategory.PHONE and field_type not in TEXT_TYPES:
            return FieldCategory.OTHER  # e.g. "phone type" selects
        return personal
    if _COVER.search(t):
        return FieldCategory.COVER_LETTER_TEXT
    if _GPA.search(t):
        return FieldCategory.GPA
    if _GRADUATION.search(t):
        return FieldCategory.GRADUATION
    if _DISCIPLINE.search(t):
        return FieldCategory.DISCIPLINE
    if _DEGREE.search(t):
        return FieldCategory.DEGREE
    if _SCHOOL.search(t) and not _HIGH_SCHOOL.search(t):
        return FieldCategory.SCHOOL
    if _LOGISTICS.search(t):
        return FieldCategory.LOGISTICS
    if field_type == "textarea" or (_OPEN_ENDED.search(t) and field_type in TEXT_TYPES) or (
            t and label.strip().endswith("?") and field_type in TEXT_TYPES and len(t) > 30):
        return FieldCategory.OPEN_ENDED
    return FieldCategory.OTHER


def _personal(t: str) -> FieldCategory | None:
    if not t:
        return None
    if _LINKEDIN.search(t):
        return FieldCategory.LINKEDIN
    if _GITHUB.search(t):
        return FieldCategory.GITHUB
    if _EMAIL.search(t):
        return FieldCategory.EMAIL
    if _PHONE.search(t):
        return FieldCategory.PHONE
    if _WEBSITE.search(t):
        return FieldCategory.WEBSITE
    if not _NAME_EXCLUDE.search(t):
        if _FIRST.search(t):
            return FieldCategory.FIRST_NAME
        if _LAST.search(t):
            return FieldCategory.LAST_NAME
        if _FULL.search(t):
            return FieldCategory.FULL_NAME
    if _CITY_ONLY.search(t):
        return FieldCategory.CITY
    if _LOCATION.search(t):
        return FieldCategory.LOCATION
    return None


# --------------------------------------------------------------------------- #
# Mapping
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SuggestedAnswer:
    """A local-LLM draft for an open-ended question. Never entered automatically."""

    answer: str
    fact_ids: tuple[str, ...]
    model_confidence: str | None = None


Drafter = Callable[[str], "SuggestedAnswer | None"]


@dataclass(frozen=True)
class MappedAnswer:
    field: FormField
    category: FieldCategory
    answer: str | None
    source: str | None
    confidence: str  # verified | uncertain | unknown
    requires_review: bool
    is_legal: bool
    review_reason: str | None = None
    document: DocumentRef | None = None

    @property
    def key(self) -> str:
        return self.field.key

    @property
    def fill_value(self) -> str | None:
        """What will be typed / selected / uploaded. Only verified, non-empty answers."""
        if self.confidence == "verified" and self.answer:
            return self.answer
        return None


@dataclass
class MappingInputs:
    profile: CandidateProfile
    resume_variants: list[DocumentRef]
    cover_letter_variants: list[DocumentRef]
    cover_letter_text: str | None = None
    cover_letter_text_doc: DocumentRef | None = None
    #: Existing rows answered by the user (field_key -> row-like mapping), preserved on re-prepare.
    user_answers: Mapping[str, Mapping[str, Any]] | None = None


def map_fields(fields: list[FormField], inputs: MappingInputs) -> list[MappedAnswer]:
    return [map_field(fld, inputs) for fld in fields]


def field_is_legal(fld: FormField) -> bool:
    """Does the label, field name / id, placeholder, help text or an option mention a legal topic?"""
    texts = [fld.label, fld.key, fld.name, fld.element_id, fld.placeholder,
             getattr(fld, "help_text", None), getattr(fld, "description", None)]
    if fld.type in {"select", "radio", "checkbox", "checkbox_group", "select-multiple"}:
        texts += [o.label for o in fld.options]
    return any(is_legal_question(str(t)) for t in texts if t)


def apply_drafts(mapped: list[MappedAnswer], drafter: Drafter | None, *, max_drafts: int = 3) -> list[MappedAnswer]:
    """Attach local-LLM *suggestions* to unanswered open-ended questions (never filled).

    Legal / eligibility questions are never sent to the drafter.
    """
    if drafter is None or max_drafts <= 0:
        return mapped
    out: list[MappedAnswer] = []
    left = max_drafts
    for m in mapped:
        if (left > 0 and m.category is FieldCategory.OPEN_ENDED and m.answer is None and m.source is None
                and m.field.type in TEXT_TYPES and not m.is_legal and not field_is_legal(m.field)):
            left -= 1
            draft = drafter(m.field.label)
            if draft is not None and draft.answer.strip():
                facts = ",".join(draft.fact_ids) or "none"
                m = replace(m, answer=draft.answer.strip(), source="llm_draft", confidence="uncertain",
                            requires_review=True,
                            review_reason=f"open-ended question; local-LLM draft from facts [{facts}] is only a "
                                          "suggestion -- edit it and save it with `job-agent approvals answer`")
        out.append(m)
    return out


def map_field(fld: FormField, inputs: MappingInputs) -> MappedAnswer:
    category = classify_field(fld.label, fld.key, fld.type)
    if category.value not in UPLOADS and category is not FieldCategory.LEGAL and field_is_legal(fld):
        category = FieldCategory.LEGAL  # legal wording in the name, placeholder, help text or options
    user = (inputs.user_answers or {}).get(fld.key)
    if user is not None:
        mapped = _from_user(fld, category, user)
    else:
        mapped = _auto(fld, category, inputs)
    if not fld.visible and not fld.is_file and mapped.confidence == "verified" and mapped.source != "user":
        mapped = replace(mapped, confidence="uncertain", requires_review=True,
                         review_reason="this control is hidden (custom widget); check it on the page yourself")
    return mapped


def _from_user(fld: FormField, category: FieldCategory, row: Mapping[str, Any]) -> MappedAnswer:
    answer = row.get("answer")
    is_legal = bool(row.get("is_legal_declaration")) or category is FieldCategory.LEGAL
    reason = "answered by you"
    review = is_legal
    if answer and fld.type in {"select", "radio"} and match_option(str(answer), fld.options) is None:
        return MappedAnswer(fld, category, str(answer), "user", "uncertain", True, is_legal,
                            f"your earlier answer {answer!r} is not one of the current options")
    if is_legal:
        reason = "legal/eligibility declaration answered by you; confirm it in the approval"
    return MappedAnswer(fld, category, None if answer is None else str(answer), "user", "verified", review,
                        is_legal, reason)


def _auto(fld: FormField, cat: FieldCategory, inputs: MappingInputs) -> MappedAnswer:
    profile = inputs.profile
    if cat is FieldCategory.RESUME:
        return _document(fld, cat, inputs.resume_variants, "résumé")
    if cat is FieldCategory.COVER_LETTER_FILE:
        return _document(fld, cat, inputs.cover_letter_variants, "cover letter")
    if cat is FieldCategory.OTHER_UPLOAD:
        return _review(fld, cat, "unrecognized upload; attach the right file yourself")
    if cat is FieldCategory.LEGAL:
        return _legal(fld, profile.verified_answers)
    if cat is FieldCategory.EEO:
        return _review(fld, cat, "voluntary self-identification (EEO): left blank unless you choose an answer")
    if cat is FieldCategory.AGREEMENT:
        return _review(fld, cat, "agreement/certification: never checked automatically; read it and answer yourself")
    if cat in PERSONAL:
        return _personal_answer(fld, cat, profile)
    if cat is FieldCategory.COVER_LETTER_TEXT:
        if inputs.cover_letter_text and fld.type in TEXT_TYPES:
            doc = inputs.cover_letter_text_doc
            if doc is None or doc.status != "approved":
                return _review(fld, cat, _approve_cover_letter(doc or next(iter(inputs.cover_letter_variants), None)))
            return MappedAnswer(fld, cat, inputs.cover_letter_text, f"document:{doc.id}", "verified", False, False,
                                "text of your approved cover letter document")
        return _missing(fld, cat, "no cover letter text available; paste it yourself if you want to")
    if cat in EDUCATION:
        return _education(fld, cat, profile.education[0] if profile.education else None)
    matches = matching_verified_answers(fld.label, profile.verified_answers)
    if len(matches) == 1:
        return _from_verified(fld, cat, matches[0])
    if len(matches) > 1:
        ids = ", ".join(m.id for m in matches)
        return _review(fld, cat, f"several verified answers match this question ({ids}); choose one yourself")
    if cat is FieldCategory.LOGISTICS:
        return _review(fld, cat, "salary / availability / relocation question: answer it yourself "
                                 "(or save a verified answer in your profile)")
    if cat is FieldCategory.REFERRAL_SOURCE:
        return _review(fld, cat, "no verified answer for this question in your profile")
    if cat is FieldCategory.OPEN_ENDED:
        return _review(fld, cat, "open-ended question: write the answer yourself")
    return _review(fld, cat, "unrecognized question: answer it yourself")


def _review(fld: FormField, cat: FieldCategory, reason: str, *, is_legal: bool = False,
            suggestion: str | None = None, source: str | None = None) -> MappedAnswer:
    return MappedAnswer(fld, cat, suggestion, source if suggestion else None, "uncertain" if suggestion else "unknown",
                        True, is_legal, reason)


def _missing(fld: FormField, cat: FieldCategory, reason: str) -> MappedAnswer:
    """No value available: optional fields stay blank, required ones need you."""
    return MappedAnswer(fld, cat, None, None, "unknown", fld.required, False,
                        reason if fld.required else f"left blank: {reason}")


def _fit(fld: FormField, cat: FieldCategory, value: str, source: str, *, candidates: list[str] | None = None,
         is_legal: bool = False, reason: str | None = None) -> MappedAnswer:
    """Use ``value`` if the field takes free text or one candidate exactly matches an option."""
    review = is_legal
    if fld.type in TEXT_TYPES:
        if fld.type == "month":
            month = _month_value(value)
            if month is None:
                return _review(fld, cat, f"cannot express {value!r} as a month value", is_legal=is_legal)
            value = month
        elif fld.type in {"date", "datetime-local", "week", "time", "number"}:
            return _review(fld, cat, f"field needs a {fld.type} value; your verified value is {value!r}",
                           is_legal=is_legal, suggestion=value, source=source)
        return MappedAnswer(fld, cat, value, source, "verified", review, is_legal, reason)
    if fld.type in {"select", "radio"}:
        for candidate in candidates or [value]:
            option = match_option(candidate, fld.options)
            if option is not None:
                return MappedAnswer(fld, cat, option.label, source, "verified", review, is_legal, reason)
        return _review(fld, cat, f"{value!r} is not exactly one of the options "
                                 f"({', '.join(o.label for o in fld.options[:8])})", is_legal=is_legal)
    return _review(fld, cat, f"cannot enter {value!r} into a {fld.type} field automatically", is_legal=is_legal)


def _profile_value(value: str | None, path: str) -> tuple[str | None, str | None]:
    if value is None or not str(value).strip():
        return None, f"{path} is not set in your profile"
    if _PLACEHOLDER_RE.search(str(value)):
        return None, f"{path} still contains a <placeholder> in your profile"
    return str(value).strip(), None


def _personal_answer(fld: FormField, cat: FieldCategory, profile: CandidateProfile) -> MappedAnswer:
    p = profile.personal
    attr = {
        FieldCategory.EMAIL: "email", FieldCategory.PHONE: "phone", FieldCategory.LOCATION: "location",
        FieldCategory.CITY: "location", FieldCategory.LINKEDIN: "linkedin_url", FieldCategory.GITHUB: "github_url",
        FieldCategory.WEBSITE: "portfolio_url", FieldCategory.FULL_NAME: "full_name",
        FieldCategory.FIRST_NAME: "full_name", FieldCategory.LAST_NAME: "full_name",
    }[cat]
    value, problem = _profile_value(getattr(p, attr), f"personal.{attr}")
    if value is None:
        return _missing(fld, cat, problem or "not set")
    source = f"profile:personal.{attr}"
    if cat is FieldCategory.CITY:
        value = value.split(",")[0].strip()
    if cat in {FieldCategory.FIRST_NAME, FieldCategory.LAST_NAME}:
        parts = value.split()
        if len(parts) != 2:
            guess = parts[0] if cat is FieldCategory.FIRST_NAME else parts[-1]
            return _review(fld, cat, f"your full name has {len(parts)} part(s); confirm the "
                                     f"{'first' if cat is FieldCategory.FIRST_NAME else 'last'} name",
                           suggestion=guess, source=source)
        value = parts[0] if cat is FieldCategory.FIRST_NAME else parts[1]
    return _fit(fld, cat, value, source)


def _education(fld: FormField, cat: FieldCategory, edu: Education | None) -> MappedAnswer:
    if edu is None:
        return _missing(fld, cat, "no education entry in your profile")
    base = f"profile:education.{edu.id}"
    if cat is FieldCategory.SCHOOL:
        return _fit(fld, cat, edu.institution, f"{base}.institution")
    if cat is FieldCategory.DEGREE:
        if _DEGREE_COMPLETED.search(normalize(fld.label)) and edu.expected:
            return _review(fld, cat, f"asks for a completed degree; your most recent ({edu.degree}) is expected "
                                     f"{edu.end or ''}".strip())
        return _fit(fld, cat, edu.degree, f"{base}.degree")
    if cat is FieldCategory.DISCIPLINE:
        value, problem = _profile_value(edu.field, f"education.{edu.id}.field")
        return _fit(fld, cat, value, f"{base}.field") if value else _missing(fld, cat, problem or "not set")
    if cat is FieldCategory.GPA:
        value, problem = _profile_value(edu.gpa, f"education.{edu.id}.gpa")
        return _fit(fld, cat, value, f"{base}.gpa") if value else _missing(fld, cat, problem or "not set")
    value, problem = _profile_value(edu.end, f"education.{edu.id}.end")
    if value is None:
        return _missing(fld, cat, problem or "not set")
    year = re.search(r"\b(19|20)\d{2}\b", value)
    candidates = [value] + ([year.group(0)] if year else [])
    month = _month_value(value)
    if month:
        candidates.append(month)
    return _fit(fld, cat, value, f"{base}.end", candidates=candidates)


def _month_value(value: str) -> str | None:
    m = re.fullmatch(r"\s*([A-Za-z]+)\.?\s+((?:19|20)\d{2})\s*", value or "")
    if not m:
        return None
    month = next((n for name, n in _MONTHS.items() if name.startswith(m.group(1).lower()) and len(m.group(1)) >= 3),
                 None)
    return f"{m.group(2)}-{month:02d}" if month else None


def _approve_cover_letter(doc: DocumentRef | None) -> str:
    return f"approve the cover letter first (job-agent documents approve {doc.id if doc else '<id>'})"


def _document(fld: FormField, cat: FieldCategory, variants: list[DocumentRef], what: str) -> MappedAnswer:
    if not variants:
        return _missing(fld, cat, f"no {what} document for this application")
    doc = pick_variant(variants, fld.accept)
    if doc is None:
        formats = ", ".join(sorted({v.format for v in variants}))
        return _review(fld, cat, f"the form accepts {fld.accept!r} but your {what} exists only as {formats}")
    if doc.status != "approved":
        if cat is FieldCategory.COVER_LETTER_FILE:
            return _review(fld, cat, _approve_cover_letter(doc))
        # The résumé is attached so the form can be prepared, but it must be approved before submission.
        return MappedAnswer(fld, cat, doc.file_name, f"document:{doc.id}", "verified", True, False,
                            f"your {what} ({doc.format.upper()}) is {doc.status}, not approved: review it, run "
                            f"`job-agent documents approve {doc.id}` and prepare the application again",
                            document=doc)
    return MappedAnswer(fld, cat, doc.file_name, f"document:{doc.id}", "verified", False, False,
                        f"your {what} ({doc.format.upper()})", document=doc)


def _remainder(label_norm: str, pattern_norm: str) -> list[str] | None:
    """Words of the label outside the first whole-word occurrence of the pattern (None if absent)."""
    padded, needle = f" {label_norm} ", f" {pattern_norm} "
    idx = padded.find(needle)
    if idx < 0:
        return None
    return (padded[:idx] + " " + padded[idx + len(needle):]).split()


def matching_verified_answers(label: str, answers: list[VerifiedAnswer], *,
                              strict: bool = False) -> list[VerifiedAnswer]:
    """Verified answers with a pattern that occurs in the label as whole words.

    A pattern never matches a label that adds a negation ("not", "without", ...)
    around it. ``strict`` (legal declarations) also requires every other word of
    the label to be neutral filler (``_LEGAL_FILLER``), so "require visa
    sponsorship" answers "Will you now or in the future require visa sponsorship?"
    but not a question that adds another clause, a condition, a past tense or
    another country.
    """
    text = _YES_NO.sub(" ", normalize(_LABEL_NOISE.sub(" ", label or "")))
    out: list[VerifiedAnswer] = []
    for answer in answers:
        for pattern in answer.question_patterns:
            needle = normalize(pattern)
            rest = _remainder(text, needle) if needle else None
            if rest is None or any(w in _NEGATIONS and w not in needle.split() for w in rest):
                continue
            if strict and any(w not in _LEGAL_FILLER for w in rest):
                continue
            out.append(answer)
            break
    return out


def _legal(fld: FormField, answers: list[VerifiedAnswer]) -> MappedAnswer:
    cat = FieldCategory.LEGAL
    legal_answers = [a for a in answers if a.is_legal_declaration]
    matches = matching_verified_answers(fld.label, legal_answers, strict=True)
    if len(matches) > 1:
        ids = ", ".join(m.id for m in matches)
        return _review(fld, cat, f"legal/eligibility question matches several verified answers ({ids}); "
                                 "answer it yourself", is_legal=True)
    if matches:
        return _from_verified(fld, cat, matches[0], force_legal=True)
    reason = ("legal/eligibility question: never answered automatically -- answer it with `job-agent approvals "
              "answer` or save a verified answer with is_legal_declaration: true in your profile")
    partial = [a.id for a in matching_verified_answers(fld.label, legal_answers)]
    if partial:
        reason += f"; verified answer(s) {', '.join(partial)} match only part of this question"
    other = [a.id for a in matching_verified_answers(fld.label, [a for a in answers if not a.is_legal_declaration])]
    if other:
        reason += (f"; verified answer(s) {', '.join(other)} are not marked is_legal_declaration and are never "
                   "used for legal questions")
    return _review(fld, cat, reason, is_legal=True)


def _from_verified(fld: FormField, cat: FieldCategory, va: VerifiedAnswer, *, force_legal: bool = False) -> MappedAnswer:
    is_legal = force_legal or va.is_legal_declaration or cat is FieldCategory.LEGAL
    if cat in {FieldCategory.EEO, FieldCategory.AGREEMENT}:
        return _review(fld, cat, f"verified answer '{va.id}' matches, but this kind of question is never "
                                 "answered automatically")
    value, problem = _profile_value(va.answer, f"verified_answers.{va.id}")
    if value is None:
        return _review(fld, cat, problem or "verified answer is empty", is_legal=is_legal)
    if fld.type in {"checkbox", "checkbox_group", "select-multiple"}:
        return _review(fld, cat, f"verified answer '{va.id}' cannot be applied to a {fld.type} automatically",
                       is_legal=is_legal)
    reason = (f"legal/eligibility declaration answered from your verified answer '{va.id}'; confirm it in the "
              "approval" if is_legal else f"from your verified answer '{va.id}'")
    mapped = _fit(fld, cat, value, f"verified_answer:{va.id}", is_legal=is_legal, reason=reason)
    if mapped.confidence != "verified":
        return replace(mapped, requires_review=True, is_legal=is_legal)
    return mapped
