"""Skill taxonomy and profile-evidence lookup.

Every skill has a canonical name, a category and a word-boundary regex built
from its aliases. Extraction from job text is deterministic; whether the
*candidate* has a skill is decided only from the verified profile: a skill
listed in ``profile.skills`` (directly, or via a strict "is a kind of"
implication such as MySQL -> SQL) and/or facts whose ``skills`` / ``keywords``
/ text mention it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

from job_agent.scoring.text import LEFT, RIGHT, bounded, phrase_regex

if TYPE_CHECKING:  # pragma: no cover
    from job_agent.profile import CandidateProfile

LANGUAGE, TOOL, PLATFORM, METHOD, DOMAIN, SOFT = "language", "tool", "platform", "method", "domain", "soft"
CATEGORIES = (LANGUAGE, TOOL, PLATFORM, METHOD, DOMAIN, SOFT)


@dataclass(frozen=True)
class SkillDef:
    name: str
    category: str
    pattern: re.Pattern[str]
    implies: tuple[str, ...] = ()
    adhoc: bool = False  # built from a profile skill that is not in the static taxonomy


@dataclass(frozen=True)
class SkillMention:
    name: str
    start: int
    end: int
    text: str


def _skill(name: str, category: str, *aliases: str, implies: Iterable[str] = (), raw: str | None = None) -> SkillDef:
    parts = [bounded("|".join(aliases))] if aliases else []
    if raw:
        parts.append(raw)
    return SkillDef(name, category, re.compile("|".join(parts), re.I), tuple(implies))


# "R" the language: an uppercase standalone token ("R,", " R ", "R/Python",
# "Python/R", "R programming") -- never inside words ("Rust", "React", "HR"),
# never "R&D" / "R & D", "R-squared", "R2" or the "(R)" trademark sign.
_R_LANGUAGE = (
    r"(?:(?<![A-Za-z0-9_&'.\-(])|(?<=\()(?!R\)))(?-i:R)(?![A-Za-z0-9_&'+#\-])(?!\.[A-Za-z])"
    r"(?!\s*&\s*D(?![A-Za-z0-9]))"
    rf"|{LEFT}(?:r\s*studio|tidyverse|ggplot2?|dplyr){RIGHT}"
)

#: The static taxonomy. Order matters only for display; extraction keeps the
#: longest overlapping mention ("SAS Enterprise Guide" wins over "SAS").
TAXONOMY: tuple[SkillDef, ...] = (
    # -- skills in the candidate's profile ---------------------------------- #
    _skill("Python", LANGUAGE, r"python(?:\s*3)?"),
    _skill("SQL", LANGUAGE, r"sql", r"structured\s+query\s+language"),
    _skill("MySQL", TOOL, r"mysql", implies=("SQL",)),
    _skill("Excel", TOOL, r"(?:microsoft\s+|ms\s+)?(?-i:Excel|EXCEL)(?!\s+(?:in|at|as|with|under|on)\b)",
           r"pivot\s+tables?", r"v-?lookups?", r"xlookups?"),
    _skill("Power BI", TOOL, r"(?:microsoft\s+)?power[\s\-]?bi", implies=("Business intelligence",)),
    _skill("Tableau", TOOL, r"tableau", implies=("Business intelligence",)),
    _skill("R", LANGUAGE, raw=_R_LANGUAGE),
    _skill("Jira", TOOL, r"jira"),
    _skill("Confluence", TOOL, r"(?-i:Confluence)"),
    _skill("SAS", TOOL, r"(?-i:SAS)"),
    _skill("SAS Enterprise Guide", TOOL, r"sas\s+enterprise\s+guide", r"sas\s+eg", implies=("SAS",)),
    _skill("IBM Cognos", TOOL, r"(?:ibm\s+)?cognos(?:\s+analytics)?", implies=("Business intelligence",)),
    _skill("DB2", TOOL, r"(?:ibm\s+)?db2"),
    _skill("Flask", TOOL, r"flask", implies=("Python",)),
    _skill("MongoDB", TOOL, r"mongo\s*db", r"mongo"),
    _skill("AI-assisted development", METHOD,
           r"ai[\s\-]+assisted\s+(?:software\s+)?(?:development|coding|programming|engineering)",
           r"ai\s+(?:coding|programming|pair[\s\-]programming)\s+(?:assistants?|tools?)",
           r"ai[\s\-]+powered\s+(?:coding|development)\s+tools?"),
    _skill("Data analytics", DOMAIN, r"data\s+analytics", r"data\s+analysis",
           r"analy[sz](?:e|ing)\s+(?:large\s+|complex\s+)?data(?:\s*sets?)?"),
    _skill("Business reporting", DOMAIN, r"business\s+reporting", r"management\s+reporting", r"kpi\s+reporting",
           r"operational\s+reporting", r"reporting\s+(?:and|&)\s+dashboards?", r"dashboards?\s+(?:and|&)\s+reporting"),
    _skill("Business intelligence", DOMAIN, r"business\s+intelligence"),
    _skill("Data visualization", DOMAIN, r"data\s+visuali[sz]ations?", r"visuali[sz]ation\s+tools?",
           r"dashboards?", r"dashboarding"),
    _skill("Statistics", METHOD, r"statistics", r"statistical(?:\s+(?:analysis|methods|modell?ing|techniques))?",
           r"regression(?:\s+analysis|\s+models?|\s+modell?ing)?", r"hypothesis\s+testing"),
    # -- common requirements the candidate may lack -------------------------- #
    _skill("Java", LANGUAGE, r"java(?!\s+script)"),
    _skill("JavaScript", LANGUAGE, r"javascript", r"java\s+script", r"(?-i:JS)", r"ecmascript", r"node\.?js"),
    _skill("TypeScript", LANGUAGE, r"typescript", implies=("JavaScript",)),
    # case-sensitive so the verb "react to ..." is not a skill
    _skill("React", TOOL, r"(?-i:React)(?:\s*(?:\.js|JS))?(?!\s+(?:to|quickly|swiftly|fast)\b)", r"react\.?js",
           r"react\s+native", implies=("JavaScript",)),
    _skill("C++", LANGUAGE, r"c\+\+"),
    _skill("C#", LANGUAGE, r"c\#", r"c\s+sharp"),
    _skill("Scala", LANGUAGE, r"scala"),
    _skill("Rust", LANGUAGE, r"rust"),
    _skill("Ruby", LANGUAGE, r"ruby(?:\s+on\s+rails)?"),
    _skill("PHP", LANGUAGE, r"php"),
    _skill("HTML/CSS", LANGUAGE, r"html5?", r"css3?"),
    _skill("VBA", LANGUAGE, r"vba", r"visual\s+basic"),
    _skill("SPSS", TOOL, r"spss"),
    _skill("Stata", TOOL, r"stata"),
    _skill("MATLAB", TOOL, r"matlab"),
    _skill("pandas", TOOL, r"pandas", r"numpy", implies=("Python",)),
    _skill("SQL Server", TOOL, r"(?:microsoft\s+|ms\s+)?sql\s+server", r"mssql", implies=("SQL",)),
    _skill("PostgreSQL", TOOL, r"postgres(?:ql)?", implies=("SQL",)),
    _skill("AWS", PLATFORM, r"aws", r"amazon\s+web\s+services", r"(?:amazon\s+)?redshift"),
    _skill("Azure", PLATFORM, r"(?:microsoft\s+)?azure"),
    _skill("GCP", PLATFORM, r"gcp", r"google\s+cloud(?:\s+platform)?", r"bigquery"),
    _skill("Spark", PLATFORM, r"(?-i:Spark)", r"apache\s+spark", r"pyspark"),
    _skill("Hadoop", PLATFORM, r"hadoop", r"(?-i:Hive)"),
    _skill("Snowflake", PLATFORM, r"snowflake"),
    _skill("Databricks", PLATFORM, r"databricks"),
    _skill("dbt", TOOL, r"dbt"),
    _skill("Looker", TOOL, r"looker(?:\s+studio)?", implies=("Business intelligence",)),
    _skill("Salesforce", PLATFORM, r"salesforce", r"sfdc"),
    _skill("SAP", PLATFORM, r"(?-i:SAP)"),
    _skill("Kubernetes", PLATFORM, r"kubernetes", r"k8s"),
    _skill("Docker", TOOL, r"docker"),
    _skill("Airflow", TOOL, r"(?:apache\s+)?airflow"),
    _skill("Kafka", PLATFORM, r"(?:apache\s+)?kafka"),
    _skill("Git", TOOL, r"git", r"github", r"gitlab", r"version\s+control"),
    _skill("Linux", PLATFORM, r"linux", r"unix"),
    _skill("REST APIs", METHOD, r"rest(?:ful)?\s+apis?"),
    _skill("Machine learning", METHOD, r"machine[\s\-]+learning", r"(?-i:ML)"),
    _skill("Deep learning", METHOD, r"deep[\s\-]+learning", r"neural\s+networks?", implies=("Machine learning",)),
    _skill("NLP", METHOD, r"nlp", r"natural\s+language\s+processing"),
    _skill("Generative AI / LLMs", METHOD, r"generative\s+ai", r"gen\s*ai", r"llms?", r"large\s+language\s+models?",
           r"prompt\s+engineering", r"retrieval[\s\-]augmented\s+generation", r"chatgpt", r"gpt(?:-?\d+)?"),
    _skill("PyTorch", TOOL, r"pytorch", implies=("Deep learning",)),
    _skill("TensorFlow", TOOL, r"tensorflow", implies=("Deep learning",)),
    _skill("scikit-learn", TOOL, r"scikit[\s\-]learn", r"sklearn", implies=("Machine learning",)),
    _skill("A/B testing", METHOD, r"a\s*/\s*b\s+test(?:s|ing)?", r"ab\s+test(?:s|ing)", r"split\s+test(?:s|ing)?",
           r"controlled\s+experiments?", r"experimentation"),
    _skill("ETL / data pipelines", METHOD, r"etl", r"elt", r"data\s+pipelines?", r"data\s+integration"),
    _skill("Data modeling", METHOD, r"data\s+modell?ing", r"dimensional\s+modell?ing", r"star\s+schemas?"),
    _skill("Data warehousing", METHOD, r"data\s+warehous(?:e|es|ing)"),
    _skill("Data structures & algorithms", METHOD, r"data\s+structures(?:\s+(?:and|&)\s+algorithms)?"),
    _skill("Financial modeling", METHOD, r"financial\s+modell?ing"),
    _skill("Requirements gathering", METHOD,
           r"requirements\s+(?:gathering|elicitation|analysis|definition|documentation)",
           r"gather(?:ing)?\s+(?:business\s+)?requirements", r"business\s+requirements", r"user\s+stories",
           r"process\s+mapping", r"bpmn"),
    _skill("Product management", METHOD, r"product\s+management", r"product\s+roadmaps?", r"product\s+lifecycle",
           r"product\s+strategy", r"product\s+requirements", r"prds?"),
    _skill("Project management", METHOD, r"project\s+management", r"pmp"),
    _skill("Agile/Scrum", METHOD, r"agile", r"scrum", r"kanban", r"sprint\s+planning"),
    _skill("Stakeholder management", SOFT, r"stakeholder\s+(?:management|engagement|communication|alignment)",
           r"manag(?:e|ing)\s+stakeholders?", r"cross[\s\-]functional\s+stakeholders",
           r"partner(?:ing)?\s+with\s+stakeholders", r"work(?:ing)?\s+with\s+(?:business\s+)?stakeholders"),
    _skill("Figma", TOOL, r"figma"),
    _skill("Alteryx", TOOL, r"alteryx"),
    _skill("Google Analytics", TOOL, r"google\s+analytics", r"ga4"),
    _skill("Mixpanel", TOOL, r"mixpanel"),
    _skill("Amplitude", TOOL, r"(?-i:Amplitude)"),
    _skill("PowerPoint", TOOL, r"(?:microsoft\s+|ms\s+)?powerpoint", r"power\s+point"),
    _skill("Visio", TOOL, r"(?:microsoft\s+)?visio"),
)

_BY_NAME: dict[str, SkillDef] = {s.name.lower(): s for s in TAXONOMY}


def skill_by_name(name: str, taxonomy: Iterable[SkillDef] | None = None) -> SkillDef | None:
    if taxonomy is None:
        return _BY_NAME.get(name.strip().lower())
    key = name.strip().lower()
    return next((s for s in taxonomy if s.name.lower() == key), None)


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
def find_skill_mentions(text: str | None, taxonomy: Iterable[SkillDef] = TAXONOMY) -> list[SkillMention]:
    """All skill mentions in ``text``; a mention inside a longer mention of another skill is dropped."""
    if not text:
        return []
    mentions: list[SkillMention] = []
    for skill in taxonomy:
        for m in skill.pattern.finditer(text):
            if m.end() > m.start():
                mentions.append(SkillMention(skill.name, m.start(), m.end(), m.group(0)))
    kept = [
        a for a in mentions
        if not any(b.name != a.name and b.start <= a.start and a.end <= b.end and (b.end - b.start) > (a.end - a.start)
                   for b in mentions)
    ]
    kept.sort(key=lambda x: (x.start, -x.end, x.name))
    return kept


def extract_skills(text: str | None, taxonomy: Iterable[SkillDef] = TAXONOMY) -> list[str]:
    """Canonical skill names in order of first mention (unique)."""
    seen: dict[str, None] = {}
    for mention in find_skill_mentions(text, taxonomy):
        seen.setdefault(mention.name, None)
    return list(seen)


def implied_closure(names: Iterable[str], taxonomy: Iterable[SkillDef] = TAXONOMY) -> list[str]:
    """``names`` plus everything they imply (transitively), order-preserving."""
    by_name = {s.name: s for s in taxonomy}
    out: dict[str, None] = {}
    stack = list(names)
    while stack:
        name = stack.pop(0)
        if name in out:
            continue
        out[name] = None
        skill = by_name.get(name)
        if skill:
            stack.extend(skill.implies)
    return list(out)


# --------------------------------------------------------------------------- #
# Profile index
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SkillEvidence:
    """How the verified profile supports one canonical skill."""

    skill: str
    profile_labels: tuple[str, ...] = ()  # profile.skills entries naming it directly
    via_labels: tuple[str, ...] = ()  # profile.skills entries implying it (e.g. MySQL -> SQL)
    fact_ids: tuple[str, ...] = ()

    @property
    def supported(self) -> bool:
        return bool(self.profile_labels or self.via_labels or self.fact_ids)

    def describe(self) -> str:
        parts = []
        if self.profile_labels:
            parts.append("profile skill")
        elif self.via_labels:
            parts.append(f"via profile skill {', '.join(self.via_labels)}")
        if self.fact_ids:
            parts.append(f"evidence: {', '.join(self.fact_ids)}")
        return "; ".join(parts) if parts else "no evidence in verified profile"


@dataclass(frozen=True)
class ProfileSkillIndex:
    taxonomy: tuple[SkillDef, ...]
    #: canonical skill -> profile.skills labels that name it directly
    declared: dict[str, tuple[str, ...]]
    #: canonical skill -> profile.skills labels that imply it
    implied: dict[str, tuple[str, ...]]
    #: canonical skill -> ids of verified facts that mention it
    facts: dict[str, tuple[str, ...]]
    #: profile.skills labels -> canonical skills they name directly
    label_skills: dict[str, tuple[str, ...]]
    fact_texts: dict[str, str] = field(default_factory=dict)
    #: lowercase fact skill/keyword labels -> fact ids (for free-text requirement lookups)
    fact_labels: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def evidence(self, skill: str) -> SkillEvidence:
        return SkillEvidence(skill, self.declared.get(skill, ()), self.implied.get(skill, ()),
                             self.facts.get(skill, ()))

    def has(self, skill: str) -> bool:
        return self.evidence(skill).supported

    def extract(self, text: str | None) -> list[str]:
        return extract_skills(text, self.taxonomy)

    def category(self, skill: str) -> str:
        found = skill_by_name(skill, self.taxonomy)
        return found.category if found else TOOL

    def free_text_evidence(self, label: str) -> SkillEvidence:
        """Evidence for a requirement label that maps to no taxonomy skill (exact/phrase match only)."""
        key = label.strip().lower()
        labels = tuple(lbl for lbl in self.label_skills if lbl.lower() == key)
        fact_ids = list(self.fact_labels.get(key, ()))
        pattern = phrase_regex(label)
        for fid, text in self.fact_texts.items():
            if fid not in fact_ids and pattern.search(text):
                fact_ids.append(fid)
        ordered = tuple(fid for fid in self.fact_texts if fid in fact_ids)
        return SkillEvidence(label, labels, (), ordered)


def _adhoc_skill(label: str) -> SkillDef:
    words = [re.escape(w) for w in re.split(r"[\s\-]+", label.strip()) if w]
    body = r"[\s\-]+".join(words)
    flags = 0 if len(label.strip()) <= 2 else re.I  # short labels ("Go") are matched case-sensitively
    return SkillDef(label.strip(), TOOL, re.compile(bounded(body), flags), (), adhoc=True)


_INDEX_CACHE: dict[str, ProfileSkillIndex] = {}


def build_profile_index(profile: "CandidateProfile") -> ProfileSkillIndex:
    """Index the verified profile by canonical skill. Cached by profile hash."""
    key = profile.profile_hash()
    cached = _INDEX_CACHE.get(key)
    if cached is not None:
        return cached

    taxonomy = list(TAXONOMY)
    label_skills: dict[str, tuple[str, ...]] = {}
    for label in profile.skills:
        names = extract_skills(label, taxonomy)
        if not names:
            adhoc = _adhoc_skill(label)
            taxonomy.append(adhoc)
            names = [adhoc.name]
        label_skills[label] = tuple(names)
    tax = tuple(taxonomy)

    declared: dict[str, list[str]] = {}
    implied: dict[str, list[str]] = {}
    for label, names in label_skills.items():
        for name in names:
            declared.setdefault(name, []).append(label)
        for name in implied_closure(names, tax):
            if name not in names:
                implied.setdefault(name, []).append(label)

    facts: dict[str, list[str]] = {}
    fact_texts: dict[str, str] = {}
    fact_labels: dict[str, list[str]] = {}
    for fact in profile.all_facts():
        fact_texts[fact.id] = fact.text
        names: list[str] = []
        for tag in [*fact.skills, *fact.keywords]:
            fact_labels.setdefault(tag.strip().lower(), []).append(fact.id)
            names.extend(extract_skills(tag, tax))
            names.extend(label_skills.get(next((lbl for lbl in label_skills if lbl.lower() == tag.lower()), ""), ()))
        names.extend(extract_skills(fact.text, tax))
        for name in implied_closure(names, tax):
            bucket = facts.setdefault(name, [])
            if fact.id not in bucket:
                bucket.append(fact.id)

    index = ProfileSkillIndex(
        taxonomy=tax,
        declared={k: tuple(v) for k, v in declared.items()},
        implied={k: tuple(v) for k, v in implied.items() if k not in declared},
        facts={k: tuple(v) for k, v in facts.items()},
        label_skills=label_skills,
        fact_texts=fact_texts,
        fact_labels={k: tuple(dict.fromkeys(v)) for k, v in fact_labels.items()},
    )
    if len(_INDEX_CACHE) > 16:
        _INDEX_CACHE.clear()
    _INDEX_CACHE[key] = index
    return index


def skill_evidence(profile: "CandidateProfile") -> dict[str, list[str]]:
    """Each ``profile.skills`` entry -> ids of verified facts that support it (may be empty)."""
    index = build_profile_index(profile)
    out: dict[str, list[str]] = {}
    for label, names in index.label_skills.items():
        ids: list[str] = []
        for name in names:
            for fid in index.facts.get(name, ()):
                if fid not in ids:
                    ids.append(fid)
        out[label] = [fid for fid in index.fact_texts if fid in ids]
    return out
