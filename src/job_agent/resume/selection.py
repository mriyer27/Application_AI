"""Deterministic selection of verified facts for a job.

* Experiences and projects keep the profile's order (written reverse-chronological).
* Within each entry, up to ``max_bullets_per_role`` facts are chosen by relevance
  to the job keywords and ordered by relevance (ties keep profile order).
* The skills section lists ONLY ``profile.skills``; relevant ones first.
* Education is always included; dates are the profile's verbatim values
  ("Expected <end>" when ``expected`` is true). Graduation dates are never altered.
* Work-authorization / visa information is never read here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from job_agent.profile import CandidateProfile, Education, Fact
from job_agent.resume import lexicon
from job_agent.resume.keywords import JobKeywords
from job_agent.resume.models import Bullet, EducationEntry, ExperienceEntry, Header, ResumeContent
from job_agent.resume.text import content_words, normalize_space

_SKILL_HIT, _SKILL_REQ_BONUS, _KEYWORD_HIT, _TEXT_HIT, _TEXT_CAP = 3.0, 1.0, 2.0, 0.5, 3.0


@dataclass
class ScoredFact:
    fact: Fact
    score: float
    index: int  # position within its entry (tie-break)
    matched: list[str] = field(default_factory=list)  # profile strings found in the listing
    entry_id: str = ""
    entry_kind: str = ""


def fact_text(fact: Fact) -> str:
    return normalize_space(fact.text)


def score_fact(fact: Fact, kw: JobKeywords, index: int = 0) -> ScoredFact:
    score = 0.0
    matched: list[str] = []
    seen_lex: set[str] = set()
    for skill in fact.skills:
        names = lexicon.find_skills(skill)
        weight = max((kw.skill_weights.get(n, 0.0) for n in names), default=0.0)
        if weight > 0 or kw.mentions(skill):
            score += _SKILL_HIT + (_SKILL_REQ_BONUS if weight >= 2.0 else 0.0)
            seen_lex.update(names)
            if kw.mentions(skill):
                matched.append(skill)
            else:  # the listing names it via a lexicon alias -> show the canonical name
                matched.extend(n for n in names if n in kw.skill_weights and n not in matched)
    for name in lexicon.find_skills(fact.text):
        if name not in seen_lex and name in kw.skill_weights:
            score += _KEYWORD_HIT
            seen_lex.add(name)
            if name not in matched:
                matched.append(name)
    for keyword in fact.keywords:
        if kw.mentions(keyword):
            score += _KEYWORD_HIT
            if keyword not in matched:
                matched.append(keyword)
        else:
            stems = content_words(keyword)
            if stems and all(kw.has_stem(s) for s in stems):
                score += _KEYWORD_HIT / 2
    text_hits = sum(1 for w in set(content_words(fact.text)) if kw.has_stem(w))
    score += min(_TEXT_CAP, _TEXT_HIT * text_hits)
    return ScoredFact(fact=fact, score=round(score, 3), index=index, matched=matched)


def rank_facts(facts: list[Fact], kw: JobKeywords, limit: int) -> list[ScoredFact]:
    """Top ``limit`` facts by relevance, ordered by relevance (ties: profile order)."""
    scored = [score_fact(f, kw, i) for i, f in enumerate(facts)]
    scored.sort(key=lambda s: (-s.score, s.index))
    return scored[:limit]


def _bullet(s: ScoredFact) -> Bullet:
    return Bullet(text=fact_text(s.fact), fact_ids=[s.fact.id], generator="rules", relevance=s.score,
                  matched_keywords=s.matched)


def education_dates(edu: Education) -> str:
    """Dates exactly as verified in the profile; "Expected <end>" for expected degrees."""
    end = edu.end.strip() if edu.end else None
    if end and edu.expected and not end.lower().startswith("expected"):
        end = f"Expected {end}"
    start = edu.start.strip() if edu.start else None
    if start and end:
        return f"{start} – {end}"
    return end or start or ""


def skill_relevance(skill: str, kw: JobKeywords) -> float:
    weight = max((kw.skill_weights.get(n, 0.0) for n in lexicon.find_skills(skill)), default=0.0)
    if kw.mentions(skill):
        weight = max(weight, 2.0)
    else:
        stems = content_words(skill)
        if len(stems) >= 2 and sum(1 for s in stems if kw.has_stem(s)) * 3 >= len(stems) * 2:
            weight = max(weight, 0.5)
    return weight


def order_skills(skills: list[str], kw: JobKeywords) -> list[str]:
    """``profile.skills`` only (verbatim), relevant to the listing first, then profile order."""
    scored = [(skill_relevance(s, kw), i, s) for i, s in enumerate(skills)]
    relevant = sorted((t for t in scored if t[0] > 0), key=lambda t: (-t[0], t[1]))
    rest = [t for t in scored if t[0] <= 0]
    return [s for _, _, s in relevant + rest]


def build_header(profile: CandidateProfile) -> Header:
    p = profile.personal
    links = [u for u in (p.linkedin_url, p.github_url, p.portfolio_url) if u]
    return Header(full_name=p.full_name, email=p.email, phone=p.phone, location=p.location, links=links)


def select_resume_content(profile: CandidateProfile, kw: JobKeywords, *, max_bullets_per_role: int) -> ResumeContent:
    education = [
        EducationEntry(
            id=e.id, institution=e.institution, degree=e.degree, field=e.field, location=e.location,
            dates=education_dates(e), gpa=e.gpa,
            bullets=[_bullet(s) for s in rank_facts(e.facts, kw, max_bullets_per_role)],
        )
        for e in profile.education
    ]
    experience = [
        ExperienceEntry(
            id=x.id, kind="experience", title=x.title, organization=x.employer, team=x.team, location=x.location,
            dates=x.dates, bullets=[_bullet(s) for s in rank_facts(x.facts, kw, max_bullets_per_role)],
        )
        for x in profile.experience
    ]
    projects = [
        ExperienceEntry(
            id=p.id, kind="project", title=p.name, dates=p.dates,
            bullets=[_bullet(s) for s in rank_facts(p.facts, kw, max_bullets_per_role)],
        )
        for p in profile.projects
    ]
    certifications = []
    for c in profile.certifications:
        text = c.name + (f", {c.issuer}" if c.issuer else "") + (f" ({c.date})" if c.date else "")
        certifications.append(text)
    return ResumeContent(
        header=build_header(profile),
        summary=normalize_space(profile.summary) if profile.summary else None,
        education=education,
        experience=experience,
        projects=projects,
        skills=order_skills(list(profile.skills), kw),
        certifications=certifications,
    )


def candidate_facts(profile: CandidateProfile, kw: JobKeywords) -> list[ScoredFact]:
    """Every verified fact scored against the job, with the entry it belongs to (for cover letters)."""
    out: list[ScoredFact] = []
    for x in profile.experience:
        for i, f in enumerate(x.facts):
            s = score_fact(f, kw, i)
            s.entry_id, s.entry_kind = x.id, "experience"
            out.append(s)
    for p in profile.projects:
        for i, f in enumerate(p.facts):
            s = score_fact(f, kw, i)
            s.entry_id, s.entry_kind = p.id, "project"
            out.append(s)
    for e in profile.education:
        for i, f in enumerate(e.facts):
            s = score_fact(f, kw, i)
            s.entry_id, s.entry_kind = e.id, "education"
            out.append(s)
    return out
