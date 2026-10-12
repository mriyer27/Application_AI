"""Deterministic claim validation (the truthfulness guarantee).

``validate_claim`` checks one generated statement against the verified fact(s)
it is supposed to restate. Its rules are heuristics that err on the side of
rejecting: a rejected rewording simply falls back to the verbatim fact text.

Rules (each violation is an ``Issue`` with severity ``error`` unless noted):

* every number / percentage / amount must appear in the source fact(s), with the
  same unit (%, $, k/m/b, x, +); spelled-out quantities ("doubled", "dozens")
  must also appear in the source; hedges before a number ("approximately 30%")
  must be kept;
* no lexicon tool/skill/technology unless it is in the source fact (text or its
  ``skills``) -- or, with ``strict_skills=False``, in ``profile.skills``;
* no leadership / ownership verbs (led, managed, owned, spearheaded, directed,
  headed, supervised, architected, founded, ...) unless the source uses them;
* no organization / proper names that are not in the source (including other
  profile employers and the job's employer);
* no certifications / degrees that are not in the source or the profile;
* no work-authorization / visa wording, links, e-mail addresses or placeholders;
* at least ~50% of the content words must come from the source; length bounds.

``validate_resume`` / ``validate_cover_letter`` check whole documents:
traceability of every bullet/claim to fact ids, header fields equal to
``profile.personal``, verbatim education dates, skills subset of the profile,
and placeholder text (``<...>``) in used profile fields (=> needs review).
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

from job_agent.profile import CandidateProfile, Fact
from job_agent.resume import lexicon
from job_agent.resume.models import Claim, CoverLetterContent, Issue, ResumeContent
from job_agent.resume.text import content_set, content_words, mentions, normalize_space, split_sentences, words

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
_HEDGE = (r"(?:approximately|approx\.?|about|around|roughly|nearly|almost|up\s+to|over|more\s+than|less\s+than|"
          r"at\s+least|an\s+estimated|estimated|~)")
_HEDGED_NUM = re.compile(rf"(?<![A-Za-z]){_HEDGE}\s*(?:\$\s?)?(\d+(?:[.,]\d+)*)", re.IGNORECASE)

_LEADERSHIP = re.compile(
    r"(?<![A-Za-z])(led|lead|leads|leading|managed|manage|manages|managing|owned|own|owns|owning|ownership|"
    r"spearheaded|spearhead|spearheads|spearheading|directed|directing|headed|supervised|supervise|supervises|"
    r"supervising|supervisor|architected|architecting|founded|co-founded|cofounded|founder|co-founder|oversaw|"
    r"oversee|oversees|overseeing|orchestrated|championed|pioneered|mentored|chaired|steered|captained)"
    r"(?![A-Za-z])",
    re.IGNORECASE,
)
_CREDENTIAL = re.compile(
    r"(?<![A-Za-z])(certified|certification|certifications|certificate|certificates|licensed|licensure|"
    r"accredited|PMP|CPA|CFA|CISSP|CISA|CSM|CAPM|CBAP|ITIL|scrum\s+master|green\s+belt|black\s+belt|MBA|"
    r"Ph\.?\s?D\.?|doctorate|doctoral|master'?s|masters|master\s+of|bachelor'?s|bachelors|bachelor\s+of|"
    r"degree|diploma|B\.S\.|M\.S\.|B\.A\.|M\.A\.|BSc|MSc|B\.Tech|M\.Tech|BTech|MTech)(?![A-Za-z])",
    re.IGNORECASE,
)
_WORK_AUTH_CI = re.compile(
    r"(?<![A-Za-z])(visa|visas|sponsorship|sponsor\s+(?:a\s+)?(?:visa|work)|work\s+authori[sz]ation|"
    r"authori[sz]ed\s+to\s+work|employment\s+authori[sz]ation|work\s+permit|F-1|J-1|H-1B|H1-?B|STEM\s+OPT|"
    r"green\s+card|permanent\s+resident|U\.?S\.?\s+citizen(?:ship)?|citizenship|immigration|security\s+clearance|"
    r"export\s+control)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_WORK_AUTH_CS = re.compile(r"(?<![A-Za-z])(OPT|CPT|EAD)(?![A-Za-z])")
_LINK = re.compile(r"(https?://|www\.|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"<[^<>]+>")
_BRACKET_PLACEHOLDER = re.compile(r"\[[^\[\]]{2,60}\]")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9&+#'’.-]*")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _issue(code: str, message: str, severity: str = "error", location: str | None = None) -> Issue:
    return Issue(code=code, severity=severity, message=message, location=location)  # type: ignore[arg-type]


def _numbers(text: str) -> list[tuple[str, str, str]]:
    """``(currency, value, suffix)`` for every number in ``text`` (normalized)."""
    out = []
    for m in _NUM_RE.finditer(text):
        cur = "$" if (m.group("cur") or "").strip() in {"$", "USD"} else (m.group("cur") or "").strip()
        val = m.group("val").replace(",", "")
        if "." in val:
            val = val.rstrip("0").rstrip(".") or "0"
        suf = (m.group("suf") or "").strip().lower()
        if suf.startswith("per"):
            suf = "%"
        out.append((cur, val, suf))
    return out


def _number_label(n: tuple[str, str, str]) -> str:
    return f"{n[0]}{n[1]}{n[2]}"


def check_numbers(text: str, source_text: str, *, extra_sources: str = "") -> list[Issue]:
    issues: list[Issue] = []
    allowed = _numbers(source_text) + _numbers(extra_sources)
    for n in _numbers(text):
        cur, val, suf = n
        same_value = [a for a in allowed if a[1] == val]
        ok = bool(same_value) and (not suf or any(a[2] == suf for a in same_value)) and (
            not cur or any(a[0] == cur for a in same_value))
        if not ok:
            issues.append(_issue("number_not_in_source",
                                 f"number '{_number_label(n)}' does not appear in the verified source fact"))
    src_words = {w.lower() for w in _NUMBER_WORDS.findall(source_text + " " + extra_sources)}
    for w in {w.lower() for w in _NUMBER_WORDS.findall(text)}:
        if w not in src_words:
            issues.append(_issue("quantity_not_in_source",
                                 f"quantity word '{w}' does not appear in the verified source fact"))
    for m in _HEDGED_NUM.finditer(source_text):
        value = m.group(1)
        if re.search(rf"(?<![\d.,]){re.escape(value)}(?![\d])", text) and not re.search(
                rf"{_HEDGE}\s*(?:\$\s?)?{re.escape(value)}(?![\d])", text, re.IGNORECASE):
            issues.append(_issue("hedge_removed",
                                 f"the source qualifies {value} (e.g. 'approximately'); the qualifier must be kept"))
    return issues


def _skills_in(texts: Iterable[str]) -> set[str]:
    found: set[str] = set()
    for t in texts:
        found.update(lexicon.find_skills(t))
    return found


def check_skills(text: str, source_facts: Sequence[Fact], profile: CandidateProfile, *, strict: bool = True,
                 extra_allowed: Iterable[str] = ()) -> list[Issue]:
    source_allowed = _skills_in([f.text for f in source_facts] + [s for f in source_facts for s in f.skills])
    source_allowed |= _skills_in(extra_allowed)
    profile_allowed = _skills_in(profile.skills)
    issues = []
    for name, matched in lexicon.find_skill_spans(text):
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
        known = tok.lower() in allowed_tokens or all(
            not part or part.lower() in allowed_tokens or not part[:1].isupper() for part in tok.split("-"))
        if is_name and not known:
            acronym = len(tok) >= 2 and tok.isupper()
            next_is_name = i + 1 < len(tokens) and tokens[i + 1].group(0)[:1].isupper()
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


def check_forbidden_content(text: str, source_text: str = "") -> list[Issue]:
    issues: list[Issue] = []
    for m in {m.group(0) for m in _WORK_AUTH_CI.finditer(text)} | {m.group(0) for m in _WORK_AUTH_CS.finditer(text)}:
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


def overlap_ratio(text: str, source_text: str) -> float:
    cand = content_set(text)
    if not cand:
        return 0.0
    return len(cand & content_set(source_text)) / len(cand)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def validate_claim(text: str, source_facts: Sequence[Fact], profile: CandidateProfile, *,
                   strict_skills: bool = True, forbidden_names: Iterable[str] = (),
                   min_overlap: float = MIN_OVERLAP) -> list[Issue]:
    """Validate one generated statement against the verified fact(s) it restates.

    Returns an empty list when the statement is acceptable. ``forbidden_names``
    are extra organization names (e.g. the job's employer) that may only appear
    if the source fact contains them.
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
    n_words = len(words(candidate))
    src_words = len(words(source_text))
    if n_words < MIN_WORDS:
        issues.append(_issue("too_short", f"only {n_words} words"))
    if n_words > max(40, int(src_words * 1.6)) or len(candidate) > MAX_CHARS:
        issues.append(_issue("too_long", f"{n_words} words / {len(candidate)} characters is too long"))
    issues += check_numbers(candidate, source_text)
    issues += check_skills(candidate, source_facts, profile, strict=strict_skills)
    issues += check_leadership(candidate, source_text)
    issues += check_credentials(candidate, source_text, profile)
    skill_text = "\n".join(s for f in source_facts for s in f.skills)
    issues += check_organizations(candidate, source_text, profile, forbidden_names=forbidden_names,
                                  allowed_text=skill_text)
    issues += check_forbidden_content(candidate, source_text)
    ratio = overlap_ratio(candidate, source_text + "\n" + skill_text)
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
    from job_agent.resume.selection import education_dates

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
        if entry.dates != education_dates(edu) or (edu.end and edu.end not in entry.dates):
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


def check_company_claims(sentence: str, *, employer: str, listing_text: str, profile_text: str) -> list[Issue]:
    """A sentence about the employer may only use information from the listing."""
    issues: list[Issue] = []
    refers = bool(_COMPANY_REF.search(sentence)) or (employer and mentions(sentence, employer))
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


def validate_cover_letter(content: CoverLetterContent, claims: Sequence[Claim], profile: CandidateProfile, *,
                          listing_text: str, profile_label: str = "your profile") -> list[Issue]:
    """Document-level checks for a cover letter (template or validated LLM draft)."""
    issues: list[Issue] = _header_issues(content.header, profile)
    if content.signature != profile.personal.full_name:
        issues.append(_issue("signature_mismatch", "signature must be the profile name"))
    known_ids = {f.id for f in profile.all_facts()}
    for claim in claims:
        if claim.fact_ids and not set(claim.fact_ids) <= known_ids:
            issues.append(_issue("unknown_fact_id", f"claim cites unknown fact ids {claim.fact_ids}"))
    cited = [f for f in profile.all_facts() if any(f.id in c.fact_ids for c in claims)]
    cited_text = "\n".join(f.text for f in cited)
    trusted_numbers = "\n".join([cited_text, content.job_title, content.employer, content.date_text,
                                 *(f"{e.start or ''} {e.end or ''} {e.gpa or ''}" for e in profile.education)])
    body = "\n".join(content.paragraphs)
    issues += check_numbers(body, trusted_numbers)
    issues += check_skills(body, cited, profile, strict=False, extra_allowed=[content.job_title, content.employer])
    issues += check_leadership(body, cited_text + "\n" + content.job_title)
    issues += check_credentials(body, cited_text, profile)
    profile_text = "\n".join([*organizations(profile), *profile.skills, cited_text,
                              *profile.preferences.preferred_themes, credential_text(profile)])
    for sentence in split_sentences(body):
        issues += check_company_claims(sentence, employer=content.employer, listing_text=listing_text,
                                       profile_text=profile_text)
    text = content.plain_text()
    contact = "\n".join(content.header.contact_parts())
    issues += [i for i in check_forbidden_content(text, contact) if i.code != "placeholder_text"]
    issues += placeholder_issues(profile, text, profile_label=profile_label, document="cover_letter")
    for i in issues:
        i.document = "cover_letter"
    return issues
