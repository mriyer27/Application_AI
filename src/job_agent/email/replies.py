"""Suggested replies (drafts the user may copy). Never sent by this program.

Drafts never accept a proposed time, negotiate, commit the candidate to
anything, disclose sensitive information, state anything about work
authorization / sponsorship, or claim experience (numbers -- also with a
changed qualifier such as "9 or more" --, tools, leadership, degrees,
certifications, employers, job titles) that is not in the verified profile;
they use placeholders such as
``[your availability]`` that the user fills in. An optional local-LLM draft is
accepted only if it passes the same deterministic checks
(``security.legal_text``: also misspelled or non-English work-authorization
statements, clearance claims and non-ASCII numerals), otherwise the template is
used. Checks run on ``security.textnorm.normalize_text`` output, and that
normalized text is what gets stored.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, Callable

from job_agent.models import EmailCategory
from job_agent.security.legal_text import draft_claim_problems
from job_agent.security.textnorm import normalize_text
from job_agent.security.untrusted import clean_untrusted

C = EmailCategory

REPLY_CATEGORIES: frozenset[EmailCategory] = frozenset({
    C.RECRUITER_MESSAGE, C.INTERVIEW_INVITATION, C.SCHEDULING_REQUEST, C.ASSESSMENT,
})
MAX_REPLY_CHARS = 1500

_TEMPLATES: dict[EmailCategory, str] = {
    C.RECRUITER_MESSAGE: (
        "Hi [recruiter name],\n\n"
        "Thank you for reaching out. I would be glad to learn more about the [role] opportunity. "
        "Could you share more details about the role, the team and the next steps in the process?\n\n"
        "I am generally available [your availability] if a short call would be helpful.\n\n"
        "Best regards,\n{name}"
    ),
    C.INTERVIEW_INVITATION: (
        "Hi [name],\n\n"
        "Thank you for the invitation to interview for the [role] position. I would be glad to speak with the team.\n\n"
        "My availability over the next few days is: [your availability]. "
        "Please let me know what works best on your side, and whether there is anything I should prepare.\n\n"
        "Best regards,\n{name}"
    ),
    C.SCHEDULING_REQUEST: (
        "Hi [name],\n\n"
        "Thank you for reaching out to schedule. I am available at the following times: [your availability] "
        "([your time zone]).\n\n"
        "Please let me know which option works for the team.\n\n"
        "Best regards,\n{name}"
    ),
    C.ASSESSMENT: (
        "Hi [name],\n\n"
        "Thank you for sending the assessment for the [role] position. I will review the instructions. "
        "Could you please confirm the deadline and the expected time to complete it?\n\n"
        "Best regards,\n{name}"
    ),
}

#: Phrases a suggested reply must never contain (acceptance, commitment, negotiation, sensitive data).
_FORBIDDEN = re.compile(
    r"\b(?:i (?:accept|agree)|accept(?:ing)? (?:the|your) (?:offer|invitation|time|slot)|i(?:'ll| will) (?:be there|"
    r"attend|join)|see you (?:on|at|then)|(?:that|this) (?:time|slot|works) (?:works|for me)|works for me|"
    r"confirm(?:ed|ing)? (?:the|my|our) (?:interview|meeting|time|slot|attendance)|i commit|i guarantee|"
    r"salary|compensation|counter[- ]?offer|negotiat\w*|(?:my|expected) (?:rate|pay)|sponsor\w*|visa|citizen\w*|"
    r"social security|ssn|date of birth|password|bank|immigration)\b"
    r"|\$\s?\d|\b\d{1,2}(?::\d{2})?\s?(?:am|pm)\b|\b\d{3}[\s.\-]?\d{3}[\s.\-]?\d{4}\b|[\w.+-]+@[\w-]+\.[\w.]+",
    re.I,
)


def template_reply(category: EmailCategory, *, candidate_name: str | None) -> str | None:
    template = _TEMPLATES.get(category)
    if template is None:
        return None
    return template.format(name=(candidate_name or "[your name]").strip() or "[your name]")


def reply_problems(text: str, *, facts: Iterable[Any] | None = None, skills: Iterable[str] | None = None,
                   context: Iterable[str] = (), profile: Any = None) -> list[str]:
    """Why a drafted reply / follow-up may not be offered (empty list = safe).

    ``facts`` (verified fact texts or ``Fact`` objects), ``skills`` (profile
    skills) and ``profile`` (the ``CandidateProfile``: education,
    certifications, employers, titles) let claims be checked; when ``facts`` is
    ``None`` any first-person experience claim with numbers, tools or leadership
    is rejected, and without ``profile`` any degree or graduation wording the
    facts lack. ``context`` is trusted application data whose numbers (e.g. the
    date you applied) may be repeated and whose names (the employer) may be
    named -- never claimed as your background ("my time at Acme"). The incoming
    email is never a source of allowed claims.

    Everything is checked on ``normalize_text(text)`` (non-ASCII numerals in
    ``text`` itself are a problem too); offer that normalized text, not ``text``.
    """
    norm = normalize_text(text)
    problems: list[str] = []
    if not norm.strip():
        problems.append("is empty")
    if len(norm) > MAX_REPLY_CHARS:
        problems.append(f"is longer than {MAX_REPLY_CHARS} characters")
    if _FORBIDDEN.search(norm):
        problems.append("accepts, commits, negotiates or discloses sensitive information")
    problems += draft_claim_problems(text, facts=facts, skills=skills, context=tuple(context), profile=profile)
    return list(dict.fromkeys(problems))


def reply_is_safe(text: str, *, facts: Iterable[Any] | None = None, skills: Iterable[str] | None = None,
                  context: Iterable[str] = (), profile: Any = None) -> bool:
    return not reply_problems(text, facts=facts, skills=skills, context=tuple(context), profile=profile)


def suggest_reply(category: EmailCategory, *, candidate_name: str | None, subject: str = "", body: str = "",
                  llm_draft: Callable[..., str] | None = None, facts: Iterable[Any] | None = None,
                  profile_skills: Iterable[str] | None = None, profile: Any = None,
                  employer: str | None = None) -> tuple[str | None, str]:
    """Return ``(draft, source)`` where source is ``template`` / ``llm`` / ``none``.

    ``facts`` / ``profile_skills`` / ``profile`` are only used to check the LLM
    draft; ``employer`` (the organization the email is from, trusted application
    data) may be named in it, never claimed as your background. An accepted LLM
    draft is returned normalized (``normalize_text``): exactly the text that
    passed the checks.
    """
    if category not in REPLY_CATEGORIES:
        return None, "none"
    fallback = template_reply(category, candidate_name=candidate_name)
    if llm_draft is not None:
        draft = llm_draft(category=category.value, subject=subject, body=body,
                          candidate_name=candidate_name or "[your name]")
        cleaned = clean_untrusted(str(draft or ""), max_chars=MAX_REPLY_CHARS * 2)
        normalized = normalize_text(cleaned)
        if "[" in normalized and reply_is_safe(cleaned, facts=None if facts is None else list(facts),
                                               skills=profile_skills, context=(candidate_name or "", employer or ""),
                                               profile=profile):
            return normalized, "llm"
    return fallback, "template"
