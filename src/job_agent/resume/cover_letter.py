"""Cover letters: a deterministic template, plus an optional validated LLM draft.

Template structure (every sentence is either fixed wording, a profile value, the
job's title/employer, or a verified fact):

1. "Dear Hiring Team,"
2. Opening: the exact job title and employer, and the candidate's degree program
   with its (expected) graduation date verbatim from the profile.
3. Two or three body sentences, each restating one selected verified fact and
   naming a requirement keyword that literally appears in the listing.
4. Motivation: only ``profile.preferences.preferred_themes`` that the listing
   also mentions -- no knowledge about the company is invented.
5. Closing, "Sincerely," and the profile name.

An LLM draft (``job_agent.llm.generation.draft_cover_letter``) is used only if
every sentence passes the deterministic checks; any failure => template.
Cover letters are always ``needs_review``.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Sequence

from job_agent.models import DocumentStatus
from job_agent.profile import CandidateProfile, Education, Fact
from job_agent.resume import lexicon, validation
from job_agent.resume.keywords import JobKeywords
from job_agent.resume.models import Claim, CoverLetterContent, GeneratedCoverLetter, Issue
from job_agent.resume.selection import ScoredFact, build_header, candidate_facts, fact_text
from job_agent.resume.text import content_set, content_words, mentions, normalize_space, split_sentences, words
from job_agent.security.untrusted import clean_untrusted, injection_suspected

GREETING = "Dear Hiring Team,"
CLOSING = "Sincerely,"
_MAX_BODY_FACTS = 3


def display_text(text: str | None, *, max_len: int) -> str:
    """Untrusted single-line text (job title / employer) made safe to print in a letter."""
    cleaned = normalize_space(clean_untrusted(text or "", max_chars=max_len * 2))
    cleaned = re.sub(r"[<>{}\[\]\\`]", "", cleaned)
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rsplit(" ", 1)[0].rstrip(",;:-") or cleaned[:max_len]
    return cleaned


def format_letter_date(on: date) -> str:
    return f"{on.strftime('%B')} {on.day}, {on.year}"


def _primary_education(profile: CandidateProfile) -> Education:
    expected = [e for e in profile.education if e.expected]
    return expected[0] if expected else profile.education[0]


def opening_paragraph(profile: CandidateProfile, *, job_title: str, employer: str) -> str:
    edu = _primary_education(profile)
    program = edu.degree + (f" in {edu.field}" if edu.field else "")
    first = f"I am writing to apply for the {job_title} position at {employer}."
    if edu.expected:
        when = ""
        if edu.end:
            when = f" ({edu.end})" if edu.end.lower().startswith("expected") else f" (expected graduation: {edu.end})"
        second = f"I am currently pursuing my {program} at {edu.institution}{when}."
    else:
        when = f" ({edu.end})" if edu.end else ""
        second = f"I completed my {program} at {edu.institution}{when}."
    return f"{first} {second}"


def _lower_first(text: str, profile: CandidateProfile) -> str | None:
    """Lower-case the first word if it is an ordinary capitalized word; None if unsafe."""
    ws = words(text)
    if not ws:
        return None
    first = ws[0]
    if first in {"I", "We"} or not first[:1].isupper() or not first[1:].islower():
        return None
    protected = {t.lower() for s in profile.skills for t in s.split()} | {n.lower() for n in lexicon.lexicon_names()}
    protected |= {t.lower() for org in validation.organizations(profile) for t in org.split()}
    if first.lower() in protected:
        return None
    return text[0].lower() + text[1:]


def _entry_context(profile: CandidateProfile, sf: ScoredFact) -> str:
    if sf.entry_kind == "experience":
        x = next(x for x in profile.experience if x.id == sf.entry_id)
        return f"In my {x.title} role at {x.employer}"
    if sf.entry_kind == "project":
        p = next(p for p in profile.projects if p.id == sf.entry_id)
        return f"In my {p.name} project"
    e = next(e for e in profile.education if e.id == sf.entry_id)
    return f"During my {e.degree} studies at {e.institution}"


def body_sentence(profile: CandidateProfile, sf: ScoredFact, keyword: str | None, *, first: bool = True,
                  same_entry: bool = False) -> str:
    text = fact_text(sf.fact)
    if not text.endswith((".", "!", "?")):
        text += "."
    context = "In the same role" if same_entry else _entry_context(profile, sf)
    lowered = _lower_first(text, profile)
    core = f"{context}, I {lowered}" if lowered else f"{context}: {text}"
    if not keyword:
        return core
    lead = f"Your listing mentions {keyword}." if first else f"The listing also mentions {keyword}."
    return f"{lead} {core}"


def _pick_body_facts(profile: CandidateProfile, kw: JobKeywords) -> list[tuple[ScoredFact, str | None]]:
    """2-3 relevant facts, each tied to a distinct keyword the listing literally mentions."""
    scored = candidate_facts(profile, kw)
    order = {("experience", x.id): i for i, x in enumerate(profile.experience)}
    scored.sort(key=lambda s: (-s.score, order.get((s.entry_kind, s.entry_id), 99), s.index))
    chosen: list[tuple[ScoredFact, str | None]] = []
    used_keywords: set[str] = set()
    used_entries: dict[str, int] = {}
    for sf in scored:
        if len(chosen) >= _MAX_BODY_FACTS:
            break
        keyword = next((k for k in sf.matched if k.lower() not in used_keywords and kw.mentions(k)), None)
        if keyword is None:
            keyword = next((k for k in sf.matched if k in kw.skill_weights and k.lower() not in used_keywords),
                           None)
        if keyword is None or used_entries.get(sf.entry_id, 0) >= 2:
            continue
        chosen.append((sf, keyword))
        used_keywords.add(keyword.lower())
        used_entries[sf.entry_id] = used_entries.get(sf.entry_id, 0) + 1
    if len(chosen) < 2:  # not enough listing matches: fall back to the most relevant verified facts
        for sf in scored:
            if len(chosen) >= 2:
                break
            if all(sf.fact.id != c[0].fact.id for c in chosen):
                chosen.append((sf, None))
    return chosen


def motivation_sentence(profile: CandidateProfile, kw: JobKeywords, job_title: str) -> str:
    themes = [t for t in profile.preferences.preferred_themes if t.strip() and kw.mentions(t)]
    if not themes:
        return f"I would welcome the opportunity to bring this experience to the {job_title} role."
    joined = themes[0] if len(themes) == 1 else ", ".join(themes[:-1]) + f" and {themes[-1]}"
    return (f"The listing's references to {joined} match the areas I most want to work in, "
            f"and I would welcome the opportunity to bring this experience to the {job_title} role.")


CLOSING_PARAGRAPH = ("Thank you for considering my application. I would welcome the opportunity to discuss how my "
                     "experience could contribute to your team.")


def template_cover_letter(profile: CandidateProfile, kw: JobKeywords, *, job_title: str, employer: str,
                          on: date) -> tuple[CoverLetterContent, list[Claim], list[Issue]]:
    issues: list[Issue] = []
    body_items = _pick_body_facts(profile, kw)
    if sum(1 for _, k in body_items if k) < 2:
        issues.append(Issue(code="few_listing_matches", severity="info", document="cover_letter",
                            message="Fewer than two verified facts match keywords in the listing; the letter uses "
                                    "your most relevant facts instead."))
    claims: list[Claim] = []
    body_sentences = []
    previous_entry: str | None = None
    for n, (sf, keyword) in enumerate(body_items):
        sentence = body_sentence(profile, sf, keyword, first=n == 0, same_entry=sf.entry_id == previous_entry)
        previous_entry = sf.entry_id
        body_sentences.append(sentence)
        claims.append(Claim(text=sentence, fact_ids=[sf.fact.id], generator="rules",
                            section=f"cover_letter:{sf.entry_kind}:{sf.entry_id}"))
    opening = opening_paragraph(profile, job_title=job_title, employer=employer)
    edu = _primary_education(profile)
    claims.insert(0, Claim(text=opening, fact_ids=[], generator="rules", section=f"cover_letter:opening:{edu.id}"))
    paragraphs = [opening]
    if body_sentences:
        paragraphs.append(" ".join(body_sentences))
    paragraphs.append(motivation_sentence(profile, kw, job_title))
    paragraphs.append(CLOSING_PARAGRAPH)
    content = CoverLetterContent(
        header=build_header(profile), date_text=format_letter_date(on), employer=employer, job_title=job_title,
        greeting=GREETING, paragraphs=paragraphs, closing=CLOSING, signature=profile.personal.full_name,
    )
    return content, claims, issues


# --------------------------------------------------------------------------- #
# LLM draft validation
# --------------------------------------------------------------------------- #
_CLAIM_VERB = re.compile(r"(?<![A-Za-z])I\s+(?:have\s+)?[a-z]+ed(?![A-Za-z])")
_PROFICIENCY = re.compile(
    r"(?<![A-Za-z])(expert|expertise|proficient|proficiency|mastery|skilled|advanced|extensive|in-depth|deep|"
    r"seasoned|veteran|specialist|fluent|years\s+of|track\s+record|proven)(?![A-Za-z])",
    re.IGNORECASE,
)
_GREETING_RE = re.compile(r"^\s*(dear|hello|hi|to whom)\b", re.IGNORECASE)
_SIGNOFF_RE = re.compile(r"^\s*(sincerely|best regards|regards|kind regards|thank you,|warm regards|respectfully)\b",
                         re.IGNORECASE)


def _is_claim_sentence(sentence: str) -> bool:
    return bool(validation._NUM_RE.search(sentence) or lexicon.find_skills(sentence)
                or validation._LEADERSHIP.search(sentence) or validation._CREDENTIAL.search(sentence)
                or _CLAIM_VERB.search(sentence))


def validate_llm_draft(paragraphs: Sequence[str], fact_ids_used: Sequence[str], allowed_facts: Sequence[Fact],
                       profile: CandidateProfile, kw: JobKeywords, *, job_title: str,
                       employer: str) -> tuple[list[str], list[Claim], list[str]]:
    """Validate an LLM cover-letter draft sentence by sentence.

    Returns ``(clean paragraphs, claims, problems)``; any problem means the
    draft must not be used.
    """
    problems: list[str] = []
    allowed = {f.id: f for f in allowed_facts}
    unknown = [fid for fid in fact_ids_used if fid not in allowed]
    if unknown:
        problems.append(f"draft cites fact ids that were not provided: {unknown}")
    cited = [allowed[fid] for fid in fact_ids_used if fid in allowed]
    paras = [normalize_space(p) for p in paragraphs if p and p.strip()]
    while paras and _GREETING_RE.match(paras[0]) and len(words(paras[0])) <= 6:
        paras.pop(0)
    while paras and (_SIGNOFF_RE.match(paras[-1]) or paras[-1] == profile.personal.full_name):
        paras.pop()
    total_words = sum(len(words(p)) for p in paras)
    if not 2 <= len(paras) <= 6:
        problems.append(f"expected 2-6 paragraphs, got {len(paras)}")
    if not 80 <= total_words <= 450:
        problems.append(f"letter length {total_words} words is outside 80-450")
    if not cited:
        problems.append("draft cites no verified facts")

    edu_text = "\n".join(f"{e.degree} {e.field or ''} {e.institution} {e.start or ''} {e.end or ''}"
                         for e in profile.education)
    profile_text = "\n".join([*validation.organizations(profile), *profile.skills, edu_text,
                              *profile.preferences.preferred_themes, profile.personal.full_name,
                              validation.credential_text(profile)])
    context_text = "\n".join([job_title, employer, edu_text])
    claims: list[Claim] = []
    for para in paras:
        for sentence in split_sentences(para):
            issues: list[Issue] = []
            mapped = [f for f in cited if _maps_to(sentence, f)]
            source = "\n".join(f.text for f in (mapped or cited))
            issues += validation.check_numbers(sentence, source, extra_sources=context_text)
            issues += validation.check_skills(sentence, mapped or cited, profile, strict=False,
                                              extra_allowed=[job_title, employer])
            issues += validation.check_leadership(sentence, source + "\n" + job_title)
            issues += validation.check_credentials(sentence, source, profile)
            issues += validation.check_forbidden_content(sentence)
            issues += validation.check_organizations(
                sentence, source, profile, allowed_text="\n".join([profile_text, context_text, kw.text]))
            issues += validation.check_company_claims(sentence, employer=employer, listing_text=kw.text,
                                                      profile_text=profile_text + "\n" + source)
            for m in sorted({m.group(0).lower() for m in _PROFICIENCY.finditer(sentence)}):
                if not mentions(source, m):
                    issues.append(Issue(code="unverified_proficiency",
                                        message=f"'{m}' claims a proficiency level no verified fact states"))
            if _is_claim_sentence(sentence) and not mapped and not _only_context(sentence,
                                                                                 context_text + "\n" + kw.text):
                issues.append(Issue(code="unsupported_claim", message="claim-bearing sentence matches no cited fact"))
            if issues:
                problems.append(f"'{sentence[:80]}': " + "; ".join(validation.summarize(issues)))
            elif mapped:
                claims.append(Claim(text=sentence, fact_ids=[f.id for f in mapped], generator="llm",
                                    section="cover_letter:body"))
    return paras, claims, problems


def _maps_to(sentence: str, fact: Fact) -> bool:
    fact_words = content_set(fact.text)
    if not fact_words:
        return False
    shared = content_set(sentence) & fact_words
    if len(shared) / len(fact_words) >= 0.3:
        return True
    fact_numbers = {n[1] for n in validation._numbers(fact.text)}
    return bool(fact_numbers & {n[1] for n in validation._numbers(sentence)})


def _only_context(sentence: str, allowed_text: str) -> bool:
    """A sentence that only restates the listing / job title / education (e.g. the opening).

    Every content word must come from ``allowed_text`` or neutral letter wording;
    numbers, skills, proficiency and leadership words are checked separately.
    """
    allowed = content_set(allowed_text) | validation.LETTER_VOCABULARY
    return all(w in allowed for w in content_words(sentence)) and not validation._LEADERSHIP.search(sentence)


def build_cover_letter(profile: CandidateProfile, kw: JobKeywords, *, job_title: str, employer: str, on: date,
                       drafter: Any = None, model_label: str | None = None) -> GeneratedCoverLetter:
    """Template letter, or a validated LLM draft when ``drafter`` is given and its draft passes every check.

    ``drafter(employer=, job_title=, job_highlights=, facts=, candidate_name=)`` returns an object with
    ``.paragraphs`` and ``.fact_ids_used``, or ``None`` when the LLM failed (the wrapper records the error).
    """
    title = display_text(job_title, max_len=120) or "advertised"
    company = display_text(employer, max_len=100) or "your organization"
    content, claims, issues = template_cover_letter(profile, kw, job_title=title, employer=company, on=on)
    if injection_suspected(job_title) or injection_suspected(employer):
        issues.append(Issue(code="listing_text_suspicious", severity="review", document="cover_letter",
                            message="The job title or employer name contains instruction-like text; check the "
                                    "opening sentence."))
    generator = "rules"
    if drafter is not None:
        facts = [sf.fact for sf, _ in _pick_body_facts(profile, kw)]
        extra = [sf.fact for sf in sorted(candidate_facts(profile, kw), key=lambda s: -s.score)[:6]
                 if sf.fact not in facts]
        facts = (facts + extra)[:6]
        highlights = [display_text(r, max_len=200) for r in kw.requirements[:8] if not injection_suspected(r)]
        draft = drafter(employer=company, job_title=title, job_highlights=highlights, facts=facts,
                        candidate_name=profile.personal.full_name)
        if draft is None:
            problems = ["the local LLM was unavailable or returned an error"]
            paras, llm_claims = [], []
        else:
            paras, llm_claims, problems = validate_llm_draft(
                [str(p) for p in (getattr(draft, "paragraphs", None) or [])],
                [str(f) for f in (getattr(draft, "fact_ids_used", None) or [])],
                facts, profile, kw, job_title=title, employer=company)
        if not problems:
            label = f"llm:{model_label}" if model_label else "llm"
            content = content.model_copy(update={"paragraphs": paras})
            claims = [c.model_copy(update={"generator": label}) for c in llm_claims]
            generator = label
            issues = [i for i in issues if i.code != "few_listing_matches"]
        else:
            issues.append(Issue(code="llm_cover_letter_rejected", severity="info", document="cover_letter",
                                message="The local-LLM draft was rejected and the template letter was used: "
                                        + " | ".join(problems[:5])))
    issues += validation.validate_cover_letter(content, claims, profile, listing_text=kw.text)
    issues.append(Issue(code="cover_letter_review", severity="review", document="cover_letter",
                        message="Cover letters always need your review before use."))
    return GeneratedCoverLetter(content=content, claims=claims, issues=issues, status=DocumentStatus.NEEDS_REVIEW,
                                generator=generator)
