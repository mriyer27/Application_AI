"""Explainable, deterministic job scoring.

``score_job`` is a pure function of (job row, verified profile, config,
optional validated local-LLM analysis): the same inputs always give the same
``ScoreResult``. It never reads the clock, the network or the database.

Seven factors are scored 0..1, each with a concrete explanation, and combined
with ``config.scoring.weights.normalized()`` (weights sum to 100) into a 0..100
overall score. Eligibility flags are computed separately (``eligibility.py``)
and influence the category / shortlist, never the factor arithmetic.

Whether the candidate *has* a qualification is decided only against the
verified profile (skills list and fact ids), never by the LLM.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping

from job_agent.models import CodingIntensity, EligibilityFlag, FactorScore, JobAnalysis, MatchCategory, ScoreResult
from job_agent.scoring import eligibility as elig
from job_agent.scoring.skills import SOFT, TOOL, ProfileSkillIndex, SkillEvidence, build_profile_index
from job_agent.scoring.text import (
    DEGREE_NAMES,
    DOCTORATE,
    as_list,
    contains_phrase,
    dedupe,
    degree_level_of,
    degree_levels,
    phrase_regex,
    sentences,
    short_list,
)

if TYPE_CHECKING:  # pragma: no cover
    from job_agent.config import AppConfig
    from job_agent.profile import CandidateProfile

SCORING_VERSION = "rules-1.1"
LLM_VERSION_SUFFIX = "+llm"
#: Descriptions shorter than this are "insufficient information" -> needs_review.
MIN_DESCRIPTION_CHARS = 300

FACTOR_NAMES = ("career_interest", "skills", "experience_level", "domain_relevance", "location", "compensation",
                "employer_growth")

REQUIRED_WEIGHT = 1.0
PREFERRED_WEIGHT = 0.4
SOFT_SKILL_MULTIPLIER = 0.5
HOURS_PER_YEAR = 2080
_PERIOD_MULTIPLIER = {"year": 1, "month": 12, "week": 52, "hour": HOURS_PER_YEAR}
_PERIOD_WORD = {"month": "monthly x 12", "week": "weekly x 52", "hour": f"hourly x {HOURS_PER_YEAR} hours"}


@dataclass
class _Factor:
    name: str
    score: float
    explanation: str
    pro: str | None = None  # short reason to apply
    con: str | None = None  # short reason not to apply


@dataclass
class _Findings:
    matching: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    disqualifiers: list[str] = field(default_factory=list)
    evidence: list[dict[str, str]] = field(default_factory=list)
    senior_skip: str | None = None  # "Skip - ..." recommendation


def _r(value: float) -> float:
    return round(min(1.0, max(0.0, value)), 3)


def _job_dict(job: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return dict(job)
    except (TypeError, ValueError):  # pragma: no cover - exotic mappings
        return {k: job[k] for k in job.keys()}  # type: ignore[attr-defined]


def scoring_config_hash(config: "AppConfig") -> str:
    """Hash of the settings that influence scores (search + scoring sections)."""
    blob = {
        "search": config.search.model_dump(mode="json", exclude={"max_jobs_per_run", "max_posting_age_days"}),
        "scoring": config.scoring.model_dump(mode="json", exclude={"use_llm", "max_llm_jobs_per_run"}),
    }
    return hashlib.sha256(json.dumps(blob, sort_keys=True).encode()).hexdigest()[:16]


def base_version(version: str | None) -> str:
    return (version or "").split("+", 1)[0]


# --------------------------------------------------------------------------- #
# career_interest
# --------------------------------------------------------------------------- #
_LEVEL_WORDS = (r"associate|junior|jr|entry[\s\-]level|entry|graduate|new\s+grad(?:uate)?|senior|sr|staff|principal|"
                r"lead|intern|early[\s\-]career")
_LEVEL_PREFIX = re.compile(rf"^(?:(?:{_LEVEL_WORDS})\s+)+", re.I)
_LEVEL_SUFFIX = re.compile(r"(?:\s+(?:i{1,3}|iv|[1-4]|intern(?:ship)?|level\s+[1-4i]+))+$", re.I)
_TITLE_SEPARATOR = re.compile(r"\s+[-–—|]\s*|\s*[,(:]")
_STOPWORDS = {"and", "of", "the", "a", "an", "for", "to", "in", "with", "on", "at"}

_ENGINEERING_TITLE = re.compile(
    r"\b(?:software|backend|back[\s\-]end|frontend|front[\s\-]end|full[\s\-]?stack|platform|infrastructure|devops|"
    r"site\s+reliability|security|ml|machine\s+learning|mobile|ios|android|embedded|firmware|systems|cloud|data|qa|"
    r"test|automation)\s+(?:engineer|developer|engineering)\b|\b(?:swe|sde|sre|programmer|developer)\b|"
    r"\bsoftware\s+engineering\b",
    re.I,
)
_ENGINEERING_PHRASES: dict[str, re.Pattern[str]] = {
    label: re.compile(rx, re.I)
    for label, rx in {
        "production code": r"\bproduction(?:[\s\-]quality)?\s+code\b",
        "writing code": r"\bwrit(?:e|ing)\s+(?:clean|scalable|maintainable|efficient|well[\s\-]tested|high[\s\-]quality)\s+code\b",
        "code reviews": r"\bcode\s+reviews?\b",
        "microservices": r"\bmicro[\s\-]?services\b",
        "distributed systems": r"\bdistributed\s+systems\b",
        "CI/CD": r"\bci\s*/\s*cd\b",
        "software development lifecycle": r"\bsoftware\s+development\s+life\s*cycle\b|\bsdlc\b",
        "unit testing": r"\bunit\s+tests?\b|\btest[\s\-]driven\b",
        "building services/APIs": r"\bback[\s\-]?end\s+services\b|\bbuild(?:ing)?\s+(?:and\s+maintain(?:ing)?\s+)?(?:apis|services)\b",
        "system design": r"\bsystem\s+design\b",
        "object-oriented programming": r"\bobject[\s\-]oriented\b",
    }.items()
}
_AVOID_BUILTIN: dict[str, re.Pattern[str]] = {
    label: re.compile(rx, re.I)
    for label, rx in {
        "data structures & algorithms": r"\bdata\s+structures\s+(?:and|&)\s+algorithms\b|(?-i:\bDSA\b)",
        "LeetCode": r"\bleet\s*code\b",
        "HackerRank": r"\bhacker\s*rank\b",
        "competitive programming": r"\bcompetitive\s+(?:programming|coding)\b",
        "coding challenges / interviews": r"\bcoding\s+(?:challenges?|interviews?|assessments?)\b|\blive\s+coding\b|"
                                          r"\bwhiteboard(?:ing)?\s+(?:interviews?|coding)\b",
    }.items()
}


def _norm_title(text: str) -> str:
    t = text.lower().replace("&", " and ")
    t = re.sub(r"[^a-z0-9+#/ ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = _LEVEL_PREFIX.sub("", t)
    return _LEVEL_SUFFIX.sub("", t).strip()


def _tokens(text: str) -> set[str]:
    return {w for w in re.split(r"[\s/]+", text) if w and w not in _STOPWORDS}


def _career_interest(data: dict[str, Any], profile: "CandidateProfile", config: "AppConfig",
                     analysis: JobAnalysis | None) -> _Factor:
    title = str(data.get("title") or "").strip()
    description = str(data.get("description_text") or "")
    full = _norm_title(title)
    base = _norm_title(_TITLE_SEPARATOR.split(title, maxsplit=1)[0])
    targets = dedupe([*config.search.target_titles, *profile.preferences.target_roles])

    best_score, best_target, how = 0.0, "", "none"
    for target in targets:
        tn = _norm_title(target)
        if not tn:
            continue
        if tn in {full, base}:
            cand, kind = 1.0, "exact"
        elif full and phrase_regex(tn).search(full):
            cand, kind = 0.85, "contains"
        else:
            tt, wt = _tokens(tn), _tokens(full)
            overlap = len(tt & wt) / len(tt) if tt else 0.0
            cand, kind = (_r(0.2 + 0.5 * overlap), "overlap") if overlap > 0 else (0.0, "none")
        if cand > best_score:
            best_score, best_target, how = cand, target, kind

    keywords = [k for k in config.search.title_keywords if contains_phrase(title, k)]
    score = best_score
    if how == "exact":
        why = f"title '{title}' matches target role '{best_target}'"
    elif how == "contains":
        why = f"title '{title}' contains target role '{best_target}'"
    else:
        if how == "overlap":
            why = f"title '{title}' shares words with target role '{best_target}'"
        else:
            why = f"title '{title}' does not resemble your target roles"
        if keywords and score < 0.5:
            score = 0.5
            why = f"title '{title}' contains your title keyword(s) {short_list(keywords, 4)}"
        score = min(score, 0.7)
        if score <= 0:
            score = 0.1

    penalties: list[tuple[float, str]] = []
    if _ENGINEERING_TITLE.search(title):
        penalties.append((0.4, "title indicates a software-engineering role"))
    else:
        eng = [label for label, rx in _ENGINEERING_PHRASES.items() if rx.search(description)]
        if len(eng) >= 3:
            penalties.append((0.15, f"description emphasizes software-engineering work ({short_list(eng, 4)})"))
    avoid = [label for label, rx in _AVOID_BUILTIN.items() if rx.search(description) or rx.search(title)]
    for phrase in profile.preferences.avoid_focus:
        if phrase.lower() not in {a.lower() for a in avoid} and (
                contains_phrase(description, phrase) or contains_phrase(title, phrase)):
            avoid.append(phrase)
    if avoid:
        penalties.append((min(0.3, 0.1 * len(avoid)),
                          f"listing mentions topics you prefer to avoid ({short_list(avoid, 4)})"))
    if analysis is not None and analysis.coding_intensity == CodingIntensity.HIGH:
        penalties.append((0.1, "local AI analysis rates the coding intensity as high"))

    final = max(0.05, _r(score - sum(p for p, _ in penalties)))
    explanation = why[0].upper() + why[1:] + f" ({score:.2f})"
    if penalties:
        explanation += "".join(f"; minus {p:.2f}: {reason}" for p, reason in penalties)
        explanation += " (ordinary coding mentions such as Python/SQL are not penalized; floor 0.05)"
    explanation += f" => {final:.2f}."
    pro = f"Title matches your target role '{best_target}'" if how in {"exact", "contains"} and final >= 0.8 else None
    con = None
    if final <= 0.4:
        con = (penalties[0][1].capitalize() if penalties else f"Title '{title}' is not one of your target roles")
    return _Factor("career_interest", _r(final), explanation, pro, con)


# --------------------------------------------------------------------------- #
# skills
# --------------------------------------------------------------------------- #
@dataclass
class _Requirement:
    label: str
    category: str
    preferred: bool
    evidence: SkillEvidence
    from_llm: bool = False

    @property
    def weight(self) -> float:
        w = PREFERRED_WEIGHT if self.preferred else REQUIRED_WEIGHT
        return w * (SOFT_SKILL_MULTIPLIER if self.category == SOFT else 1.0)


def _requirements(data: dict[str, Any], index: ProfileSkillIndex,
                  analysis: JobAnalysis | None) -> tuple[list[_Requirement], str]:
    required_quals = as_list(data.get("required_qualifications"))
    preferred_quals = as_list(data.get("preferred_qualifications"))
    if required_quals:
        required = index.extract("\n".join(required_quals))
        source = "the listed required qualifications"
    else:
        required = index.extract(str(data.get("description_text") or ""))
        source = "the description (no separate requirements list)"
    preferred = [n for n in index.extract("\n".join(preferred_quals)) if n not in required]

    reqs = [_Requirement(n, index.category(n), False, index.evidence(n)) for n in required]
    reqs += [_Requirement(n, index.category(n), True, index.evidence(n)) for n in preferred]
    if analysis is not None:
        known = {r.label.lower() for r in reqs}
        for labels, is_preferred in ((analysis.required_skills, False), (analysis.preferred_skills, True)):
            for label in labels:
                names = index.extract(label)
                if names:
                    for n in names:
                        if n.lower() not in known:
                            known.add(n.lower())
                            reqs.append(_Requirement(n, index.category(n), is_preferred, index.evidence(n), True))
                elif label.strip() and label.strip().lower() not in known:
                    known.add(label.strip().lower())
                    reqs.append(_Requirement(label.strip(), TOOL, is_preferred, index.free_text_evidence(label), True))
    return reqs, source


def _skills(data: dict[str, Any], index: ProfileSkillIndex, profile: "CandidateProfile",
            analysis: JobAnalysis | None, findings: _Findings) -> _Factor:
    reqs, source = _requirements(data, index, analysis)
    if not reqs:
        return _Factor("skills", 0.5,
                       "Insufficient information: no recognizable skills or tools in the listing; neutral 0.50.")
    total = sum(r.weight for r in reqs)
    matched_w = sum(r.weight for r in reqs if r.evidence.supported)
    score = _r(matched_w / total) if total else 0.5

    for r in reqs:
        kind = "preferred" if r.preferred else "required"
        tag = f"{kind}{', from local AI analysis' if r.from_llm else ''}"
        if r.evidence.supported:
            findings.matching.append(f"{r.label} ({tag}; {r.evidence.describe()})")
            for fid in r.evidence.fact_ids[:3]:
                fact = profile.fact_by_id(fid)
                if fact is not None:
                    findings.evidence.append({"requirement": r.label, "fact_id": fid, "fact_text": fact.text})
        else:
            findings.missing.append(f"{r.label} ({tag}; no evidence in verified profile)")

    def summary(items: list[_Requirement]) -> str:
        have = [r.label for r in items if r.evidence.supported]
        lack = [r.label for r in items if not r.evidence.supported]
        text = f"{len(have)}/{len(items)} with verified evidence"
        if have:
            text += f" (matched: {short_list(have)})"
        if lack:
            text += f" (missing: {short_list(lack)})"
        return text

    required = [r for r in reqs if not r.preferred]
    preferred = [r for r in reqs if r.preferred]
    parts = []
    if required:
        parts.append(f"Required skills from {source}: {summary(required)}")
    else:
        parts.append(f"No required skills recognized in {source}")
    if preferred:
        parts.append(f"preferred: {summary(preferred)}")
    explanation = "; ".join(parts) + (
        f". Weighted share {matched_w:.2f}/{total:.2f} = {score:.2f} (required x{REQUIRED_WEIGHT:g}, "
        f"preferred x{PREFERRED_WEIGHT:g}, soft skills x{SOFT_SKILL_MULTIPLIER:g})."
    )
    have_req = sum(1 for r in required if r.evidence.supported)
    pro = (f"Verified evidence for {have_req} of {len(required)} required skills" if required and score >= 0.7
           else None)
    lacking = [r.label for r in required if not r.evidence.supported]
    con = (f"No verified evidence for {len(lacking)} of {len(required)} required skills ({short_list(lacking, 5)})"
           if lacking and score <= 0.5 else None)
    return _Factor("skills", score, explanation, pro, con)


# --------------------------------------------------------------------------- #
# experience_level (incl. degree level)
# --------------------------------------------------------------------------- #
_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
              "ten": 10}
_NUM = r"\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten"
_YEARS_RE = re.compile(
    rf"(?P<prefix>(?:at\s+least|minimum(?:\s+of)?|min\.?|over|more\s+than)\s+)?(?<![\d.])(?P<a>{_NUM})\)?\s*"
    rf"(?:\+|plus)?\s*(?:(?:-|–|—|to)\s*(?P<b>{_NUM})\)?\s*\+?\s*)?(?:years?|yrs?)\b(?=(?P<ctx>[^.;\n]{{0,60}}))",
    re.I,
)
_YEARS_NOT_EXPERIENCE = re.compile(r"^\s*(?:old|ago|of\s+age)\b", re.I)
_PREFERRED_HINT = re.compile(
    r"\b(?:preferred|a\s+plus|nice[\s\-]to[\s\-]have|bonus|ideally|desired|desirable|advantageous)\b", re.I)
_REQUIREMENT_HINT = re.compile(r"\b(?:required|requires?|requirements?|must|minimum|need|needs|qualifications?)\b",
                               re.I)

_SENIOR_TITLE = re.compile(r"\b(?:senior|sr|staff|principal|director|head\s+of|vice\s+president|vp|svp|evp|chief|"
                           r"distinguished)\b", re.I)
_LEAD_TITLE = re.compile(r"\blead\b", re.I)
_PEOPLE_MANAGER_TITLE = re.compile(
    r"^\s*manager\b|\b(?:engineering|analytics|data|team|people|department|general|regional|district|store|"
    r"operations|sales|delivery)\s+manager\b|\bmanager\s+of\b", re.I)
_HIGH_LEVEL_TITLE = re.compile(r"(?-i:\b(?:III|IV)\b)")
_MID_TITLE = re.compile(r"(?-i:\bII\b)")
_ENTRY_TITLE = re.compile(r"\b(?:entry[\s\-]level|entry|new\s+grad(?:uate)?|graduate|early[\s\-]career|"
                          r"intern(?:ship)?|campus|apprentice|trainee|university)\b", re.I)
_JUNIOR_TITLE = re.compile(r"\b(?:associate|junior|jr)\b", re.I)
_ENTRY_TEXT = re.compile(r"\b(?:entry[\s\-]level|new\s+grad(?:uate)?s?|recent\s+(?:college\s+|university\s+)?"
                         r"grad(?:uate)?s?|early[\s\-]career|university\s+grad(?:uate)?s?|"
                         r"no\s+(?:prior\s+)?experience\s+(?:is\s+)?(?:required|necessary))\b", re.I)
_PEOPLE_MANAGER_TEXT = re.compile(r"\b(?:manag(?:e|ing)\s+a\s+team|direct\s+reports|people\s+management|"
                                  r"lead\s+a\s+team\s+of|build\s+and\s+lead\s+a\s+team|"
                                  r"hire(?:,|\s+and)\s+(?:mentor|manage|develop)\s+(?:a\s+)?team)\b", re.I)
_LEVEL_COLUMN = {
    "intern": 1.0, "internship": 1.0, "entry": 1.0, "new_grad": 1.0, "early_career": 1.0, "junior": 0.9,
    "associate": 0.9, "mid": 0.6, "senior": 0.15, "lead": 0.25, "staff": 0.15, "principal": 0.15,
    "director": 0.1, "manager": 0.2,
}
_ANALYSIS_SENIORITY = {
    "intern": 1.0, "entry": 1.0, "junior": 0.9, "associate": 0.9, "new grad": 1.0, "mid": 0.6, "senior": 0.15,
    "lead": 0.25, "staff": 0.15, "principal": 0.15, "director": 0.1, "manager": 0.2, "executive": 0.1,
}


#: Fit of an explicit "N+ years" minimum for an early-career candidate (internships
#: plus well under two years of professional experience).
_YEARS_SCALE = {0: 1.0, 1: 0.95, 2: 0.8, 3: 0.55, 4: 0.3}


def years_score(years: int) -> float:
    return _YEARS_SCALE.get(max(years, 0), 0.1)


def _num(text: str) -> int:
    return int(text) if text.isdigit() else _NUM_WORDS[text.lower()]


def _sentence_years(sentence: str) -> tuple[int, str] | None:
    values: list[tuple[int, str]] = []
    for m in _YEARS_RE.finditer(sentence):
        ctx = m.group("ctx") or ""
        if _YEARS_NOT_EXPERIENCE.search(ctx):
            continue
        before = sentence[max(0, m.start() - 40):m.start()]
        if not (m.group("prefix") or "experienc" in ctx.lower() or "experienc" in before.lower()):
            continue
        low = _num(m.group("a"))
        if m.group("b"):
            low = min(low, _num(m.group("b")))
        phrase = sentence[m.start():m.end()].strip()
        values.append((low, phrase))
    if not values:
        return None
    if len(values) > 1 and re.search(r"\bor\b", sentence, re.I):
        return min(values, key=lambda v: v[0])
    return max(values, key=lambda v: v[0])


def explicit_years(data: Mapping[str, Any]) -> tuple[int, str] | None:
    """Required years of experience stated in the listing: ``(years, verbatim phrase)``.

    The maximum over requirement sentences; within one sentence offering
    alternatives ("4 years, or 2 years with a Master's") the minimum.
    """
    required = as_list(data.get("required_qualifications"))
    preferred = {p.strip().lower() for p in as_list(data.get("preferred_qualifications"))}
    candidates = [s for q in required for s in sentences(q)]
    for s in sentences(str(data.get("description_text") or "")):
        if s.strip().lower() not in preferred and not any(s.strip().lower() in p for p in preferred):
            candidates.append(s)
    best: tuple[int, str] | None = None
    for s in candidates:
        if _PREFERRED_HINT.search(s):
            continue
        years = _sentence_years(s)
        if years is not None and (best is None or years[0] > best[0]):
            best = years
    return best


def _degree_requirement(data: dict[str, Any]) -> tuple[int, str] | None:
    required = as_list(data.get("required_qualifications"))
    from_list = bool(required)
    texts = [s for q in required for s in sentences(q)] if from_list else sentences(str(data.get("description_text") or ""))
    best: tuple[int, str] | None = None
    for s in texts:
        levels = degree_levels(s)
        if not levels:
            continue
        if not from_list and not _REQUIREMENT_HINT.search(s):
            continue
        alternatives = len(levels) > 1 and (re.search(r"\bor\b", s, re.I) or "/" in s)
        if _PREFERRED_HINT.search(s):
            if len(levels) < 2:
                continue
            level = min(levels)
        else:
            level = min(levels) if alternatives else max(levels)
        if best is None or level > best[0]:
            best = (level, s)
    return best


def _experience(data: dict[str, Any], profile: "CandidateProfile", analysis: JobAnalysis | None,
                findings: _Findings) -> _Factor:
    title = str(data.get("title") or "")
    description = str(data.get("description_text") or "")
    parts: list[str] = []
    score: float | None = None
    con: str | None = None

    years = explicit_years(data)
    if years is not None:
        score = years_score(years[0])
        parts.append(f"listing asks for '{years[1]}' (counted as {years[0]}) -> {score:.2f} (0 years 1.0, "
                     "1 year 0.95, 2 years 0.8, 3 years 0.55, 4 years 0.3, 5+ years 0.1; you are early-career)")
        if years[0] > 2:
            findings.missing.append(f"{years[0]}+ years of professional experience (profile: internships / early career)")
        if years[0] >= 5:
            findings.disqualifiers.append(f"Requires {years[0]}+ years of experience")
            findings.senior_skip = f"Skip - requires {years[0]}+ years"
            con = f"Requires {years[0]}+ years of experience"
        elif years[0] >= 3:
            con = f"Asks for {years[0]}+ years of experience"

    title_level: tuple[float, str] | None = None
    if _SENIOR_TITLE.search(title):
        title_level = (0.15, f"senior-level title ('{_SENIOR_TITLE.search(title).group(0)}')")  # type: ignore[union-attr]
    elif _PEOPLE_MANAGER_TITLE.search(title):
        title_level = (0.2, "people-manager title")
    elif _LEAD_TITLE.search(title):
        title_level = (0.25, "lead-level title")
    elif _HIGH_LEVEL_TITLE.search(title):
        title_level = (0.3, "level III/IV title")
    elif _ENTRY_TITLE.search(title):
        title_level = (1.0, f"entry-level title ('{_ENTRY_TITLE.search(title).group(0)}')")  # type: ignore[union-attr]
    elif _JUNIOR_TITLE.search(title):
        title_level = (0.9, f"junior/associate title ('{_JUNIOR_TITLE.search(title).group(0)}')")  # type: ignore[union-attr]
    elif _MID_TITLE.search(title):
        title_level = (0.6, "level II title")

    if title_level is not None:
        if score is None:
            score = title_level[0]
            parts.append(f"{title_level[1]} -> {score:.2f}")
        elif title_level[0] < 0.5 and title_level[0] < score:
            score = title_level[0]
            parts.append(f"{title_level[1]} lowers it to {score:.2f}")
        else:
            parts.append(title_level[1])
        if title_level[0] <= 0.25:
            findings.disqualifiers.append(f"Senior-level role: {title_level[1]}")
            findings.senior_skip = findings.senior_skip or "Skip - senior-level role"
            con = con or f"Senior-level role ({title_level[1]})"

    if score is None:
        level = str(data.get("experience_level") or "").strip().lower().replace("-", "_").replace(" ", "_")
        if level in _LEVEL_COLUMN:
            score = _LEVEL_COLUMN[level]
            parts.append(f"listing experience level '{level}' -> {score:.2f}")
    if score is None:
        m = _ENTRY_TEXT.search(description)
        if m:
            score = 0.9
            parts.append(f"description mentions '{m.group(0)}' -> 0.90")
    if score is None and analysis is not None:
        if analysis.years_experience_required is not None:
            score = years_score(analysis.years_experience_required)
            parts.append(f"local AI analysis reads {analysis.years_experience_required}+ years -> {score:.2f} "
                         "(rules found no explicit requirement)")
        elif analysis.seniority.strip().lower() in _ANALYSIS_SENIORITY:
            score = _ANALYSIS_SENIORITY[analysis.seniority.strip().lower()]
            parts.append(f"local AI analysis reads seniority '{analysis.seniority}' -> {score:.2f} "
                         "(rules found no explicit signal)")
    if score is None:
        score = 0.6
        parts.append("no explicit experience requirement or level found -> neutral 0.60")

    if _PEOPLE_MANAGER_TEXT.search(description) and score > 0.3:
        score = 0.3
        parts.append("description describes managing people -> capped at 0.30")

    degree = _degree_requirement(data)
    if degree is not None:
        score = _apply_degree(degree, profile, score, parts, findings)

    explanation = "; ".join(parts)
    explanation = explanation[0].upper() + explanation[1:] + "."
    pro = "Experience requirement fits an early-career candidate" if score >= 0.9 else None
    return _Factor("experience_level", _r(score), explanation, pro, con if score <= 0.6 else None)


def _apply_degree(degree: tuple[int, str], profile: "CandidateProfile", score: float, parts: list[str],
                  findings: _Findings) -> float:
    level, _sentence = degree
    name = DEGREE_NAMES[level]
    completed = [(degree_level_of(e.degree) or 0, e) for e in profile.education if not e.expected]
    expected = [(degree_level_of(e.degree) or 0, e) for e in profile.education if e.expected]
    done = [e for lvl, e in completed if lvl >= level]
    if done:
        edu = done[0]
        detail = f"{edu.degree}{' in ' + edu.field if edu.field else ''}, {edu.institution}"
        findings.matching.append(f"{name} (profile: {detail})")
        findings.evidence.append({"requirement": name, "fact_id": edu.id,
                                  "fact_text": detail + (f" ({edu.end})" if edu.end else "")})
        parts.append(f"{name} requirement met by {edu.id}")
        return score
    pending = [e for lvl, e in expected if lvl >= level]
    if pending:
        edu = pending[0]
        findings.missing.append(f"{name} (profile lists {edu.degree} as expected {edu.end or 'in the future'})")
        parts.append(f"{name} requested; yours is expected {edu.end or 'later'} (not penalized; see eligibility "
                     "flags for timing)")
        return score
    findings.missing.append(f"{name} (not in verified profile)")
    if level == DOCTORATE:
        findings.disqualifiers.append("Requires a PhD / doctorate")
    capped = min(score, 0.2 if level == DOCTORATE else 0.3)
    parts.append(f"requires a {name}, which your profile does not list -> capped at {capped:.2f}")
    return capped


# --------------------------------------------------------------------------- #
# domain_relevance
# --------------------------------------------------------------------------- #
_DOMAINS: dict[str, re.Pattern[str]] = {
    "AI": re.compile(
        r"(?<![A-Za-z0-9])(?:(?-i:AI)|artificial\s+intelligence|machine\s+learning|generative|gen\s?ai|llms?|"
        r"large\s+language\s+models?|nlp|natural\s+language|intelligent\s+automation|copilots?)(?![A-Za-z0-9])",
        re.I),
    "business": re.compile(
        r"\b(?:business|strategy|strategic|stakeholders?|operations|consulting|consultants?|process\s+improvement|"
        r"requirements|kpis?|go[\s\-]to[\s\-]market|revenue|transformation)\b", re.I),
    "analytics": re.compile(
        r"\b(?:analytics?|analysts?|analysis|analy[sz](?:e|es|ed|ing)|insights?|dashboards?|reporting|metrics|data|"
        r"sql|visuali[sz]ation|business\s+intelligence|statistics|statistical)\b", re.I),
    "product": re.compile(
        r"\b(?:products?|roadmaps?|user\s+research|customer\s+insights|experimentation|a/b\s+test(?:s|ing)?|"
        r"feature\s+adoption)\b", re.I),
}


def _domain(data: dict[str, Any], analysis: JobAnalysis | None) -> _Factor:
    title = str(data.get("title") or "")
    description = str(data.get("description_text") or "")
    strengths: dict[str, float] = {}
    notes: list[str] = []
    for name, rx in _DOMAINS.items():
        if rx.search(title):
            strengths[name] = 1.0
            notes.append(f"{name} 1.00 (title)")
            continue
        terms = {m.group(0).lower() for m in rx.finditer(description)}
        strengths[name] = round(min(0.9, 0.3 * len(terms)), 3)
        notes.append(f"{name} {strengths[name]:.2f} ({len(terms)} distinct term{'s' if len(terms) != 1 else ''})")
    top = sorted(strengths.values(), reverse=True)[:2]
    rules = _r(sum(top) / 2)
    explanation = f"Signals: {', '.join(notes)}; mean of the two strongest = {rules:.2f}"
    score = rules
    if analysis is not None:
        llm_values = sorted([analysis.ai_relevance, analysis.business_relevance, analysis.analytics_relevance,
                             analysis.product_relevance], reverse=True)[:2]
        llm = _r(sum(llm_values) / 2)
        score = _r(0.5 * rules + 0.5 * llm)
        explanation += (f"; local AI relevance (AI {analysis.ai_relevance:.2f}, business "
                        f"{analysis.business_relevance:.2f}, analytics {analysis.analytics_relevance:.2f}, product "
                        f"{analysis.product_relevance:.2f}) top-two mean {llm:.2f}, blended 50/50 => {score:.2f}")
        if analysis.fit_explanation.strip():
            explanation += f". Local AI opinion (unverified; may be wrong): {analysis.fit_explanation.strip()}"
    explanation += "."
    strongest = [n for n, v in strengths.items() if v >= 0.9]
    pro = f"Strong {'/'.join(strongest)} focus" if score >= 0.7 and strongest else None
    con = "Little AI / business / analytics / product focus" if score <= 0.35 else None
    return _Factor("domain_relevance", score, explanation, pro, con)


# --------------------------------------------------------------------------- #
# location
# --------------------------------------------------------------------------- #
_NON_REGION = re.compile(r"\b(?:remote|hybrid|on[\s\-]?site|in[\s\-]office|anywhere|work\s+from\s+home|wfh|"
                         r"flexible|fully|first|optional|or)\b|[^A-Za-z]+", re.I)


def _location(data: dict[str, Any], profile: "CandidateProfile", config: "AppConfig") -> _Factor:
    loc = str(data.get("location") or "").strip()
    arrangement = str(data.get("work_arrangement") or "unknown").strip().lower()
    wanted = dedupe([*config.search.locations, *profile.preferences.preferred_locations])
    remote_ok = any(w.strip().lower() == "remote" for w in wanted)
    places = [w for w in wanted if w.strip().lower() != "remote"]
    matched = [p for p in places if contains_phrase(loc, p)]
    is_remote = arrangement == "remote" or contains_phrase(loc, "remote")
    is_hybrid = not is_remote and (arrangement == "hybrid" or contains_phrase(loc, "hybrid"))
    region = _NON_REGION.sub(" ", loc).strip()
    pref = config.search.remote_preference
    where = f"'{loc}'" if loc else "no location"
    hit = f" matches your location(s) {short_list(matched, 4)}" if matched else " is not in your locations"

    if not loc and arrangement == "unknown":
        return _Factor("location", 0.5, "Location and work arrangement not listed; neutral 0.50.")
    if is_remote:
        region_ok = not region or bool(matched)
        if region_ok:
            score = 1.0 if (remote_ok or matched or pref != "any") else 0.7
            why = f"Remote role ({where})" + (" and 'Remote' is in your locations" if remote_ok else "")
        else:
            score = 0.5
            why = f"Remote role, but the listed region {where} is not in your locations"
    elif pref == "remote_only":
        score = 0.3 if is_hybrid else 0.15
        why = f"{'Hybrid' if is_hybrid else 'Not remote'} ({where}); you prefer remote only"
    elif pref == "remote_or_hybrid":
        if is_hybrid:
            score = 0.9 if matched else 0.3
        elif arrangement == "onsite":
            score = 0.4 if matched else 0.1
        else:
            score = 0.6 if matched else 0.2
        why = f"{arrangement if arrangement != 'unknown' else 'Arrangement unknown'}, location {where}{hit}; you prefer remote or hybrid"
    else:
        if not loc:
            score = 0.5
            why = f"Work arrangement '{arrangement}' but no location listed"
        else:
            score = 1.0 if matched else 0.2
            why = f"Location {where}{hit}"
    explanation = f"{why[0].upper() + why[1:]} => {score:.2f}."
    pro = (f"Location fits your preferences ({short_list(matched, 3) or 'remote'})" if score >= 0.9 else None)
    con = f"Location {where} does not fit your preferences" if score <= 0.3 else None
    return _Factor("location", _r(score), explanation, pro, con)


# --------------------------------------------------------------------------- #
# compensation
# --------------------------------------------------------------------------- #
def money(value: float, currency: str | None = "USD") -> str:
    cur = (currency or "USD").upper()
    return f"${value:,.0f}" if cur == "USD" else f"{value:,.0f} {cur}"


def salary_display(job: Mapping[str, Any]) -> str:
    """Human-readable salary for tables (verbatim text first; never invented)."""
    data = _job_dict(job)
    text = str(data.get("salary_text") or "").strip()
    if text:
        return text
    lo, hi = data.get("salary_min"), data.get("salary_max")
    if lo is None and hi is None:
        return ""
    cur = data.get("salary_currency") or "USD"
    period = str(data.get("salary_period") or "unknown")
    rng = money(lo, cur) if hi is None else money(hi, cur) if lo is None or lo == hi else \
        f"{money(lo, cur)}-{money(hi, cur)}"
    return rng + ("" if period == "unknown" else f"/{period}")


def _compensation(data: dict[str, Any], profile: "CandidateProfile", config: "AppConfig") -> _Factor:
    lo, hi = data.get("salary_min"), data.get("salary_max")
    values = [float(v) for v in (lo, hi) if v is not None]
    if not values:
        return _Factor("compensation", 0.5, "Salary not listed; neutral 0.50 (salaries are never estimated).")
    currency = str(data.get("salary_currency") or "").upper() or None
    wanted = (profile.preferences.salary_currency or "USD").upper()
    if currency and currency != wanted:
        return _Factor("compensation", 0.5,
                       f"Salary is listed in {currency}; not compared with your {wanted} target; neutral 0.50.")
    period = str(data.get("salary_period") or "unknown").lower()
    notes: list[str] = []
    if period in _PERIOD_MULTIPLIER:
        multiplier = _PERIOD_MULTIPLIER[period]
        if period != "year":
            notes.append(f"estimated annual pay ({_PERIOD_WORD[period]}; an estimate)")
    elif max(values) >= 20_000:
        multiplier = 1
        notes.append("pay period not stated; treated as annual because of the amount")
    else:
        return _Factor("compensation", 0.5,
                       f"Pay of {money(max(values), currency)} listed without a clear period; not compared; "
                       "neutral 0.50.")
    top, bottom = max(values) * multiplier, min(values) * multiplier
    target = float(config.scoring.salary_target_base)
    shown = money(bottom, currency) if top == bottom else f"{money(bottom, currency)}-{money(top, currency)}"
    if target <= 0:
        score, why = 1.0, f"{shown}; no salary target configured"
    elif bottom >= target:
        score, why = 1.0, f"{shown} meets your {money(target)} base target"
    elif top >= target:
        score, why = 0.85, f"{shown} reaches your {money(target)} base target at the top of the range"
    else:
        ratio = top / target
        score = _r(0.8 * max(0.0, (ratio - 0.5) / 0.5))
        why = f"{shown} tops out at {ratio:.0%} of your {money(target)} base target (scaled)"
    is_base = data.get("salary_is_base")
    if is_base is not None and not bool(is_base):
        score = _r(score * 0.7)
        notes.append("listing gives total compensation (e.g. OTE), not base - partial credit x0.7")
    elif is_base is None:
        notes.append("listing does not say whether this is base pay; treated as base")
    explanation = why + (f" ({'; '.join(notes)})" if notes else "") + f" => {score:.2f}."
    pro = "Listed pay reaches your base salary target" if score >= 0.85 else None
    con = f"Listed pay ({shown}) is below your {money(target)} base target" if score <= 0.4 and target > 0 else None
    return _Factor("compensation", score, explanation, pro, con)


# --------------------------------------------------------------------------- #
# employer_growth
# --------------------------------------------------------------------------- #
_GROWTH_SIGNALS: dict[str, re.Pattern[str]] = {
    label: re.compile(rx, re.I)
    for label, rx in {
        "mentorship": r"\bmentor(?:ship|ing|s)?\b",
        "training": r"\btraining\s+programs?\b|\bstructured\s+training\b|\bon[\s\-]the[\s\-]job\s+training\b|"
                    r"\blearning\s+(?:and|&)\s+development\b|\bprofessional\s+development\b|"
                    r"\b(?:learning|education)\s+(?:stipend|budget)\b|\btuition\s+(?:reimbursement|assistance)\b",
        "early-career program": r"\brotational\b|\brotation\s+program\b|\bearly[\s\-]career\s+program\b|"
                                r"\bnew\s+grad(?:uate)?\s+program\b|\bgraduate\s+program\b|"
                                r"\b(?:analyst|development|leadership\s+development)\s+program\b",
        "career growth": r"\bcareer\s+(?:growth|development|progression|path(?:ing)?)\b|\bgrowth\s+opportunit(?:y|ies)\b",
    }.items()
}


def _norm_employer(name: str) -> str:
    t = re.sub(r"[^a-z0-9 ]+", " ", name.lower())
    t = re.sub(r"\b(?:inc|llc|ltd|corp|corporation|co|company|plc|gmbh|lp|llp)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _employer_growth(data: dict[str, Any], config: "AppConfig") -> _Factor:
    employer = str(data.get("employer") or "")
    description = str(data.get("description_text") or "")
    norm = _norm_employer(employer)
    target = next((t for t in config.search.target_employers
                   if _norm_employer(t) and (_norm_employer(t) == norm or contains_phrase(norm, _norm_employer(t)))),
                  None)
    signals = [label for label, rx in _GROWTH_SIGNALS.items() if rx.search(description)]
    score = 0.5 + (0.3 if target else 0.0) + 0.15 * len(signals)
    score = _r(score)
    parts = []
    if target:
        parts.append(f"'{employer}' is one of your target employers (+0.30)")
    if signals:
        parts.append(f"growth signals: {', '.join(signals)} (+0.15 each)")
    if not parts:
        explanation = "No target-employer or early-career/mentorship/training signals found; neutral 0.50."
    else:
        explanation = "Base 0.50; " + "; ".join(parts) + f" => {score:.2f}."
    pro = None
    if target:
        pro = f"{employer} is one of your target employers"
    elif signals:
        pro = f"Growth signals in the listing: {', '.join(signals)}"
    return _Factor("employer_growth", score, explanation, pro if score >= 0.65 else None, None)


# --------------------------------------------------------------------------- #
# assembly
# --------------------------------------------------------------------------- #
_FLAG_LABELS = {
    "no_sponsorship": "No-sponsorship statement",
    "citizenship_required": "Citizenship requirement stated",
    "permanent_residency_required": "Permanent-residency requirement stated",
    "clearance_required": "Security-clearance requirement stated",
    "export_control": "Export-control condition stated",
    "graduation_timing": "Graduation / start-date timing stated",
    "sponsorship_available": "Sponsorship mentioned as available",
    "work_authorization_required": "Work-authorization requirement stated",
    "eligibility_statement": "Eligibility-related wording (sponsorship, citizenship, clearance or export)",
}


def _clip(text: str, limit: int = 180) -> str:
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _category(overall: float, flags: list[EligibilityFlag], short_description: bool,
              config: "AppConfig") -> MatchCategory:
    if elig.needs_review(flags) or short_description:
        return MatchCategory.NEEDS_REVIEW
    if overall >= config.scoring.strong_threshold:
        return MatchCategory.STRONG
    if overall >= config.scoring.possible_threshold:
        return MatchCategory.POSSIBLE
    return MatchCategory.WEAK


def _next_action(category: MatchCategory, shortlisted: bool, flags: list[EligibilityFlag],
                 findings: _Findings, short_description: bool) -> str:
    if elig.has_blocker(flags):
        return "Review eligibility statement before investing time"
    if findings.senior_skip:
        return findings.senior_skip
    if elig.needs_review(flags):
        return "Review eligibility statement before investing time"
    if short_description:
        return "Open the listing and read the full description - too little information to score reliably"
    if shortlisted or category == MatchCategory.STRONG:
        return "Prepare tailored documents"
    if category == MatchCategory.POSSIBLE:
        return "Read the full listing and decide whether to prepare documents"
    return "Low priority - skip unless the listing interests you"


def score_job(job: Mapping[str, Any], profile: "CandidateProfile", config: "AppConfig",
              analysis: JobAnalysis | None = None) -> ScoreResult:
    """Score one job row (``jobs`` table mapping or equivalent dict). Pure and deterministic."""
    data = _job_dict(job)
    index = build_profile_index(profile)
    findings = _Findings()

    factors = [
        _career_interest(data, profile, config, analysis),
        _skills(data, index, profile, analysis, findings),
        _experience(data, profile, analysis, findings),
        _domain(data, analysis),
        _location(data, profile, config),
        _compensation(data, profile, config),
        _employer_growth(data, config),
    ]
    weights = config.scoring.weights.normalized()
    overall = round(min(100.0, max(0.0, sum(weights[f.name] * f.score for f in factors))), 1)

    flags = elig.detect_eligibility_flags(
        data, profile, extra_texts=analysis.sponsorship_statements if analysis is not None else ())
    description_len = len(str(data.get("description_text") or "").strip())
    short_description = description_len < MIN_DESCRIPTION_CHARS
    category = _category(overall, flags, short_description, config)
    blocker = elig.has_blocker(flags)
    shortlisted = overall >= config.scoring.shortlist_threshold and not blocker

    for flag in flags:
        if flag.severity == elig.BLOCKER:
            findings.disqualifiers.insert(0, f"{_FLAG_LABELS.get(flag.code, flag.code)}: \"{_clip(flag.excerpt)}\"")

    reasons_to_apply = [f.pro for f in factors if f.pro]
    if any(f.code == "sponsorship_available" for f in flags):
        reasons_to_apply.append("The listing mentions visa sponsorship (info only - confirm details)")
    reasons_not = [f"{_FLAG_LABELS.get(f.code, f.code)} - review before investing time"
                   for f in flags if f.severity in {elig.BLOCKER, elig.REVIEW}]
    if short_description:
        reasons_not.append(f"Short description ({description_len} characters); the score rests on limited information")
    reasons_not += [f.con for f in factors if f.con]

    result = ScoreResult(
        job_id=int(data.get("id") or 0),
        overall_score=overall,
        category=category,
        shortlisted=shortlisted,
        factors=[FactorScore(name=f.name, weight=round(weights[f.name], 2), score=f.score, explanation=f.explanation)
                 for f in factors],
        matching_qualifications=dedupe(findings.matching),
        missing_qualifications=dedupe(findings.missing),
        potential_disqualifiers=dedupe(findings.disqualifiers),
        eligibility_flags=flags,
        evidence=findings.evidence,
        reasons_to_apply=dedupe(reasons_to_apply),
        reasons_not_to_apply=dedupe(reasons_not),
        recommended_next_action=_next_action(category, shortlisted, flags, findings, short_description),
        scoring_version=SCORING_VERSION + (LLM_VERSION_SUFFIX if analysis is not None else ""),
        used_llm=analysis is not None,
        llm_model=(analysis.model or None) if analysis is not None else None,
    )
    return result
