"""Drafting helpers backed by the local LLM.

Every function takes an ``OllamaClient`` first, wraps untrusted text (job
listings, emails, form questions) with ``wrap_untrusted``, asks for JSON
validated by Pydantic, and raises ``LLMUnavailable`` / ``JobAgentError``
subclasses on failure. A few cheap deterministic guards run here (numbers
preserved, unknown fact ids dropped, legal / sensitive / unreadable questions
refused before any model call, reply / follow-up / answer drafts rejected when
they disclose, commit, make a work-authorization or other legal statement --
also misspelled or in another language --, use non-ASCII numerals or claim
numbers, tools or leadership that are not in the verified facts -- see
``security.legal_text``); callers still perform their own factual validation
before using any text. Drafts are validated and returned as
``security.textnorm.normalize_text`` output, so the text stored is exactly the
text that was checked.

Nothing here sends, submits or schedules anything: outputs are drafts for the
human to review.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, field_validator

from job_agent.llm import prompts
from job_agent.llm.analysis import clean_field
from job_agent.llm.client import OllamaClient
from job_agent.llm.errors import LegalQuestionRefused, LLMInvalidOutput
from job_agent.models import EmailCategory, EmailClassification
from job_agent.profile import Fact
from job_agent.security import legal_text
from job_agent.security.redaction import redact_text
from job_agent.security.textnorm import has_non_ascii_numerals, normalize_text
from job_agent.security.untrusted import wrap_untrusted

_NUMBER_RE = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*%?")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]")

#: Legal / eligibility questions (``security.legal_text``); kept here for callers of this module.
LEGAL_QUESTION_RE = legal_text.LEGAL_QUESTION_RE

_UNSAFE_DRAFT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "contains an email address"),
    (re.compile(r"(?<!\d)(\+?\d{1,2}[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d)"), "contains a phone number"),
    (re.compile(r"[$€£]\s?\d|\b\d+(?:[.,]\d+)?\s?[kK]\b|\b\d+(?:[.,]\d+)?\s?(?:USD|dollars)\b"),
     "mentions a money amount"),
    (re.compile(r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm|a\.m\.|p\.m\.)(?![a-z])|\b\d{1,2}:\d{2}\b", re.I),
     "names a specific time"),
    (re.compile(r"\bI(?: am|'m| will be|'ll be) available (?:on|at)\b|\bI (?:can|will) start\b|"
                r"\bI(?: hereby)? accept\b|\baccept(?:ing)? (?:the|your) (?:offer|invitation|time|slot)\b|"
                r"\bsee you (?:on|at)\b", re.I),
     "commits the candidate"),
    (re.compile(r"\b(?:F-1|J-1|H-1B|OPT|CPT|green card|visa|citizenship|social security|SSN|passport)\b", re.I),
     "mentions immigration or identity details"),
]


# --------------------------------------------------------------------------- #
# Output contracts
# --------------------------------------------------------------------------- #
class CoverLetterDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paragraphs: list[str]
    fact_ids_used: list[str]


class AnswerDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    fact_ids: list[str]
    confidence: Literal["low", "medium"]


# --------------------------------------------------------------------------- #
# Schemas sent to the model (no length constraints in the JSON schema: they are
# checked here instead, so the grammar Ollama builds stays small and fast)
# --------------------------------------------------------------------------- #
class _Out(BaseModel):
    model_config = ConfigDict(extra="ignore")


def _require_text(v: str) -> str:
    if not v.strip():
        raise ValueError("must not be empty")
    return v


#: Non-blank string (checked after parsing; not part of the JSON schema sent to Ollama).
NonBlank = Annotated[str, AfterValidator(_require_text)]


class RewordOut(_Out):
    text: NonBlank


class CoverLetterOut(_Out):
    paragraphs: list[str]
    fact_ids_used: list[str]

    @field_validator("paragraphs")
    @classmethod
    def _some(cls, v: list[str]) -> list[str]:
        if not [p for p in v if isinstance(p, str) and p.strip()]:
            raise ValueError("at least one non-empty paragraph is required")
        return v


class EmailClassificationOut(_Out):
    category: EmailCategory
    confidence: float
    summary: str
    employer_guess: str | None
    requires_attention: bool

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, v: Any) -> Any:
        if isinstance(v, str):
            key = v.strip().lower().replace(" ", "_").replace("-", "_")
            return key if key in {c.value for c in EmailCategory} else EmailCategory.OTHER.value
        return v


class ReplyOut(_Out):
    reply: NonBlank


class FollowupOut(_Out):
    message: NonBlank


class AnswerOut(_Out):
    answer: NonBlank
    fact_ids: list[str]
    confidence: Literal["low", "medium"]

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, v: Any) -> Any:
        if isinstance(v, str):
            return "medium" if v.strip().lower() in {"medium", "high"} else "low"
        return v


# --------------------------------------------------------------------------- #
# Deterministic helpers (also useful to callers)
# --------------------------------------------------------------------------- #
def numbers_in(text: str) -> set[str]:
    return set(_NUMBER_RE.findall(text or ""))


def unverified_numbers(text: str, sources: Iterable[str]) -> list[str]:
    """Numbers in ``text`` that appear in none of ``sources`` (e.g. verified fact texts)."""
    allowed: set[str] = set()
    for s in sources:
        allowed |= numbers_in(s)
    return sorted(numbers_in(text) - allowed)


def unsafe_draft_problems(text: str, *, facts: Sequence[Fact | str] | None = None,
                          profile_skills: Sequence[str] | None = None, context: Sequence[str] = ()) -> list[str]:
    """Reasons a reply/follow-up draft is unsafe.

    Disclosure, commitment and negotiation patterns, any work-authorization or
    other legal statement, and claims (numbers, tools/skills, leadership) not
    traceable to ``facts`` / ``profile_skills``. When ``facts`` is ``None`` (not
    provided) every first-person experience claim with numbers, tools or
    leadership is rejected. Numbers in ``context`` (trusted application data such
    as the date you applied) may be repeated outside experience claims.

    Checks run on ``normalize_text(text)`` (plus a check for non-ASCII numerals in
    ``text`` itself); store that normalized text, not ``text``.
    """
    norm = normalize_text(text)
    problems = [reason for pattern, reason in _UNSAFE_DRAFT_PATTERNS if pattern.search(norm)]
    problems += legal_text.draft_claim_problems(text, facts=facts, skills=profile_skills, context=context)
    return list(dict.fromkeys(problems))


def is_legal_question(question: str) -> bool:
    """Questions the LLM never drafts: legal / eligibility (``security.legal_text.is_legal_question``,
    including misspelled, non-English and unreadable-script wording) plus compensation, EEO and
    identity questions."""
    return legal_text.is_never_drafted(question)


def clean_multiline(text: str, max_len: int) -> str:
    value = _CONTROL_RE.sub("", text or "").replace("\r\n", "\n").replace("\r", "\n")
    value = "\n".join(" ".join(line.split()) for line in value.split("\n"))
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    return value if len(value) <= max_len else value[: max_len - 1].rstrip() + "…"


def _fact_ids(raw_ids: Sequence[str], valid: set[str]) -> list[str]:
    out: list[str] = []
    for raw in raw_ids:
        fid = str(raw).strip().strip("[]()\"' ")
        if fid in valid and fid not in out:
            out.append(fid)
    return out


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def reword_fact(client: OllamaClient, fact_text: str, *, job_title: str, job_keywords: list[str],
                model: str | None = None) -> str:
    """Rephrase ONE verified statement for a job. Numbers must be preserved exactly."""
    context = f"Job title: {job_title}\nJob keywords: {', '.join(str(k) for k in job_keywords[:30])}"
    wrapped = wrap_untrusted(context, label="JOB_CONTEXT", max_chars=2000)
    out = client.generate_json(system=prompts.REWORD_FACT_SYSTEM, prompt=prompts.reword_fact_prompt(fact_text, wrapped),
                               schema=RewordOut, model=model, max_tokens=160)
    text = clean_field(out.text, 400)
    added = sorted(numbers_in(text) - numbers_in(fact_text))
    missing = sorted(numbers_in(fact_text) - numbers_in(text))
    if added or missing:
        raise LLMInvalidOutput(f"reworded fact changed numbers (added {added}, missing {missing})")
    limit = max(30, len(fact_text.split()))
    if len(text.split()) > limit:
        raise LLMInvalidOutput(f"reworded fact is longer than {limit} words")
    return text


def draft_cover_letter(client: OllamaClient, *, employer: str, job_title: str, job_highlights: list[str],
                       facts: list[Fact], candidate_name: str, model: str | None = None) -> CoverLetterDraft:
    """Paragraph drafts using only the given verified facts. Unknown fact ids are dropped."""
    info = f"Employer: {employer}\nJob title: {job_title}\nHighlights:\n" + "\n".join(
        f"- {h}" for h in job_highlights[:15])
    wrapped = wrap_untrusted(info, label="JOB_INFO", max_chars=client.config.max_input_chars)
    block = prompts.facts_block((f.id, f.text) for f in facts)
    out = client.generate_json(system=prompts.COVER_LETTER_SYSTEM,
                               prompt=prompts.cover_letter_prompt(wrapped, block, candidate_name),
                               schema=CoverLetterOut, model=model, max_tokens=900)
    paragraphs = [clean_field(p, 1500) for p in out.paragraphs if isinstance(p, str) and p.strip()][:6]
    if not paragraphs:
        raise LLMInvalidOutput("cover letter draft had no usable paragraphs")
    return CoverLetterDraft(paragraphs=paragraphs, fact_ids_used=_fact_ids(out.fact_ids_used, {f.id for f in facts}))


def classify_email(client: OllamaClient, *, subject: str, sender: str, body: str,
                   model: str | None = None) -> EmailClassification:
    model_name = client.resolve_model(model)
    wrapped = wrap_untrusted(f"From: {sender}\nSubject: {subject}\n\n{body}", label="EMAIL",
                             max_chars=client.config.max_input_chars)
    out = client.generate_json(system=prompts.EMAIL_CLASSIFY_SYSTEM, prompt=prompts.email_classify_prompt(wrapped),
                               schema=EmailClassificationOut, model=model_name, max_tokens=250)
    confidence = out.confidence if out.confidence == out.confidence else 0.0  # NaN -> 0
    employer = clean_field(out.employer_guess or "", 120) or None
    return EmailClassification(
        category=out.category,
        confidence=round(min(1.0, max(0.0, confidence)), 3),
        summary=clean_field(redact_text(clean_field(out.summary, 600), pii=True), 300),
        employer_guess=employer,
        requires_attention=out.requires_attention,
        classifier=f"llm:{model_name}",
    )


def _checked_draft(text: str, kind: str, *, facts: Sequence[Fact | str] | None = None,
                   profile_skills: Sequence[str] | None = None, context: Sequence[str] = ()) -> str:
    """The normalized draft (exactly the text that was checked), or ``LLMInvalidOutput``."""
    draft = clean_multiline(normalize_text(text), 2500)
    problems = []
    if has_non_ascii_numerals(text):
        problems.append("uses numerals that are not plain digits (circled, roman, superscript or other scripts)")
    problems += unsafe_draft_problems(draft, facts=facts, profile_skills=profile_skills, context=context)
    problems = list(dict.fromkeys(problems))
    if problems:
        raise LLMInvalidOutput(f"{kind} draft rejected: {', '.join(problems)}",
                               human_action="Write this message yourself (a template is available).")
    return draft


def draft_email_reply(client: OllamaClient, *, category: str, subject: str, body: str, candidate_name: str,
                      model: str | None = None, facts: Sequence[Fact | str] | None = None,
                      profile_skills: Sequence[str] | None = None) -> str:
    """Reply draft that never accepts times, negotiates, commits, discloses sensitive information,
    states anything about work authorization or claims unverified experience.

    ``facts`` / ``profile_skills`` (verified profile data) are used only to check
    the draft; without them any experience claim with numbers, tools or
    leadership is rejected. The email itself is never a source of allowed claims.
    """
    valid = {c.value for c in EmailCategory}
    category = category if category in valid else EmailCategory.OTHER.value
    wrapped = wrap_untrusted(f"Subject: {subject}\n\n{body}", label="EMAIL", max_chars=client.config.max_input_chars)
    out = client.generate_json(system=prompts.EMAIL_REPLY_SYSTEM,
                               prompt=prompts.email_reply_prompt(category, wrapped, candidate_name),
                               schema=ReplyOut, model=model, max_tokens=400)
    return _checked_draft(out.reply, "reply", facts=facts, profile_skills=profile_skills,
                          context=(candidate_name,))


def draft_followup(client: OllamaClient, *, employer: str, job_title: str, context: str, candidate_name: str,
                   model: str | None = None, facts: Sequence[Fact | str] | None = None,
                   profile_skills: Sequence[str] | None = None) -> str:
    """Follow-up draft with the same checks as ``draft_email_reply``.

    Numbers from the application data (employer, title, ``context`` such as the
    date you applied) may be repeated, but never as part of an experience claim.
    """
    wrapped = wrap_untrusted(f"Employer: {employer}\nJob title: {job_title}\n\n{context}", label="APPLICATION_CONTEXT",
                             max_chars=4000)
    out = client.generate_json(system=prompts.FOLLOWUP_SYSTEM, prompt=prompts.followup_prompt(wrapped, candidate_name),
                               schema=FollowupOut, model=model, max_tokens=350)
    return _checked_draft(out.message, "follow-up", facts=facts, profile_skills=profile_skills,
                          context=(employer, job_title, context, candidate_name))


def draft_answer(client: OllamaClient, *, question: str, facts: list[Fact], model: str | None = None,
                 profile_skills: Sequence[str] | None = None) -> AnswerDraft:
    """Answer an ordinary (non-legal) form question from verified facts.

    Legal / eligibility / compensation / EEO / identity questions -- also
    misspelled, in another language or in a script the checks cannot read -- are
    refused before any model call (``LegalQuestionRefused``, a
    ``PermanentError``). Answers that are a bare yes/no, make a legal statement,
    use non-ASCII numerals or claim numbers, tools or leadership absent from the
    facts are rejected (``LLMInvalidOutput``). The returned answer is the
    normalized text that was checked.
    """
    if is_legal_question(question):
        raise LegalQuestionRefused("this question concerns legal, eligibility or compensation matters and must be "
                                   "answered by you; no answer was generated")
    wrapped = wrap_untrusted(question, label="FORM_QUESTION", max_chars=2000)
    block = prompts.facts_block((f.id, f.text) for f in facts)
    out = client.generate_json(system=prompts.ANSWER_SYSTEM, prompt=prompts.answer_prompt(wrapped, block),
                               schema=AnswerOut, model=model, max_tokens=300)
    if has_non_ascii_numerals(out.answer):
        raise LLMInvalidOutput("answer uses numerals that are not plain digits")
    answer = clean_multiline(normalize_text(out.answer), 1500)
    if legal_text.is_yes_no_answer(answer):
        raise LLMInvalidOutput("answer is a bare yes/no; open-ended questions are answered from facts, and yes/no "
                               "questions by you")
    extra = unverified_numbers(answer, [f.text for f in facts])
    if extra:
        raise LLMInvalidOutput(f"answer contains numbers not found in verified facts: {extra}")
    problems = legal_text.draft_claim_problems(answer, facts=facts, skills=profile_skills)
    if problems:
        raise LLMInvalidOutput(f"answer rejected: {', '.join(problems)}")
    ids = _fact_ids(out.fact_ids, {f.id for f in facts})
    return AnswerDraft(answer=answer, fact_ids=ids, confidence=out.confidence if ids else "low")
