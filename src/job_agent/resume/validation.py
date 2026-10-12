"""Deterministic claim validation (the truthfulness guarantee).

``validate_claim`` checks one generated statement against the verified fact(s)
it is supposed to restate. Its rules are heuristics that err on the side of
rejecting: a rejected rewording simply falls back to the verbatim fact text.

Rules (each violation is an ``Issue`` with severity ``error``):

* every number / percentage / amount must appear in the source fact(s) with the
  same unit (%, $, k/m/b, x) -- a bare number must also count the same thing
  ("9 programs" may not become "9 categories" or borrow "25" from "25%");
  spelled-out quantities ("doubled", "dozens", "fivefold") must also appear in
  the source;
* a number keeps the source's qualifier class: none / approximate
  ("approximately", "about", "~") / lower bound ("over", "more than", "at
  least", "12+") / upper bound ("up to", "less than"); nothing may be added,
  removed or swapped;
* no lexicon tool/skill/technology (matched case-insensitively) unless it is in
  the source fact (text or its ``skills``) -- or, with ``strict_skills=False``,
  in ``profile.skills``;
* no leadership / ownership verbs (led, managed, owned, spearheaded, ...) and
  no build/ownership verbs (built, developed, designed, created, launched,
  deployed, implemented, ...) unless the source uses them -- "Supported" may
  not become "Developed";
* no inflation wording the source lacks: "single-handedly", "solely",
  "responsible for", "end-to-end", "company-wide" / "statewide" scope words,
  intensifiers such as "significantly";
* no content word that is not in the fact text, the fact's ``skills`` /
  ``keywords``, ``profile.skills``, the caller's verified ``context`` or a small
  connective allowlist ("using", "including", "also", ...): reordering and
  trimming pass, additions do not;
* no organization / proper names that are not in the source (including other
  profile employers and the job's employer);
* no certifications / degrees that are not in the source or the profile; no
  "completed"/"hold"/"graduated" wording next to an expected degree; dates next
  to degree/graduation wording must be the profile's verbatim education dates;
* no work-authorization / visa / sponsorship / citizenship wording (broadly
  matched: "eligible to work", "employer support", "without restriction",
  OPT/CPT, H-1B, green card, ...), links, e-mail addresses or placeholders;
* at least ~50% of the content words must come from the source; length bounds.

``validate_resume`` / ``validate_cover_letter`` check whole documents:
traceability of every bullet/claim to fact ids (each re-validated), header
fields equal to ``profile.personal``, verbatim education dates, skills subset of
the profile, the cover letter's fixed template paragraphs, and placeholder text
(``<...>``) in used profile fields (=> needs review). The job title and
employer are never trusted as a source for any claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from job_agent.profile import CandidateProfile, Education, Fact
from job_agent.resume import lexicon
from job_agent.resume.models import Claim, CoverLetterContent, Issue, ResumeContent
from job_agent.resume.text import (
    STOPWORDS,
    content_set,
    content_words,
    mentions,
    normalize_space,
    split_sentences,
    stem,
    words,
)

MIN_OVERLAP = 0.5
MIN_WORDS = 4
MAX_CHARS = 400

# --------------------------------------------------------------------------- #
# Patterns
# --------------------------------------------------------------------------- #
_NUM_RE = re.compile(
    r"(?<![A-Za-z0-9.])(?P<cur>\$|USD\s?|EUR\s?|£|€)?(?P<val>\d+(?:[.,]\d+)*)"
    r"(?P<suf>\s?(?:%|percent\b|per\s+cent\b|[kKmMbB]\b|x\b|\+))?"
)
_NUMBER_WORDS = re.compile(
    r"(?<![A-Za-z])(zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
    r"fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
    r"hundreds?|thousands?|millions?|billions?|dozens?|double[ds]?|doubling|triple[ds]?|tripling|quadrupled|"
    r"half|halved|twice|thrice|multiple|several|numerous|countless)(?![A-Za-z])",
    re.IGNORECASE,
)
_FOLD = re.compile(
    r"(?<![A-Za-z])((?:two|three|four|five|six|seven|eight|nine|ten|twenty|fifty|hundred|thousand|many|multi|"
    r"several|\d+(?:\.\d+)?)[\s-]?fold)(?![A-Za-z])",
    re.IGNORECASE,
)
#: Qualifier classes for numbers. A generated number must keep the source's class.
_QUALIFIERS: tuple[tuple[str, str], ...] = (
    ("approximate", r"approximately|approx\.?|about|around|roughly|nearly|almost|circa|ca\.|an\s+estimated|"
                    r"estimated|some|~"),
    ("lower bound", r"over|more\s+than|at\s+least|upwards\s+of|in\s+excess\s+of|exceeding|above|greater\s+than|"
                    r"no\s+less\s+than|no\s+fewer\s+than|north\s+of|beyond|>=?"),
    ("upper bound", r"up\s+to|less\s+than|fewer\s+than|under|below|at\s+most|no\s+more\s+than|as\s+many\s+as|"
                    r"as\s+much\s+as|<=?"),
)
_QUAL_TAIL = tuple((name, re.compile(rf"(?:(?<![A-Za-z])|^)(?:{alt})\s*$", re.IGNORECASE))
                   for name, alt in _QUALIFIERS)

_LEADERSHIP = re.compile(
    r"(?<![A-Za-z])(led|lead|leads|leading|managed|manage|manages|managing|owned|own|owns|owning|ownership|"
    r"spearheaded|spearhead|spearheads|spearheading|directed|directing|headed|supervised|supervise|supervises|"
    r"supervising|supervisor|architected|architecting|founded|co-founded|cofounded|founder|co-founder|oversaw|"
    r"oversee|oversees|overseeing|orchestrated|championed|pioneered|mentored|chaired|steered|captained)"
    r"(?![A-Za-z])",
    re.IGNORECASE,
)
#: Build / ownership verbs: may not replace a supporting role ("Supported" -> "Developed").
_BUILD_VERBS = re.compile(
    r"(?<![A-Za-z])(build|builds|building|built|rebuilt|develop|develops|developed|developing|design|designs|"
    r"designed|designing|create|creates|created|creating|launch|launches|launched|launching|deploy|deploys|"
    r"deployed|deploying|implement|implements|implemented|implementing|architect|architects|architected|"
    r"architecting|lead|leads|led|leading|own|owns|owned|owning|drive|drives|drove|driven|driving|spearhead|"
    r"spearheads|spearheaded|spearheading|engineered|engineering|establish|established|establishing|deliver|"
    r"delivers|delivered|delivering|authored|invented|initiated|pioneered|produced|shipped|"
    r"rolled\s+out|rolling\s+out)(?![A-Za-z])",
    re.IGNORECASE,
)
_SUPPORT_VERBS = re.compile(
    r"(?<![A-Za-z-])(supported|supporting|supports|support|worked\s+on|working\s+on|assisted|assisting|assist|"
    r"helped|helping|help|contributed|contributing|contribute|participated|participating|aided|involved\s+in)"
    r"(?![A-Za-z])",
    re.IGNORECASE,
)
_IRREGULAR = {"built": "build", "rebuilt": "build", "led": "lead", "drove": "driv", "driven": "driv",
              "owned": "own", "owns": "own", "owning": "own", "shipped": "ship", "shipping": "ship"}
_INFLATION = re.compile(
    r"(?<![A-Za-z])(single[- ]?handed(?:ly)?|sole|solely|singularly|by\s+myself|on\s+my\s+own|alone|"
    r"responsible\s+for|responsibility\s+for|accountable\s+for|in\s+charge\s+of|end[- ]to[- ]end|from\s+scratch|"
    r"(?:company|organi[sz]ation|enterprise|firm|agency|department|division|business|corporate|group|team|campus|"
    r"university|county|city|region|state|nation|country|world|industry|program|portfolio|system)[- ]?wide|"
    r"statewide|nationwide|worldwide|globally|"
    r"across\s+the\s+(?:entire\s+|whole\s+)?(?:company|organi[sz]ation|enterprise|firm|agency|business|state|"
    r"country|department)|entire\s+(?:company|organi[sz]ation|enterprise|firm|agency|business|department)|"
    r"top\s+performer|best\s+performer|award[- ]winning|record[- ]breaking|first[- ]ever|"
    r"significant(?:ly)?|substantial(?:ly)?|dramatic(?:ally)?|massive(?:ly)?|drastic(?:ally)?|"
    r"exponential(?:ly)?|transformative|revolutionary|mission[- ]critical|high[- ]impact)(?![A-Za-z])",
    re.IGNORECASE,
)
_CREDENTIAL = re.compile(
    r"(?<![A-Za-z])(certified|certification|certifications|certificate|certificates|licensed|licensure|"
    r"accredited|PMP|CPA|CFA|CISSP|CISA|CSM|CAPM|CBAP|ITIL|scrum\s+master|green\s+belt|black\s+belt|MBA|"
    r"Ph\.?\s?D\.?|doctorate|doctoral|master'?s|masters|master\s+of|bachelor'?s|bachelors|bachelor\s+of|"
    r"degree|diploma|B\.S\.|M\.S\.|B\.A\.|M\.A\.|BSc|MSc|B\.Tech|M\.Tech|BTech|MTech)(?![A-Za-z])",
    re.IGNORECASE,
)
_DEGREE_WORDS = re.compile(
    r"(?<![A-Za-z])(master'?s|masters|master\s+of|bachelor'?s|bachelors|bachelor\s+of|associate'?s\s+degree|"
    r"degree|diploma|doctorate|ph\.?\s?d\.?|mba|b\.\s?tech|m\.\s?tech|btech|mtech|bsc|msc|b\.s\.|m\.s\.|b\.a\.|"
    r"m\.a\.)(?![A-Za-z])"
    r"|(?<![A-Za-z])(?-i:MS|BS|BA|MA)(?![A-Za-z])(?!\s+(?:Excel|Access|Project|SQL|Office|Teams|Word|Visio|"
    r"PowerPoint|Outlook|Azure|Dynamics))",
    re.IGNORECASE,
)
_GRADUATION = re.compile(r"(?<![A-Za-z])(graduat\w*|alumn(?:us|a|i|ae)|class\s+of|commencement|convocation)"
                         r"(?![A-Za-z])", re.IGNORECASE)
#: Completion wording that would claim an expected degree is already held.
_COMPLETION = re.compile(
    r"(?<![A-Za-z])(hold|holds|holding|held|earned|obtained|received|completed|finished|graduated|"
    r"have\s+an?|has\s+an?|had\s+an?|possess|possesses|awarded|conferred|attained|achieved|"
    r"am\s+a\s+graduate|a\s+graduate\s+of|alumn(?:us|a|i|ae))(?![A-Za-z])",
    re.IGNORECASE,
)
_DATE_PHRASE = re.compile(
    r"(?<![A-Za-z0-9])((?:January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|Spring|Summer|Fall|Autumn|Winter)\.?\s+(?:of\s+)?"
    r"(?:19|20)\d{2}|(?:19|20)\d{2})(?![A-Za-z0-9])"
)
_WORK_AUTH_CI = re.compile(
    r"(?<![A-Za-z])(visas?|sponsorship|sponsorships|"
    r"sponsor(?:ed|ing|s)?\s+(?:a\s+|my\s+|your\s+|the\s+|any\s+)?(?:visa|work|employment|H-?1-?B|candidate|"
    r"employee|worker)s?|(?:need|needs|needed|require|requires|required|seek|seeks)\s+(?:a\s+|any\s+|no\s+)?"
    r"(?:visa\s+)?sponsors?|"
    r"work\s+(?:authori[sz]ation|permit|visa|eligibility|rights|status)|right\s+to\s+work|"
    r"(?:eligible|eligibility|authori[sz]ed|authori[sz]ation|permitted|allowed|entitled|able|cleared)\s+"
    r"(?:to|for)\s+(?:legally\s+)?(?:work|employment|be\s+employed|accept\s+employment|start\s+work)|"
    r"employment\s+(?:authori[sz]ation|eligibility|status)|"
    r"employer\s+(?:support|sponsorship|assistance)|"
    r"without\s+(?:any\s+)?(?:restrictions?|limitations?|sponsorship|employer\s+support)|"
    r"legal(?:ly)?\s+(?:able|eligible|authori[sz]ed|permitted|allowed|entitled)|"
    r"F-1|J-1|H-?1-?B|I-20|STEM\s+OPT|STEM\s+extension|green\s+card|permanent\s+residen(?:t|ts|cy|ce)|"
    r"(?:U\.?S\.?|American)\s+(?:citizen(?:ship)?|person|national)|citizen|citizens|citizenship|nationality|"
    r"immigration|immigrant|non-?immigrant|security\s+clearance|export\s+control(?:led|s)?|ITAR|deemed\s+export)"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_WORK_AUTH_CS = re.compile(r"(?<![A-Za-z])(OPT|CPT|EAD)(?![A-Za-z])")
_LINK = re.compile(r"(https?://|www\.|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"<[^<>]+>")
_BRACKET_PLACEHOLDER = re.compile(r"\[[^\[\]]{2,60}\]")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9&+#'’.-]*")

#: Connective words a rewording may add without adding a claim (kept deliberately small).
CONNECTIVES: frozenset[str] = frozenset(content_set(
    "using utilizing utilising leveraging including also along together respectively based"))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _issue(code: str, message: str, severity: str = "error", location: str | None = None) -> Issue:
    return Issue(code=code, severity=severity, message=message, location=location)  # type: ignore[arg-type]


def _numbers(text: str) -> list[tuple[str, str, str]]:
    """``(currency, value, suffix)`` for every number in ``text`` (normalized)."""
    return [(n.cur, n.val, n.suf) for n in _number_spans(text)]


@dataclass(frozen=True)
class _Num:
    cur: str
    val: str
    suf: str  # %, k, m, b, x, + or ""
    qual: str  # qualifier class ("" = none)
    next_word: str  # stem of the first content word right after the number ("" if none)
    near: frozenset[str]  # stems of content words around the number (source side)


def _clause_words(text: str, *, reverse: bool = False, limit: int = 4) -> list[str]:
    """Up to ``limit`` content-word stems before/after a position, stopping at clause punctuation."""
    parts = re.split(r"[,;:!?()]|\.\s", text)
    chunk = parts[-1] if reverse else parts[0]
    found = [stem(_bare(w)) for w in words(chunk) if _bare(w) not in STOPWORDS and len(_bare(w)) >= 3]
    return found[-limit:] if reverse else found[:limit]


def _number_spans(text: str) -> list[_Num]:
    out = []
    for m in _NUM_RE.finditer(text or ""):
        cur = "$" if (m.group("cur") or "").strip() in {"$", "USD"} else (m.group("cur") or "").strip()
        val = m.group("val").replace(",", "")
        if "." in val:
            val = val.rstrip("0").rstrip(".") or "0"
        suf = (m.group("suf") or "").strip().lower()
        if suf.startswith("per"):
            suf = "%"
        before = text[max(0, m.start() - 40): m.start()]
        qual = next((name for name, rx in _QUAL_TAIL if rx.search(before)), "")
        after = text[m.end(): m.end() + 80]
        following = _clause_words(after, limit=4)
        preceding = _clause_words(before, reverse=True, limit=4)
        next_word = following[0] if following and re.match(r"^\s*[A-Za-z]", after) else ""
        out.append(_Num(cur, val, suf, qual, next_word, frozenset(following + preceding)))
    return out


def _bare(word: str) -> str:
    """Lower-cased word without a possessive ending."""
    return re.sub(r"['’]s$", "", word.lower()).strip("'’")


def _lemma(word: str) -> str:
    w = _bare(word)
    return _IRREGULAR.get(w, stem(w))


def _unit(n: _Num) -> tuple[str, str]:
    return n.cur, n.suf.replace("+", "")


def check_numbers(text: str, source_text: str) -> list[Issue]:
    """Numbers: same value, same unit, same counted thing, same qualifier class as in the source."""
    issues: list[Issue] = []
    allowed = _number_spans(source_text)
    for n in _number_spans(text):
        label = f"{n.cur}{n.val}{n.suf}"
        same_value = [a for a in allowed if a.val == n.val]
        if not same_value:
            issues.append(_issue("number_not_in_source", f"number '{label}' does not appear in the verified source fact"))
            continue
        same_unit = [a for a in same_value if _unit(a) == _unit(n)]
        if not same_unit:
            shown = ", ".join(sorted({f"{a.cur}{a.val}{a.suf}" for a in same_value}))
            issues.append(_issue("number_unit_changed",
                                 f"number '{label}' appears in the verified fact only as '{shown}' (a different unit)"))
            continue
        if not n.cur and not n.suf.replace("+", "") and n.next_word:
            if not any(n.next_word in a.near for a in same_unit):
                issues.append(_issue("number_unit_changed",
                                     f"'{label}' counts something else in the verified fact (here: '{n.next_word}…')"))
                continue
        cand_q = n.qual or ("lower bound" if n.suf.endswith("+") else "")
        src_q = {a.qual or ("lower bound" if a.suf.endswith("+") else "") for a in same_unit}
        if cand_q in src_q:
            continue
        if not cand_q:
            issues.append(_issue("hedge_removed", f"the source qualifies {n.val} (e.g. 'approximately'); "
                                                  "the qualifier must be kept"))
        elif src_q == {""}:
            issues.append(_issue("qualifier_added",
                                 f"a {cand_q} qualifier was added to {n.val}; the verified fact states it plainly"))
        else:
            issues.append(_issue("qualifier_changed",
                                 f"{n.val} is qualified as {cand_q}, but the verified fact uses "
                                 f"{' / '.join(sorted(q or 'no qualifier' for q in src_q))}"))
    src_words = {w.lower() for w in _NUMBER_WORDS.findall(source_text)}
    for w in sorted({w.lower() for w in _NUMBER_WORDS.findall(text)}):
        if w not in src_words:
            issues.append(_issue("quantity_not_in_source",
                                 f"quantity word '{w}' does not appear in the verified source fact"))
    for m in sorted({m.group(0) for m in _FOLD.finditer(text)}):
        if not mentions(source_text, m):
            issues.append(_issue("quantity_not_in_source",
                                 f"multiplier '{m}' does not appear in the verified source fact"))
    return issues


def _skills_in(texts: Iterable[str]) -> set[str]:
    found: set[str] = set()
    for t in texts:
        found.update(lexicon.find_skills(t, ignore_case=True))
    return found


def check_skills(text: str, source_facts: Sequence[Fact], profile: CandidateProfile, *, strict: bool = True,
                 extra_allowed: Iterable[str] = ()) -> list[Issue]:
    """Lexicon tools/skills (matched in any letter case) must come from the source fact."""
    source_allowed = _skills_in([f.text for f in source_facts] + [s for f in source_facts for s in f.skills])
    source_allowed |= _skills_in(extra_allowed)
    profile_allowed = _skills_in(profile.skills)
    issues = []
    for name, matched in lexicon.find_skill_spans(text, ignore_case=True):
        if name in source_allowed:
            continue
        if name in profile_allowed:
            if strict:
                issues.append(_issue("skill_not_in_source_fact",
                                     f"'{matched}' is in your skills list but not in this verified fact; "
                                     "a bullet may only name tools used in that fact"))
            continue
        issues.append(_issue("unverified_skill", f"'{matched}' is not in your verified skills or the source fact"))
    return issues


def check_leadership(text: str, source_text: str) -> list[Issue]:
    src = {w.lower() for w in _LEADERSHIP.findall(source_text)}
    return [
        _issue("leadership_claim", f"'{w}' implies leadership/ownership that the verified fact does not state")
        for w in sorted({w.lower() for w in _LEADERSHIP.findall(text)})
        if w not in src
    ]


def check_role_verbs(text: str, source_text: str) -> list[Issue]:
    """Build/ownership verbs the source does not use (e.g. 'Supported' -> 'Designed and developed')."""
    src_lemmas = {_lemma(w) for w in words(source_text)}
    support = _SUPPORT_VERBS.search(source_text)
    issues = []
    for w in sorted({m.group(0).lower() for m in _BUILD_VERBS.finditer(text)}):
        if _LEADERSHIP.fullmatch(w) or _lemma(w.split()[0]) in src_lemmas:
            continue
        if support:
            msg = (f"'{w}' turns the supporting role in the verified fact ('{support.group(0)}') into a "
                   "build/ownership claim")
        else:
            msg = f"'{w}' is a build/ownership verb that the verified fact does not use"
        issues.append(_issue("role_inflated", msg))
    return issues


def check_inflation(text: str, source_text: str) -> list[Issue]:
    """Scope, sole-credit and intensifier wording the verified fact does not contain."""
    return [
        _issue("inflated_wording", f"'{w}' inflates the scope or credit of the verified fact")
        for w in sorted({normalize_space(m.group(0)).lower() for m in _INFLATION.finditer(text)})
        if not mentions(source_text, w)
    ]


def _allowed_stems(text: str) -> set[str]:
    out: set[str] = set()
    for w in words(text):
        lw = _bare(w)
        out.add(stem(lw))
        for part in re.split(r"[-/]", lw):
            if part:
                out.add(stem(part))
    return out


def check_new_words(text: str, allowed_text: str, *, vocabulary: Iterable[str] = ()) -> list[Issue]:
    """Content words that are in none of the allowed (verified) texts nor the connective allowlist."""
    allowed = _allowed_stems(allowed_text) | CONNECTIVES | set(vocabulary)
    novel: list[str] = []
    for w in words(text):
        lw = _bare(w)
        if len(lw) < 3 or lw in STOPWORDS or stem(lw) in allowed:
            continue
        parts = [p for p in re.split(r"[-/]", lw) if p]
        if len(parts) > 1 and all(len(p) < 3 or p in STOPWORDS or stem(p) in allowed for p in parts):
            continue
        if w not in novel:
            novel.append(w)
    if not novel:
        return []
    return [_issue("new_content", "adds wording that is not in the verified fact, its skills/keywords or your "
                                  "skills list: " + ", ".join(f"'{w}'" for w in novel[:8]))]


def credential_text(profile: CandidateProfile) -> str:
    parts: list[str] = []
    for e in profile.education:
        parts += [e.degree, e.field or ""]
    for c in profile.certifications:
        parts += [c.name, c.issuer or ""]
    return "\n".join(p for p in parts if p)


def check_credentials(text: str, source_text: str, profile: CandidateProfile) -> list[Issue]:
    allowed = source_text + "\n" + credential_text(profile)
    issues = []
    for m in sorted({m.group(0) for m in _CREDENTIAL.finditer(text)}):
        if not mentions(allowed, m):
            issues.append(_issue("unverified_credential",
                                 f"'{m}' refers to a certification/degree that is not in your profile"))
    return issues


def _education_dates(edu: Education) -> str:
    from job_agent.resume.selection import education_dates

    return education_dates(edu)


def check_education_claims(text: str, profile: CandidateProfile, source_text: str = "") -> list[Issue]:
    """Degree / graduation wording: no completion claim for an expected degree; verbatim dates only."""
    if not profile.education:
        return []
    named = [e for e in profile.education
             if mentions(text, e.degree) or (e.field and mentions(text, e.field)) or mentions(text, e.institution)]
    generic = _DEGREE_WORDS.search(text)
    if not (named or generic or _GRADUATION.search(text)):
        return []
    concerned = named
    if not concerned:
        kind = (generic.group(0).lower() if generic else "")
        if kind.startswith("master") or kind in {"ms", "m.s.", "msc", "mba"}:
            concerned = [e for e in profile.education if "master" in e.degree.lower()]
        elif kind.startswith("bachelor") or kind in {"bs", "b.s.", "bsc", "ba", "b.a.", "btech", "b.tech"}:
            concerned = [e for e in profile.education if "bachelor" in e.degree.lower()]
        concerned = concerned or [e for e in profile.education if e.expected] or list(profile.education)
    issues: list[Issue] = []
    if any(e.expected for e in concerned):
        for w in sorted({normalize_space(m.group(0)).lower() for m in _COMPLETION.finditer(text)}):
            if not mentions(source_text, w):
                issues.append(_issue("completed_degree_claim",
                                     f"'{w}' presents an expected (not yet completed) degree as completed"))
                break
    dates = " | ".join(_education_dates(e) for e in concerned)
    for m in sorted({m.group(0) for m in _DATE_PHRASE.finditer(text)}):
        if not mentions(source_text, m) and not mentions(dates, m):
            issues.append(_issue("graduation_date_altered",
                                 f"'{m}' is not the verified education date ({dates or 'none in the profile'})"))
    return issues


def organizations(profile: CandidateProfile) -> list[str]:
    names = [x.employer for x in profile.experience] + [e.institution for e in profile.education]
    names += [p.name for p in profile.projects] + [c.issuer for c in profile.certifications if c.issuer]
    names += [x.team for x in profile.experience if x.team]
    return [n for n in names if n and n.strip()]


def check_organizations(text: str, source_text: str, profile: CandidateProfile, *,
                        forbidden_names: Iterable[str] = (), allowed_text: str = "") -> list[Issue]:
    issues: list[Issue] = []
    allowed_blob = source_text + "\n" + allowed_text
    for org in [*organizations(profile), *forbidden_names]:
        if org and mentions(text, org) and not mentions(allowed_blob, org):
            issues.append(_issue("unsupported_organization",
                                 f"mentions '{org}', which is not part of the verified source fact"))
    # Generic proper-name check: capitalized tokens the source does not contain.
    allowed_tokens = {t.lower() for t in _TOKEN.findall(allowed_blob)}
    allowed_tokens |= {t.lower() for s in profile.skills for t in _TOKEN.findall(s)}
    unknown: list[str] = []
    run: list[str] = []
    tokens = list(_TOKEN.finditer(text))
    for i, m in enumerate(tokens):
        tok = re.sub(r"(['’]s|[.'’-])+$", "", m.group(0))
        prefix = text[: m.start()].rstrip()
        at_start = not prefix or prefix[-1] in ".!?:;•-–—(\"'"
        is_name = bool(tok) and tok != "I" and (tok[0].isupper() or (any(c.isupper() for c in tok)
                                                                    and any(c.isdigit() for c in tok)))
        if at_start and tok.lower() in STOPWORDS:  # "At", "In", "During", "My" opening a sentence
            is_name = False
        known = tok.lower() in allowed_tokens or all(
            not part or part.lower() in allowed_tokens or not part[:1].isupper() for part in tok.split("-"))
        if is_name and not known:
            acronym = len(tok) >= 2 and tok.isupper()
            nxt = tokens[i + 1].group(0) if i + 1 < len(tokens) else ""
            next_is_name = nxt[:1].isupper() and nxt != "I"
            if not at_start or acronym or run or next_is_name:
                run.append(tok)
                continue
        if run:
            unknown.append(" ".join(run))
            run = []
    if run:
        unknown.append(" ".join(run))
    for name in dict.fromkeys(unknown):
        if any(name.lower() in i.message.lower() for i in issues):
            continue
        issues.append(_issue("unverified_name",
                             f"'{name}' (a name, organization or proper noun) does not appear in the verified source"))
    return issues


def work_authorization_terms(text: str) -> list[str]:
    """Work-authorization / visa / sponsorship / citizenship wording found in ``text``."""
    found = {normalize_space(m.group(0)) for m in _WORK_AUTH_CI.finditer(text or "")}
    found |= {m.group(0) for m in _WORK_AUTH_CS.finditer(text or "")}
    return sorted(found)


def check_forbidden_content(text: str, source_text: str = "") -> list[Issue]:
    issues: list[Issue] = []
    for m in work_authorization_terms(text):
        if not mentions(source_text, m):
            issues.append(_issue("work_authorization_mention",
                                 f"documents never mention work authorization / visa status ('{m}')"))
    for m in {m.group(0) for m in _LINK.finditer(text)}:
        if m not in source_text:
            issues.append(_issue("contains_link", f"generated text may not add links or e-mail addresses ('{m}')"))
    for m in {m.group(0) for m in _PLACEHOLDER.finditer(text)} | {m.group(0) for m in
                                                                  _BRACKET_PLACEHOLDER.finditer(text)}:
        if m not in source_text:
            issues.append(_issue("placeholder_text", f"contains placeholder text {m!r}"))
    return issues


def _mask_phrases(text: str, phrases: Iterable[str]) -> str:
    """``text`` with every exact occurrence of the given phrases blanked out."""
    for phrase in sorted({normalize_space(p) for p in phrases if p and len(normalize_space(p)) >= 2},
                         key=len, reverse=True):
        body = r"\s+".join(re.escape(part) for part in phrase.split(" "))
        text = re.sub(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", " ", text, flags=re.IGNORECASE)
    return text


def overlap_ratio(text: str, source_text: str, *, vocabulary: Iterable[str] = ()) -> float:
    cand = content_set(text)
    if not cand:
        return 0.0
    return len(cand & (content_set(source_text) | set(vocabulary))) / len(cand)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def validate_claim(text: str, source_facts: Sequence[Fact], profile: CandidateProfile, *,
                   strict_skills: bool = True, forbidden_names: Iterable[str] = (),
                   min_overlap: float = MIN_OVERLAP, context: str = "",
                   vocabulary: Iterable[str] = ()) -> list[Issue]:
    """Validate one generated statement against the verified fact(s) it restates.

    Returns an empty list when the statement is acceptable. ``forbidden_names``
    are extra organization names (e.g. the job's employer) that may only appear
    if the source fact contains them. ``context`` is *verified* profile text
    about the entry the fact belongs to (e.g. "Student Intern, Example State
    Agency" for a cover-letter sentence); it may supply names and words but
    never numbers. ``vocabulary`` is extra allowed connective stems. Listing
    text (job title, employer, description) must never be passed as context.
    """
    candidate = normalize_space(text)
    if not source_facts:
        return [_issue("no_source_fact", "a claim must cite at least one verified fact")]
    source_text = "\n".join(normalize_space(f.text) for f in source_facts)
    if candidate == normalize_space(source_text):
        # Verbatim restatement of a single fact -- only document-level rules apply.
        return check_forbidden_content(candidate, source_text)
    issues: list[Issue] = []
    if not candidate:
        return [_issue("empty", "empty statement")]
    vocabulary = frozenset(vocabulary)
    n_words = len(words(candidate))
    src_words = len(words(source_text)) + len(words(context))
    if n_words < MIN_WORDS:
        issues.append(_issue("too_short", f"only {n_words} words"))
    if n_words > max(40, int(src_words * 1.6)) or len(candidate) > MAX_CHARS:
        issues.append(_issue("too_long", f"{n_words} words / {len(candidate)} characters is too long"))
    skill_text = "\n".join(s for f in source_facts for s in f.skills)
    keyword_text = "\n".join(k for f in source_facts for k in f.keywords)
    # Exact verified context phrases (a title such as "Team Lead", an employer such as "Oracle") are
    # masked for the claim checks below; they never make the same words acceptable elsewhere.
    masked = _mask_phrases(candidate, context.split("\n"))
    issues += check_numbers(masked, source_text)
    issues += check_skills(masked, source_facts, profile, strict=strict_skills)
    issues += check_leadership(masked, source_text)
    issues += check_role_verbs(masked, source_text)
    issues += check_inflation(masked, source_text)
    issues += check_credentials(candidate, source_text + "\n" + context, profile)
    issues += check_education_claims(candidate, profile, source_text)
    issues += check_organizations(candidate, source_text, profile, forbidden_names=forbidden_names,
                                  allowed_text="\n".join([skill_text, context]))
    issues += check_forbidden_content(candidate, source_text)
    allowed_text = "\n".join([source_text, skill_text, keyword_text, *profile.skills, context])
    issues += check_new_words(candidate, allowed_text, vocabulary=vocabulary)
    ratio = overlap_ratio(candidate, "\n".join([source_text, skill_text, context]), vocabulary=vocabulary)
    if ratio < min_overlap:
        issues.append(_issue("low_overlap",
                             f"only {ratio:.0%} of the content words come from the verified fact "
                             f"(minimum {min_overlap:.0%})"))
    return issues


def summarize(issues: Sequence[Issue]) -> list[str]:
    return [f"{i.code}: {i.message}" for i in issues]


def document_status(issues: Sequence[Issue]) -> str:
    return "needs_review" if any(i.severity in {"error", "review"} for i in issues) else "draft"


# --------------------------------------------------------------------------- #
# Placeholders in used profile fields
# --------------------------------------------------------------------------- #
def _walk(obj: Any, path: str, out: list[tuple[str, str]]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            _walk(v, f"{path}.{k}" if path else k, out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _walk(v, f"{path}[{i}]", out)
    elif isinstance(obj, str) and _PLACEHOLDER.search(obj):
        out.append((path, obj))


_UNUSED_PREFIXES = ("work_authorization", "preferences", "verified_answers")


def placeholder_issues(profile: CandidateProfile, rendered_text: str, *, profile_label: str = "your profile",
                       document: str | None = None) -> list[Issue]:
    """One ``review`` issue per used profile field that still holds ``<placeholder>`` text."""
    found: list[tuple[str, str]] = []
    _walk(profile.model_dump(), "", found)
    text = normalize_space(rendered_text)
    issues: list[Issue] = []
    reported: set[str] = set()
    for path, value in found:
        if path.startswith(_UNUSED_PREFIXES):
            continue
        for ph in _PLACEHOLDER.findall(value):
            ph = normalize_space(ph)
            if ph in text and path not in reported:
                reported.add(path)
                issues.append(Issue(
                    code="profile_placeholder", severity="review", location=path, document=document,  # type: ignore[arg-type]
                    message=f"Profile field '{path}' still contains placeholder text {ph!r}. Replace it with your "
                            f"verified value in {profile_label} and regenerate.",
                ))
    for ph in sorted(set(_PLACEHOLDER.findall(text))):
        if not any(ph in i.message for i in issues):
            issues.append(Issue(code="placeholder_text", severity="review", document=document,  # type: ignore[arg-type]
                                message=f"The document contains placeholder text {ph!r}; fill it in your profile."))
    return issues


# --------------------------------------------------------------------------- #
# Document-level validation
# --------------------------------------------------------------------------- #
def _header_issues(header: Any, profile: CandidateProfile) -> list[Issue]:
    p = profile.personal
    expected = {"full_name": p.full_name, "email": p.email, "phone": p.phone, "location": p.location}
    issues = []
    for key, value in expected.items():
        if getattr(header, key) != value:
            issues.append(_issue("header_mismatch", f"header field {key} does not match profile.personal.{key}",
                                 location=f"personal.{key}"))
    allowed_links = {u for u in (p.linkedin_url, p.github_url, p.portfolio_url) if u}
    for link in header.links:
        if link not in allowed_links:
            issues.append(_issue("header_mismatch", f"link {link!r} is not in profile.personal",
                                 location="personal"))
    return issues


def validate_resume(content: ResumeContent, profile: CandidateProfile, *,
                    profile_label: str = "your profile") -> list[Issue]:
    issues = _header_issues(content.header, profile)
    if content.summary and normalize_space(content.summary) != normalize_space(profile.summary or ""):
        issues.append(_issue("unverified_summary", "summary text is not the verified profile summary"))
    edu_by_id = {e.id: e for e in profile.education}
    for entry in content.education:
        edu = edu_by_id.get(entry.id)
        if edu is None:
            issues.append(_issue("unknown_education", f"education entry {entry.id!r} is not in the profile"))
            continue
        if (entry.institution, entry.degree, entry.field) != (edu.institution, edu.degree, edu.field):
            issues.append(_issue("education_mismatch", f"education {entry.id} differs from the profile",
                                 location=f"education.{entry.id}"))
        if entry.dates != _education_dates(edu) or (edu.end and edu.end not in entry.dates):
            issues.append(_issue("education_dates_altered",
                                 f"education {entry.id} dates must be exactly as in the profile",
                                 location=f"education.{entry.id}"))
    exp_by_id = {x.id: x for x in profile.experience}
    for entry in content.experience:
        exp = exp_by_id.get(entry.id)
        if exp is None:
            issues.append(_issue("unknown_experience", f"experience entry {entry.id!r} is not in the profile"))
            continue
        if (entry.title, entry.organization, entry.dates) != (exp.title, exp.employer, exp.dates):
            issues.append(_issue("experience_mismatch",
                                 f"experience {entry.id}: title/employer/dates must match the profile",
                                 location=f"experience.{entry.id}"))
    project_ids = {p.id for p in profile.projects}
    for entry in content.projects:
        if entry.id not in project_ids:
            issues.append(_issue("unknown_project", f"project {entry.id!r} is not in the profile"))
    for section, bullet in content.all_bullets():
        issues += _bullet_issues(section, bullet.text, bullet.fact_ids, profile)
    profile_skills = set(profile.skills)
    for skill in content.skills:
        if skill not in profile_skills:
            issues.append(_issue("unverified_skill", f"skills section lists {skill!r}, which is not in profile.skills",
                                 location="skills"))
    text = content.plain_text()
    contact = "\n".join(content.header.contact_parts())
    issues += [i for i in check_forbidden_content(text, contact) if i.code != "placeholder_text"]
    issues += placeholder_issues(profile, text, profile_label=profile_label)
    for i in issues:
        i.document = "resume"
    return issues


def _bullet_issues(section: str, text: str, fact_ids: Sequence[str], profile: CandidateProfile) -> list[Issue]:
    if not fact_ids:
        return [_issue("untraced_claim", f"{section}: a statement has no source fact id", location=section)]
    facts = [profile.fact_by_id(fid) for fid in fact_ids]
    missing = [fid for fid, f in zip(fact_ids, facts, strict=True) if f is None]
    if missing:
        return [_issue("unknown_fact_id", f"{section}: fact id(s) {missing} are not in the profile",
                       location=section)]
    claim_issues = [i for i in validate_claim(text, [f for f in facts if f], profile)
                    if i.code not in {"placeholder_text"}]
    if claim_issues:
        return [_issue("unverified_bullet", f"{section}: '{text[:80]}' failed validation: "
                       + "; ".join(summarize(claim_issues)), location=section)]
    return []


# --------------------------------------------------------------------------- #
# Cover letters
# --------------------------------------------------------------------------- #
_COMPANY_REF = re.compile(
    r"(?<![A-Za-z])(your\s+(?:company|organization|organisation|firm|mission|vision|values|culture|products?|"
    r"customers|clients|platform|growth|success|reputation|leadership|history|commitment|work\s+in)|"
    r"you\s+are|you're|you\s+have|industry\s+leader|market\s+leader)(?![A-Za-z])",
    re.IGNORECASE,
)
_PUFFERY = re.compile(
    r"(?<![A-Za-z])(industry[- ]leading|industry\s+leader|market\s+leader|leading|renowned|award[- ]winning|"
    r"world[- ]class|best[- ]in[- ]class|fastest[- ]growing|innovative|cutting[- ]edge|pioneering|prestigious|"
    r"recently|announced|acquired|acquisition|founded\s+in|headquartered|#1|number\s+one|unicorn|"
    r"reputation|famous|well[- ]known|esteemed)(?![A-Za-z])",
    re.IGNORECASE,
)
#: Words a cover letter may use around verified content without making a claim.
LETTER_VOCABULARY = content_set(
    """
    apply applying application position role opportunity opportunities excited interest interested eager pleased
    thank thanks consider considering consideration discuss discussing welcome contribute contributing contribution
    team teams experience experiences skill skills background writing write pursue pursuing currently hiring listing
    mentions mention mentioned focus area areas grow growing work working drawn match matches career learn learning
    look forward hear sincerely regards dear manager candidate qualified qualification believe confident bring
    strong passion passionate motivated motivation join joining impact role's listing's references reference related
    relevant align aligns alignment value values excellent help helping support supporting ability abilities
    emphasize emphasizes emphasized highlight highlights describes describe seek seeks ask asks require requires
    graduate graduating graduation expected program programme studies study student currently degree
    """
)
#: Words a fact-restating cover-letter sentence may add around the fact ("In my ... role at ..., I ...").
BODY_VOCABULARY: frozenset[str] = frozenset(content_set(
    "role position internship project studies study program work working time also additionally addition same"))

_EMPLOYMENT_WORDS = {"internship": "internship intern", "full_time": "full-time", "part_time": "part-time",
                     "contract": "contract contractor", "other": ""}


def fact_context(profile: CandidateProfile, facts: Sequence[Fact]) -> str:
    """Verified profile text about the entries the facts belong to (titles, employers, project names, ...)."""
    ids = {f.id for f in facts}
    parts: list[str] = []
    for x in profile.experience:
        if ids & {f.id for f in x.facts}:
            parts += [x.title, x.employer, x.team or "", _EMPLOYMENT_WORDS.get(x.employment_type, "")]
    for p in profile.projects:
        if ids & {f.id for f in p.facts}:
            parts.append(p.name)
    for e in profile.education:
        if ids & {f.id for f in e.facts}:
            parts += [e.degree, e.field or "", e.institution]
    return "\n".join(p for p in parts if p)


def check_company_claims(sentence: str, *, employer: str, listing_text: str, profile_text: str) -> list[Issue]:
    """A sentence about the employer may only use information from the listing."""
    issues: list[Issue] = []
    refers = bool(_COMPANY_REF.search(sentence)) or bool(employer and mentions(sentence, employer))
    for m in {m.group(0) for m in _PUFFERY.finditer(sentence)}:
        if not mentions(listing_text, m) and not mentions(profile_text, m):
            issues.append(_issue("unverifiable_company_claim",
                                 f"'{m}' is a claim about the employer that the listing does not support"))
    if refers:
        known = content_set(listing_text) | content_set(profile_text) | LETTER_VOCABULARY | content_set(employer)
        novel = [w for w in content_words(sentence) if w not in known]
        if len(set(novel)) >= 2:
            issues.append(_issue("unverifiable_company_claim",
                                 "sentence about the employer contains information not found in the listing: "
                                 + ", ".join(sorted(set(novel))[:6])))
    return issues


_LISTING_LEAD = re.compile(r"^(?:Your listing mentions|The listing also mentions) (?P<kw>.{1,100}?)\. "
                           r"(?P<core>(?:In|During) .+)$")


def validate_body_claim(text: str, facts: Sequence[Fact], profile: CandidateProfile, *, employer: str | None,
                        listing_text: str) -> list[Issue]:
    """Validate one cover-letter body claim (a sentence restating ``facts``)."""
    issues: list[Issue] = []
    core = text
    lead = _LISTING_LEAD.match(text)
    if lead:
        keyword, core = lead.group("kw"), lead.group("core")
        fact_blob = "\n".join([*(f.text for f in facts), *(s for f in facts for s in f.skills),
                               *(k for f in facts for k in f.keywords)])
        in_listing = mentions(listing_text, keyword) or keyword in lexicon.find_skills(listing_text)
        in_fact = mentions(fact_blob, keyword) or keyword in _skills_in([fact_blob])
        if not (in_listing and in_fact):
            issues.append(_issue("unverified_listing_reference",
                                 f"'{keyword}' must be literally in the listing and in the cited verified fact"))
    issues += validate_claim(core, facts, profile, forbidden_names=[employer] if employer else [],
                             context=fact_context(profile, facts), vocabulary=BODY_VOCABULARY)
    profile_text = "\n".join([*organizations(profile), *(f.text for f in facts)])
    for sentence in split_sentences(core):
        issues += check_company_claims(sentence, employer=employer or "", listing_text=listing_text,
                                       profile_text=profile_text)
    return issues


def validate_cover_letter(content: CoverLetterContent, claims: Sequence[Claim], profile: CandidateProfile, *,
                          listing_text: str, profile_label: str = "your profile") -> list[Issue]:
    """Document-level checks for a cover letter (template or validated LLM draft).

    The opening, motivation and closing paragraphs must be exactly the
    deterministic template (built from the profile and the title-like job
    title/employer slots); every other sentence must belong to a claim that
    cites verified facts and re-validates against them.
    """
    from job_agent.resume import cover_letter as cl

    issues: list[Issue] = _header_issues(content.header, profile)
    if content.signature != profile.personal.full_name:
        issues.append(_issue("signature_mismatch", "signature must be the profile name"))
    if content.job_title and cl.title_slot(content.job_title) != content.job_title:
        issues.append(_issue("unsafe_job_title", "the job title slot holds text that is not a plain job title"))
    if content.employer and cl.employer_slot(content.employer) != content.employer:
        issues.append(_issue("unsafe_employer", "the employer slot holds text that is not a plain employer name"))
    title, employer = content.job_title or None, content.employer or None
    paras = [normalize_space(p) for p in content.paragraphs]
    fixed_ok = len(paras) >= 3
    if not fixed_ok or paras[0] != cl.opening_paragraph(profile, job_title=title, employer=employer):
        issues.append(_issue("template_altered", "the opening paragraph is not the verified template opening"))
    if not fixed_ok or paras[-2] != cl.motivation_sentence(profile, listing_text, title):
        issues.append(_issue("template_altered", "the motivation paragraph is not the verified template text"))
    if not fixed_ok or paras[-1] != cl.CLOSING_PARAGRAPH:
        issues.append(_issue("template_altered", "the closing paragraph is not the verified template text"))
    body = " ".join(paras[1:-2]) if fixed_ok else " ".join(paras)
    body_claims = [c for c in claims if not c.section.startswith("cover_letter:opening")]
    if normalize_space(" ".join(c.text for c in body_claims)) != normalize_space(body):
        issues.append(_issue("untraced_claim", "cover-letter body text is not exactly the fact-traced claims"))
    known_ids = {f.id for f in profile.all_facts()}
    for claim in body_claims:
        if not claim.fact_ids:
            issues.append(_issue("untraced_claim", f"'{claim.text[:80]}' cites no verified fact"))
            continue
        if not set(claim.fact_ids) <= known_ids:
            issues.append(_issue("unknown_fact_id", f"claim cites unknown fact ids {claim.fact_ids}"))
            continue
        facts = [f for f in (profile.fact_by_id(fid) for fid in claim.fact_ids) if f]
        problems = [i for i in validate_body_claim(claim.text, facts, profile, employer=employer,
                                                   listing_text=listing_text) if i.code != "placeholder_text"]
        if problems:
            issues.append(_issue("unverified_claim", f"'{claim.text[:80]}' failed validation: "
                                 + "; ".join(summarize(problems))))
    text = content.plain_text()
    contact = "\n".join(content.header.contact_parts())
    issues += [i for i in check_forbidden_content(text, contact) if i.code != "placeholder_text"]
    issues += placeholder_issues(profile, text, profile_label=profile_label, document="cover_letter")
    for i in issues:
        i.document = "cover_letter"
    return issues
