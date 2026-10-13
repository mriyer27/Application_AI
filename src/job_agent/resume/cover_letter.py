"""Cover letters: a deterministic template, plus an optional validated LLM body.

Template structure (every sentence is either fixed wording, a profile value, a
title-like job title / employer slot, or a verified fact):

1. "Dear Hiring Team,"
2. Opening: the job title and employer, and the candidate's degree program
   with its (expected) graduation date verbatim from the profile.
3. Two or three body sentences, each restating one selected verified fact and
   naming a requirement keyword that literally appears in the listing.
4. Motivation: only ``profile.preferences.preferred_themes`` that the listing
   also mentions -- no knowledge about the company is invented.
5. Closing, "Sincerely," and the profile name.

The job title / employer come from the untrusted listing. They are used only
when they look like a plain title / name (no sentence punctuation, no
first-person words, no instructions, at most 12 words, no full-width /
look-alike / invisible characters, and nothing claim-like: no words about a
person such as "candidate" or "he", no verbs such as "led" / "is" / "holds",
no counts of people / years / money, no work-authorization wording, no
background wording such as "as a ...", "bringing", "with a/my", "former",
"ex-", "alumni", "cleared", proficiency words ("proven", "hands-on") or
graduation wording other than a leading "Graduate" / a school or program
name, and in an employer no degree / certification wording; a job title's
numbers may only be years, versions or level numerals). Otherwise the
letter says "the advertised position" / "your organization" and a ``review``
issue is added. They are never a source for any claim.

With the LLM (``job_agent.llm.generation.draft_cover_letter``), the opening,
motivation and closing paragraphs stay the deterministic template. The draft
may only supply body sentences: each must restate fact(s) the draft cites and
pass ``validation.validate_claim`` against them (with the verified entry
context such as "Student Intern, Example State Agency"). Every first-person
sentence is a claim. Sentences are judged and kept in their normalized form
(``validation.claim_text``); one with full-width, look-alike or invisible
characters the cited facts do not contain is rejected. Pure letter boilerplate
("Thank you for your time.") is dropped in favour of the template's own wording. Any other sentence, or any
failure, means the whole template letter is used. Cover letters are always
``needs_review``.
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
TITLE_FALLBACK = "the advertised position"
EMPLOYER_FALLBACK = "your organization"
MAX_SLOT_WORDS = 12


def display_text(text: str | None, *, max_len: int) -> str:
    """Untrusted single-line text made safe to show (control/markup characters removed, truncated)."""
    cleaned = normalize_space(clean_untrusted(text or "", max_chars=max_len * 2))
    cleaned = re.sub(r"[<>{}\[\]\\`]", "", cleaned)
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rsplit(" ", 1)[0].rstrip(",;:-") or cleaned[:max_len]
    return cleaned


# --------------------------------------------------------------------------- #
# Title / employer slots (untrusted listing text)
# --------------------------------------------------------------------------- #
_SLOT_ABBREVIATIONS = frozenset(
    "sr jr snr mgr assoc asst dir vp svp evp avp eng engr co inc ltd corp st dept intl bros llc plc no mt ft dr "
    "univ natl".split())
_SLOT_FIRST_PERSON = re.compile(
    r"(?<![A-Za-z'’])(?:me|my|mine|myself|we|our|ours|ourselves|i['’](?:m|ve|d|ll)|im|ive)(?![A-Za-z'’])",
    re.IGNORECASE)
_SLOT_US = re.compile(r"(?<![A-Za-z])us(?![A-Za-z])")  # lower-case "us" only; "US" is a country
_PRONOUN_I = re.compile(r"(?<![A-Za-z'’])I(?![A-Za-z'’])")
#: Words about a person ("this candidate led ...", "applicant is authorized ...").
_SLOT_PEOPLE = re.compile(r"(?<![A-Za-z'’])(?:candidates?|applicants?|this\s+person|the\s+person|himself|herself|"
                          r"themselves|yourself)(?![A-Za-z'’])", re.IGNORECASE)
_SLOT_PRONOUNS = re.compile(r"(?<![A-Za-z'’])(?:he|she|him|his|her|hers|they|them|their|theirs|you|your|yours)"
                            r"(?![A-Za-z'’])", re.IGNORECASE)  # job titles only ("Her Campus" is an employer)
#: Verbs, relative pronouns and experience words that make a slot a statement rather than a name.
_SLOT_CLAIM_WORDS = re.compile(
    r"(?<![A-Za-z'’-])(?:is|are|was|were|am|been|being|has|have|had|hold|holds|held|will|would|can|could|should|"
    r"does|did|who|whom|whose|which|where|led|oversaw|overseen|spearheaded|supervised|mentored|architected|founded|"
    r"co-founded|owned|headed|championed|pioneered|orchestrated|built|designed|created|launched|deployed|"
    r"implemented|delivered|drove|shipped|achieved|increased|reduced|improved|saved|earned|won|grew|grown|"
    r"generated|received|awarded|completed|graduated|years?|yrs|decades?|developed(?!\s+markets?)|"
    r"managed(?!\s+(?:services?|care|security|cloud|hosting|networks?|it|print|detection|accounts?|markets?|futures|"
    r"funds?|portfolios?|solutions?|infrastructure)))(?![A-Za-z'’-])",
    re.IGNORECASE)
#: A count of people / time / money ("40 engineers", "ten years", "team of 40").
_SLOT_QUANTITY = re.compile(
    r"(?<![A-Za-z0-9])(?:\d[\d,.]*|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|"
    r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundreds?|thousands?|millions?|dozens?|several|numerous|many)"
    r"\s*(?:\+|plus)?\s+(?:of\s+)?(?:years?|yrs|months?|decades?|engineers|developers|people|persons|staff|"
    r"employees|members|direct\s+reports|reports|analysts|scientists|designers|clients|customers|users|projects|"
    r"programs|teams|dollars|usd|million|billion|percent)(?![A-Za-z])"
    r"|(?<![A-Za-z])(?:team|group|staff|department|organization)\s+of\s+\d"
    r"|[$£€%]|\d\s*\+|(?<![A-Za-z])percent(?![A-Za-z])",
    re.IGNORECASE)
#: Wording that describes the candidate's background rather than naming a job / organization
#: ("Acme as a Stanford graduate", "Acme, bringing a PhD", "former Google engineer", "TS/SCI Cleared Analyst").
_SLOT_BACKGROUND = re.compile(
    r"(?<![A-Za-z'’-])(?:as\s+(?:an?|the)(?!\s+service(?![A-Za-z]))|bringing|with\s+(?:an?|my)|former(?:ly)?|cleared|"
    r"alumn\w*)(?![A-Za-z'’-])|(?<![A-Za-z])ex-(?=[A-Za-z])",
    re.IGNORECASE)
#: Proficiency claims ("Python expertise", "proven", "hands-on"); "Specialist" / "Expert" / "Advanced" are title words.
_SLOT_PROFICIENCY = re.compile(
    r"(?<![A-Za-z'’-])(?:expertise|proficient|proficiency|mastery|skilled|seasoned|fluent|track\s+record|proven|"
    r"hands-on|in-depth|extensive)(?![A-Za-z'’-])",
    re.IGNORECASE)
#: "Graduate" followed by a word that makes it a program, school or job ("Graduate School of Business").
_SLOT_GRADUATE_NOUN = re.compile(
    r"\s+(?:school|college|institute|center|centre|studies|programs?|division|assistant|research|student|teaching|"
    r"management|hotels?)(?![A-Za-z])",
    re.IGNORECASE)
_SLOT_MULTIPLIER = re.compile(r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?\s*[kKmMbBxX×](?![A-Za-z0-9])|"
                              r"(?<![A-Za-z0-9.])[xX×]\d")
_SLOT_NUMBER = re.compile(r"(?<![A-Za-z0-9.,])\d+(?:[.,]\d+)*(?![A-Za-z0-9])")
_LEVEL_WORDS = frozenset("level tier grade band step class phase gs ic".split())


def _title_numbers_ok(cleaned: str) -> bool:
    """Numbers in a job title may only be years, versions or level numerals ("Analyst 2", "Tier 1", "Web 2.0")."""
    if _SLOT_MULTIPLIER.search(cleaned):
        return False
    for m in _SLOT_NUMBER.finditer(cleaned):
        num = m.group(0)
        if re.fullmatch(r"(?:19|20)\d{2}", num) or re.fullmatch(r"\d+\.\d+", num):
            continue
        before = re.findall(r"[A-Za-z]+", cleaned[: m.start()])
        if before and before[-1].lower() in _LEVEL_WORDS:
            continue
        after = cleaned[m.end():]
        if len(num) <= 2 and (not after.strip() or re.match(r"\s*[-–—|/(),&]", after)):
            continue
        return False
    return True


def _graduation_claim(cleaned: str, *, title: bool) -> bool:
    """Graduation wording other than a leading "Graduate" or a program / school / job name.

    "Graduate Assistant", "New Graduate Analyst", "Stanford Graduate School of
    Business" and (in a job title) a cohort such as "Class of 2027" name
    something; "Acme and Stanford graduate network" or "Acme alumni" describe the
    candidate.
    """
    for m in validation._GRADUATION.finditer(cleaned):
        if title and re.fullmatch(r"class\s+of", m.group(0), re.IGNORECASE) and re.match(
                r"\s+(?:19|20)\d{2}(?![\w])", cleaned[m.end():]):
            continue
        if m.group(0).lower() == "graduate":
            before = cleaned[: m.start()]
            if (not before.strip() or re.search(r"(?<![A-Za-z])(?:new|recent)\s+$", before, re.IGNORECASE)
                    or _SLOT_GRADUATE_NOUN.match(cleaned[m.end():])):
                continue
        return True
    return False


def _claim_like(cleaned: str, *, title: bool) -> bool:
    """True when slot text states something about a person (claims, counts, legal status, background) instead of naming."""
    if _SLOT_PEOPLE.search(cleaned) or _SLOT_CLAIM_WORDS.search(cleaned) or _SLOT_QUANTITY.search(cleaned):
        return True
    if validation.work_authorization_terms(cleaned):
        return True
    if _SLOT_BACKGROUND.search(cleaned) or _SLOT_PROFICIENCY.search(cleaned) or _graduation_claim(cleaned, title=title):
        return True
    if not title and (validation._CREDENTIAL.search(cleaned) or validation._DEGREE_WORDS.search(cleaned)):
        return True  # degrees / certifications never name an employer ("Acme, certified PMP")
    if title:
        return bool(_SLOT_PRONOUNS.search(cleaned)) or not _title_numbers_ok(cleaned)
    return False


def _slot(text: str | None, *, max_len: int, title: bool = False) -> str | None:
    """A title-like single line, or None when the text could carry a sentence / claim / instruction.

    The text must not contain characters that could disguise words (full-width,
    look-alike or invisible characters); the returned slot is normalized text.
    """
    if validation.unexpected_characters(normalize_space(text)):
        return None
    cleaned = normalize_space(clean_untrusted(text or "", max_chars=max_len * 4))
    cleaned = normalize_space(re.sub(r"[<>{}\[\]\\`]", "", cleaned)).strip(" ,-–—|/")
    cleaned = validation.claim_text(cleaned)
    if not cleaned or len(cleaned) > max_len:
        return None
    if re.search(r"[!?;:…\"“”]", cleaned) or validation._LINK.search(cleaned):
        return None
    for m in re.finditer(r"\.", cleaned):
        before = re.search(r"([A-Za-z0-9]+)$", cleaned[: m.start()])
        token = before.group(1) if before else ""
        following = cleaned[m.end(): m.end() + 1]
        if token.isdigit() and following.isdigit():  # "Web 2.0"
            continue
        if (len(token) == 1 and token.isalpha()) or token.lower() in _SLOT_ABBREVIATIONS:  # "U.S.", "Sr.", "Inc."
            continue
        return None
    if len([t for t in cleaned.split() if re.search(r"[A-Za-z0-9]", t)]) > MAX_SLOT_WORDS:
        return None
    if _SLOT_FIRST_PERSON.search(cleaned) or _SLOT_US.search(cleaned):
        return None
    for m in _PRONOUN_I.finditer(cleaned):
        rest = cleaned[m.end():]
        if not rest.strip() or re.match(r"^\s*[-–—|/(),&]", rest):  # roman numeral level: "Analyst I"
            continue
        return None
    if injection_suspected(cleaned) or _claim_like(cleaned, title=title):
        return None
    return cleaned


def title_slot(job_title: str | None) -> str | None:
    """The job title if it looks like a plain job title, else None (use ``TITLE_FALLBACK``)."""
    return _slot(job_title, max_len=120, title=True)


def employer_slot(employer: str | None) -> str | None:
    """The employer if it looks like a plain organization name, else None (use ``EMPLOYER_FALLBACK``)."""
    return _slot(employer, max_len=100)


# --------------------------------------------------------------------------- #
# Template
# --------------------------------------------------------------------------- #
def format_letter_date(on: date) -> str:
    return f"{on.strftime('%B')} {on.day}, {on.year}"


def _primary_education(profile: CandidateProfile) -> Education:
    expected = [e for e in profile.education if e.expected]
    return expected[0] if expected else profile.education[0]


def opening_paragraph(profile: CandidateProfile, *, job_title: str | None, employer: str | None) -> str:
    position = f"the {job_title} position" if job_title else TITLE_FALLBACK
    first = f"I am writing to apply for {position} at {employer or EMPLOYER_FALLBACK}."
    edu = _primary_education(profile)
    program = edu.degree + (f" in {edu.field}" if edu.field else "")
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


def motivation_sentence(profile: CandidateProfile, listing_text: str, job_title: str | None) -> str:
    """Fixed motivation wording; names only preferred themes the listing literally mentions."""
    role = f"the {job_title} role" if job_title else "this role"
    themes = [t for t in profile.preferences.preferred_themes if t.strip() and mentions(listing_text, t)]
    if not themes:
        return f"I would welcome the opportunity to bring this experience to {role}."
    joined = themes[0] if len(themes) == 1 else ", ".join(themes[:-1]) + f" and {themes[-1]}"
    return (f"The listing's references to {joined} match the areas I most want to work in, "
            f"and I would welcome the opportunity to bring this experience to {role}.")


CLOSING_PARAGRAPH = ("Thank you for considering my application. I would welcome the opportunity to discuss how my "
                     "experience could contribute to your team.")


def template_cover_letter(profile: CandidateProfile, kw: JobKeywords, *, job_title: str | None,
                          employer: str | None, on: date) -> tuple[CoverLetterContent, list[Claim], list[Issue]]:
    """The deterministic letter. ``job_title`` / ``employer`` must be slot values (or None for the fallback)."""
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
    paragraphs.append(motivation_sentence(profile, kw.text, job_title))
    paragraphs.append(CLOSING_PARAGRAPH)
    content = CoverLetterContent(
        header=build_header(profile), date_text=format_letter_date(on), employer=employer or "",
        job_title=job_title or "", greeting=GREETING, paragraphs=paragraphs, closing=CLOSING,
        signature=profile.personal.full_name,
    )
    return content, claims, issues


# --------------------------------------------------------------------------- #
# LLM draft validation
# --------------------------------------------------------------------------- #
_PROFICIENCY = re.compile(
    r"(?<![A-Za-z])(expert|expertise|proficient|proficiency|mastery|skilled|advanced|extensive|in-depth|deep|"
    r"seasoned|veteran|specialist|fluent|years\s+of|track\s+record|proven|hands-on)(?![A-Za-z])",
    re.IGNORECASE,
)
_GREETING_RE = re.compile(r"^\s*(dear|hello|hi|to whom)\b", re.IGNORECASE)
_GREETING_PREFIX = re.compile(r"^\s*(?:dear|hello|hi)\b[^,]{0,40},\s*", re.IGNORECASE)
_SIGNOFF_RE = re.compile(r"^\s*(sincerely|best regards|regards|kind regards|thank you,|warm regards|respectfully)\b",
                         re.IGNORECASE)
_FIRST_PERSON = re.compile(r"(?<![A-Za-z'’])(?:I|(?i:me|my|mine|myself))(?![A-Za-z])")
#: Openers of letter boilerplate (no claim) that the template's own opening/closing replaces.
_BOILERPLATE_START = re.compile(
    r"^(?:I\s+am\s+writing\s+to\s+(?:apply|express|submit)|I['’]m\s+writing\s+to\s+(?:apply|express|submit)|"
    r"I\s+(?:would|['’]d)\s+(?:welcome|appreciate|love|be\s+(?:glad|happy|delighted|grateful))|"
    r"Thank\s+you|Thanks\s+(?:for|again)|I\s+look\s+forward|I\s+am\s+looking\s+forward|"
    r"I\s+am\s+(?:excited|eager|pleased|happy|delighted|glad|grateful)\s+to\s+(?:apply|submit|be\s+considered|"
    r"have\s+the\s+(?:opportunity|chance))|Please\s+(?:feel\s+free|let\s+me\s+know|consider)|I\s+appreciate\s+your)",
    re.IGNORECASE,
)
BOILERPLATE_VOCABULARY = content_set(
    """
    write writing apply applying application express expressing interest interested position role job opening
    opportunity opportunities thank thanks time consideration considering consider welcome discuss discussing
    discussion contribute contributing contribution team look looking forward hear hearing excited eager pleased
    happy delighted glad grateful submit submitting candidacy review reviewing attention appreciate chance speak
    speaking talk talking conversation further learn learning more bring bringing experience background skills
    qualifications fit
    """
)
_CLAIM_MARKERS = (validation._CREDENTIAL, validation._LEADERSHIP, validation._BUILD_VERBS, validation._INFLATION,
                  validation._DEGREE_WORDS, validation._GRADUATION, validation._PUFFERY, validation._LINK,
                  validation._NUMBER_WORDS, _PROFICIENCY)


def is_boilerplate(sentence: str, *, job_title: str | None, employer: str | None,
                   profile: CandidateProfile) -> bool:
    """Claim-free letter boilerplate ("Thank you for your time.") that the template wording replaces."""
    if validation.unexpected_characters(normalize_space(sentence)):
        return False  # full-width / look-alike / invisible characters: judged as a claim (and rejected)
    text = _GREETING_PREFIX.sub("", validation.claim_text(sentence))
    if not _BOILERPLATE_START.match(text):
        return False
    masked = validation._mask_phrases(text, [job_title or "", employer or "", profile.personal.full_name])
    if re.search(r"\d", masked) or lexicon.find_skills(masked, ignore_case=True):
        return False
    if any(rx.search(masked) for rx in _CLAIM_MARKERS) or validation.work_authorization_terms(masked):
        return False
    tokens = re.findall(r"[A-Za-z][A-Za-z'’-]*", masked)
    if any(t[:1].isupper() and t != "I" and not t.startswith(("I'", "I’")) for t in tokens[1:]):
        return False  # a name, organization or other proper noun
    return all(w in BOILERPLATE_VOCABULARY for w in content_words(masked))


def validate_llm_draft(paragraphs: Sequence[str], fact_ids_used: Sequence[str], allowed_facts: Sequence[Fact],
                       profile: CandidateProfile, kw: JobKeywords, *, job_title: str | None,
                       employer: str | None) -> tuple[list[str], list[Claim], list[str]]:
    """Validate an LLM cover-letter draft sentence by sentence.

    Returns ``(body paragraphs, claims, problems)``. Body paragraphs hold only
    the sentences that restate cited facts and pass ``validate_claim`` (with
    the verified entry context); boilerplate is dropped. Any problem means the
    draft must not be used. ``job_title`` / ``employer`` are slot values (or
    None); they are never a source for a claim.
    """
    problems: list[str] = []
    allowed = {f.id: f for f in allowed_facts}
    unknown = [fid for fid in fact_ids_used if fid not in allowed]
    if unknown:
        problems.append(f"draft cites fact ids that were not provided: {unknown}")
    cited = [allowed[fid] for fid in dict.fromkeys(fact_ids_used) if fid in allowed]
    if not cited:
        problems.append("draft cites no verified facts")
    paras = [normalize_space(p) for p in paragraphs if p and p.strip()]
    while paras and _GREETING_RE.match(paras[0]) and len(words(paras[0])) <= 6:
        paras.pop(0)
    while paras and (_SIGNOFF_RE.match(paras[-1]) or paras[-1] == profile.personal.full_name):
        paras.pop()
    if not 1 <= len(paras) <= 6:
        problems.append(f"expected 1-6 paragraphs, got {len(paras)}")
    total_words = sum(len(words(p)) for p in paras)
    if total_words > 450:
        problems.append(f"letter length {total_words} words is over 450")

    title, company = job_title or None, employer or None
    body: list[str] = []
    claims: list[Claim] = []
    for para in paras:
        kept: list[str] = []
        for raw_sentence in split_sentences(para):
            if is_boilerplate(raw_sentence, job_title=title, employer=company, profile=profile):
                continue
            # Judged (and kept) in normalized form; the raw form is checked for disguising characters.
            sentence = validation.claim_text(raw_sentence)
            mapped = [f for f in cited if _maps_to(sentence, f)]
            if not mapped:
                kind = "first-person statement" if _FIRST_PERSON.search(sentence) else "sentence"
                problems.append(f"'{sentence[:80]}': unsupported_claim: this {kind} restates no cited verified fact")
                continue
            issues = validation.validate_body_claim(raw_sentence, mapped, profile, employer=company,
                                                    listing_text=kw.text)
            source = "\n".join(validation.claim_text(f.text) for f in mapped)
            for m in sorted({m.group(0).lower() for m in _PROFICIENCY.finditer(sentence)}):
                if not mentions(source, m):
                    issues.append(Issue(code="unverified_proficiency",
                                        message=f"'{m}' claims a proficiency level no verified fact states"))
            if issues:
                problems.append(f"'{sentence[:80]}': " + "; ".join(validation.summarize(issues)))
                continue
            kept.append(sentence)
            claims.append(Claim(text=sentence, fact_ids=[f.id for f in mapped], generator="llm",
                                section="cover_letter:body"))
        if kept:
            body.append(" ".join(kept))
    if not claims:
        problems.append("the draft has no sentence that restates a cited verified fact")
    return body, claims, problems


def _maps_to(sentence: str, fact: Fact) -> bool:
    fact_text = validation.claim_text(fact.text)
    fact_words = content_set(fact_text)
    if not fact_words:
        return False
    shared = content_set(sentence) & fact_words
    if len(shared) / len(fact_words) >= 0.3:
        return True
    fact_numbers = {n[1] for n in validation._numbers(fact_text)}
    return bool(fact_numbers & {n[1] for n in validation._numbers(sentence)})


def build_cover_letter(profile: CandidateProfile, kw: JobKeywords, *, job_title: str, employer: str, on: date,
                       drafter: Any = None, model_label: str | None = None) -> GeneratedCoverLetter:
    """Template letter, or the template with a validated LLM body when ``drafter`` is given and its draft passes.

    ``drafter(employer=, job_title=, job_highlights=, facts=, candidate_name=)`` returns an object with
    ``.paragraphs`` and ``.fact_ids_used``, or ``None`` when the LLM failed (the wrapper records the error).
    """
    title = title_slot(job_title)
    company = employer_slot(employer)
    content, claims, issues = template_cover_letter(profile, kw, job_title=title, employer=company, on=on)
    if title is None and normalize_space(job_title or ""):
        issues.append(Issue(code="job_title_not_used", severity="review", document="cover_letter",
                            message="The listing's job title does not look like a plain job title (sentence "
                                    "punctuation, first- or third-person wording, claims such as counts of "
                                    "people or years, leadership verbs or legal-status words, unusual "
                                    f"characters, instructions or more than {MAX_SLOT_WORDS} words), so the "
                                    f"letter says '{TITLE_FALLBACK}'. Check the listing and edit the letter if "
                                    "needed."))
    if company is None and normalize_space(employer or ""):
        issues.append(Issue(code="employer_not_used", severity="review", document="cover_letter",
                            message="The listing's employer name does not look like a plain organization name "
                                    "(sentence punctuation, claims about a person, unusual characters or "
                                    f"instructions), so the letter says '{EMPLOYER_FALLBACK}'. Check the "
                                    "listing."))
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
        draft = drafter(employer=company or EMPLOYER_FALLBACK, job_title=title or TITLE_FALLBACK,
                        job_highlights=highlights, facts=facts, candidate_name=profile.personal.full_name)
        if draft is None:
            problems = ["the local LLM was unavailable or returned an error"]
            body, llm_claims = [], []
        else:
            body, llm_claims, problems = validate_llm_draft(
                [str(p) for p in (getattr(draft, "paragraphs", None) or [])],
                [str(f) for f in (getattr(draft, "fact_ids_used", None) or [])],
                facts, profile, kw, job_title=title, employer=company)
        if not problems:
            label = f"llm:{model_label}" if model_label else "llm"
            opening, fixed_tail = content.paragraphs[0], content.paragraphs[-2:]
            content = content.model_copy(update={"paragraphs": [opening, *body, *fixed_tail]})
            claims = [claims[0], *(c.model_copy(update={"generator": label}) for c in llm_claims)]
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
