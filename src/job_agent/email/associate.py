"""Link emails to the jobs / applications they are about, with a confidence 0..1.

A link reaches the auto-update threshold (``AUTO_THRESHOLD``) only from a
*verified* source **and** with the application's role named in the email:

  * domain   -- the sender's registrable domain is exactly the domain of the
                job / application URL (``acme.com`` for ``careers.acme.com``)
  * confirmed_domain -- you linked mail from this domain to this employer before
  * platform -- an ATS (Greenhouse, Lever, Workday, ...) the job was posted on
                (``boards.greenhouse.io/acme`` -> mail from ``greenhouse-mail.io``),
                naming the employer's full name, from no other account on it
  * confirmed_platform -- an ATS account (``acme@myworkday.com``,
                ``acme.icims.com``) you linked to this employer before
  * thread   -- an earlier message in the same Gmail thread is already linked
                *and* was sent from the same registrable domain (no role needed)

The role check: the email must name this application's job title ("the
Business Analyst position", "Business Analyst - next steps"). An email naming no
role, another role, or several roles is capped below the threshold.

Anything that merely *resembles* the employer stays below the threshold: a
domain spelled like the employer's name on some TLD (``acme-analytics.xyz``),
a prefix (``better.com`` for Betterment), the name inside a longer company name
("Mercury Insurance" or "Blue Mercury" for Mercury), or a display name / subject
on an ATS the job was not posted on (anyone with an account there can type any
company name). Those links are stored as suggestions for you to confirm.

The LLM's ``employer_guess`` is untrusted and only used as a weak hint: it can
never raise a candidate to the auto-update threshold by itself.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

from job_agent.database import Database
from job_agent.email import repository as email_repo
from job_agent.email.classify import ATS_DOMAINS, is_ats_sender, is_generic_sender, is_platform_sender
from job_agent.email.parse import domain_core, registrable_domain
from job_agent.security import textnorm

#: Association confidence required for automatic, evidence-based status updates.
AUTO_THRESHOLD = 0.8
#: Candidates below this are not stored at all.
MIN_STORE_CONFIDENCE = 0.5
#: A runner-up within this margin of the best candidate makes the match ambiguous.
AMBIGUITY_MARGIN = 0.05
#: Upper bound for a link that must be confirmed by you (wrong role, several applications, ambiguity).
REVIEW_CAP = 0.6

#: Domain spelled exactly like the employer's name, but not known to be theirs.
NAME_DOMAIN_SCORE = 0.65
#: Domain that only resembles the employer's name (prefix / first word).
SIMILAR_DOMAIN_SCORE = 0.5
#: Hard ceiling for any link not backed by a verified source, and for any link whose email does not
#: name the application's role. Anyone can write an employer's name in a subject.
UNVERIFIED_CAP = 0.7

#: Methods backed by something you or the job posting established (see the module docstring).
VERIFIED_METHODS: frozenset[str] = frozenset({"domain", "confirmed_domain", "platform", "confirmed_platform"})

#: Hosts where anyone can publish a job page or form: their domain says nothing about the employer.
_SHARED_HOSTS: frozenset[str] = frozenset({
    "google.com", "forms.gle", "office.com", "microsoft.com", "live.com", "typeform.com", "jotform.com",
    "wufoo.com", "airtable.com", "notion.site", "notion.so", "github.io", "gitlab.io", "webflow.io", "wixsite.com",
    "squarespace.com", "wordpress.com", "medium.com", "craigslist.org", "ycombinator.com", "bit.ly",
})

_LEGAL_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "plc", "gmbh", "ag",
    "sa", "lp", "llp", "pllc", "holdings", "group", "the",
}
#: Suffixes that may follow an employer name without making it a different company.
_LEGAL_FOLLOWERS = _LEGAL_SUFFIXES - {"holdings", "group", "the"}
#: Capitalized words that commonly follow an employer name without extending it
#: ("Acme Careers", "Acme Hiring Team", Title-Case subjects like "Acme Was ...").
_FOLLOWER_OK = frozenset({
    "careers", "career", "recruiting", "recruitment", "recruiter", "recruiters", "talent", "team", "teams", "hiring",
    "hr", "human", "resources", "people", "staffing", "workforce", "acquisition", "university", "campus", "jobs",
    "job", "notifications", "notification", "noreply", "no", "do", "not", "reply", "applicant", "applicants",
    "application", "applications", "candidate", "candidates", "interview", "interviews", "offer", "offers",
    "update", "updates", "status", "position", "positions", "role", "roles", "opportunity", "opportunities",
    "opening", "openings", "scheduling", "assessment", "onboarding", "support", "is", "was", "has", "have", "had",
    "will", "would", "can", "could", "may", "and", "or", "but", "the", "a", "an", "for", "to", "at", "in", "on",
    "of", "by", "with", "from", "via", "we", "our", "you", "your", "us", "i", "thank", "thanks", "dear", "hi",
    "hello", "regarding", "re", "fwd", "next", "steps", "step",
})
#: Capitalized words that commonly come right *before* an employer name without extending it
#: ("Thank You For Applying To Mercury", "Welcome To Mercury", "Join Mercury").
_PRECEDER_OK = _FOLLOWER_OK | frozenset({
    "join", "joining", "joined", "welcome", "about", "regards", "sincerely", "best", "cheers", "invitation",
    "confirmation", "received", "submitted", "reminder", "message", "letter", "news", "here", "all", "everyone",
    "apply", "applying", "applied", "considering", "choosing", "chose", "congratulations", "into", "within",
    "as", "like", "fw", "aw", "wg", "important", "action", "required", "feedback", "decision",
})
#: Small words that join two names into one ("Bank of America", "Procter & Gamble" -> "Procter and Gamble").
_NAME_CONNECTORS = frozenset({"and", "of", "de", "du", "la", "le", "y", "e", "van", "von", "der", "den"})
#: Labels / local-part / URL words that say nothing about which company a platform account belongs to.
_PLATFORM_GENERIC = frozenset({
    "us", "eu", "uk", "ca", "au", "in", "mail", "email", "emails", "e", "em", "m", "mg", "notifications",
    "notification", "notify", "no", "reply", "noreply", "donotreply", "do", "not", "jobs", "job", "careers",
    "career", "talent", "hire", "hiring", "recruiting", "recruitment", "recruiter", "apply", "candidates",
    "candidate", "app", "apps", "www", "messages", "message", "msg", "system", "alerts", "alert", "info", "hr",
    "team", "support", "bounce", "bounces", "mailer", "sender", "outbound", "send", "scheduling", "schedule",
    "calendar", "invite", "invites", "assessment", "assessments", "test", "tests", "interview", "interviews",
    "boards", "board", "embed", "external", "portal", "en", "sites", "site", "jobboards",
})
#: Generic words stripped from the start / end of an account name ("acmecareers" -> "acme").
_TENANT_AFFIXES = tuple(sorted((w for w in _PLATFORM_GENERIC if len(w) >= 4), key=len, reverse=True))
#: Sending and job-board domains of the same platform ("boards.greenhouse.io" / "greenhouse-mail.io").
_PLATFORM_FAMILY: dict[str, str] = {
    "greenhouse.io": "greenhouse", "greenhouse-mail.io": "greenhouse", "lever.co": "lever",
    "ashbyhq.com": "ashby", "ashbyhq.io": "ashby", "myworkday.com": "workday", "myworkdayjobs.com": "workday",
    "workday.com": "workday", "workablemail.com": "workable", "workable.com": "workable",
    "jazzhr.com": "jazzhr", "applytojob.com": "jazzhr", "successfactors.com": "successfactors",
    "successfactors.eu": "successfactors",
}

_TITLE_WORD = r"(?:Sr|Jr|St)\.|[A-Z0-9][A-Za-z0-9+#']*"
_TITLE = rf"(?:{_TITLE_WORD})(?:(?:[ \t]+(?:of|and|&)[ \t]+|[ \t]*[-\u2013/&][ \t]*|[ \t]+)(?:{_TITLE_WORD})){{0,7}}"
_TITLE_NOUN = (r"(?P<n>(?i:position|role|opening|opportunity|job|vacancy|posting|req|requisition|application|"
               r"candidacy))")
#: Nouns after which the captured words are a role for sure ("the X position"); after the others
#: ("your X application", "application for X") the words must end like a role ("... Analyst").
_ROLE_NOUNS_AFTER = frozenset({"position", "role", "opening", "opportunity", "job", "vacancy", "posting"})
_TITLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "the Business Analyst position", "your Senior Data Engineer application"
    re.compile(rf"(?i:\b(?:the|our|your|this|that|its|an?)\s+)(?P<t>{_TITLE})[ \t]+{_TITLE_NOUN}\b"),
    # "application for the Business Analyst position at Acme", "applying for Business Analyst."
    re.compile(rf"(?i:\b(?:application|applying|applied|candidacy)\s+for\s+(?:(?:the|our|an?)\s+)?)"
               rf"(?P<t>{_TITLE})(?=[ \t]+(?i:at|with|here)\b|[ \t]*[.,;:!?()\n]|[ \t]*$|[ \t]+[-\u2013][ \t])"),
    # "Position: Business Analyst", "Job title - Data Analyst"
    re.compile(rf"(?i:\b(?:position|role|job title|title)[ \t]*[:\u2013-][ \t]*)(?P<t>{_TITLE})"),
    # "joining Acme as a Senior Data Engineer", "hired as an Analyst"
    re.compile(rf"(?i:\bas\s+an?\s+)(?P<t>{_TITLE})"),
    # "the position of Business Analyst", "the role of Data Engineer"
    re.compile(rf"(?i:\b(?:position|role|post)\s+of\s+(?:(?:an?|the)\s+)?)(?P<t>{_TITLE})"),
)
#: Subject segments ("Senior Data Engineer (Req 7781) - next steps", "Re: Req 7781 / Data Engineer").
_SUBJECT_PREFIX = re.compile(r"^\s*(?:(?:re|fwd?|fw|aw|wg)\s*:\s*)+", re.I)
_SUBJECT_SPLIT = re.compile(r"\s+[-\u2013\u2014|/:]\s+|\s*[|()\[\]]\s*|:\s+|,\s+")
_TITLE_SEGMENT = re.compile(rf"(?:{_TITLE})")
#: Last words that make a captured phrase look like a role.
_ROLE_WORDS = frozenset({
    "analyst", "engineer", "developer", "scientist", "manager", "coordinator", "specialist", "associate",
    "assistant", "intern", "internship", "director", "consultant", "designer", "architect", "administrator",
    "officer", "representative", "rep", "lead", "technician", "accountant", "advisor", "adviser", "planner",
    "strategist", "researcher", "writer", "editor", "recruiter", "programmer", "tester", "auditor", "controller",
    "executive", "supervisor", "operator", "agent", "clerk", "nurse", "teacher", "instructor", "tutor", "fellow",
    "trainee", "apprentice", "owner", "head", "president", "vp", "cashier", "attorney", "paralegal", "counsel",
    "economist", "statistician", "actuary", "underwriter", "banker", "trader", "buyer", "marketer", "generalist",
    "producer", "mechanic", "electrician", "therapist", "pharmacist", "physician", "modeler", "modeller",
    "sre", "dba", "partner", "steward", "chef", "driver", "sales", "support", "operations", "ops",
})
#: Product / platform names that appear as "your Workday application".
_PLATFORM_NAMES = frozenset({"workday", "greenhouse", "lever", "icims", "taleo", "smartrecruiters", "jobvite",
                             "ashby", "bamboohr", "workable", "linkedin", "indeed", "handshake", "hirevue",
                             "hackerrank", "codility", "codesignal"})
#: Title Case words where a captured title really starts after ("Interest In The Business Analyst Role").
_TITLE_BOUNDARY = frozenset({"the", "your", "our", "a", "an", "in", "for", "to", "at", "with", "on", "this", "that",
                             "its", "about", "regarding"})
#: Words that are not a role ("this Exciting opportunity", "the Same position").
_NOT_A_TITLE = frozenset({"exciting", "new", "open", "same", "above", "following", "next", "current", "great",
                          "amazing", "this", "that", "job", "application", "career", "online", "interview", "your",
                          "our", "the", "a", "an", "other", "another", "available", "full", "time", "remote"})
#: Tokens ignored when comparing titles ("Data Analyst II", "Business Analyst (Remote)").
_TITLE_QUALIFIERS = frozenset({"i", "ii", "iii", "iv", "1", "2", "3", "4", "remote", "hybrid", "onsite", "on", "site",
                               "us", "usa", "full", "part", "time", "fulltime", "parttime", "contract", "temporary",
                               "temp", "permanent", "req", "requisition"})
#: Words that turn a mentioned title into a different, more specific one.
_TITLE_PREFIX_MODIFIERS = frozenset({"senior", "sr", "junior", "jr", "lead", "principal", "staff", "associate",
                                     "assistant", "head", "chief", "deputy", "executive", "intern", "vp"})
_TITLE_SUFFIX_MODIFIERS = frozenset({"manager", "lead", "intern", "internship", "director", "supervisor",
                                     "architect", "head"})


def _ascii_fold(text: str | None) -> str:
    """Shared Unicode normalization (look-alike letters, invisible characters), then ASCII."""
    text = unicodedata.normalize("NFKD", textnorm.normalize_text(text or ""))
    return text.encode("ascii", "ignore").decode()


def normalize_employer(name: str | None) -> str:
    """``"The Acme Analytics, Inc."`` -> ``"acme analytics"``."""
    text = _ascii_fold(name).lower().replace("&", " and ")
    tokens = [t for t in re.split(r"[^a-z0-9]+", text) if t]
    while tokens and tokens[0] == "the":
        tokens.pop(0)
    while len(tokens) > 1 and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def normalize_text(text: str | None) -> str:
    text = _ascii_fold(text).lower().replace("&", " and ")
    return " ".join(t for t in re.split(r"[^a-z0-9]+", text) if t)


def _fold(text: str | None) -> str:
    """ASCII text that keeps letter case (capitalization matters for name boundaries).

    Look-alike letters become the Latin letters they imitate ("Вlue" -> "Blue");
    any other non-Latin letter becomes ``X``, so it still reads as a word next to
    a name instead of silently disappearing.
    """
    out: list[str] = []
    for ch in unicodedata.normalize("NFKD", textnorm.normalize_text(text or "")):
        if ch.isascii():
            out.append(ch)
        elif unicodedata.category(ch).startswith("M"):
            continue  # combining accent: "Zürich" -> "Zurich"
        elif ch.isalnum():
            out.append("X")
        else:
            out.append(" ")
    return "".join(out).replace("&", " and ")


def _host_domain(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return None
    return registrable_domain(host) if host else None


# --------------------------------------------------------------------------- #
# Employer names
# --------------------------------------------------------------------------- #
_WORDS_BEFORE = re.compile(r"(?:([A-Za-z][A-Za-z0-9]*)[ \t]+)?([A-Za-z][A-Za-z0-9]*)(?:[ \t]+|-)$")
_WORDS_AFTER = re.compile(r"(?:[ \t]+|-)([A-Za-z][A-Za-z0-9]*)(?:[ \t]+([A-Za-z][A-Za-z0-9]*))?")


def _foreign_word(word: str | None, allowed: frozenset[str] | set[str]) -> bool:
    """A capitalized word that is not a known neighbour of a company name."""
    return bool(word) and word[0].isupper() and word.lower() not in allowed  # type: ignore[index]


def _mention_kind(hay: str, start: int, end: int, *, before_ok: set[str], after_ok: set[str],
                  title_norm: str) -> str:
    """``"named"`` or ``"other"`` (part of a longer company name) for one match of the employer's name."""
    before = hay[max(0, start - 80):start]
    words = _WORDS_BEFORE.search(before)
    title_before = bool(title_norm) and normalize_text(before).endswith(title_norm)
    if words and not title_before:
        w2, w1 = words.group(1), words.group(2)
        if _foreign_word(w1, before_ok):
            return "other"  # "Blue Mercury", "American Express"
        if w1.lower() in _NAME_CONNECTORS and _foreign_word(w2, before_ok):
            return "other"  # "Bank of America", "Procter and Gamble"
    rest = hay[end:]
    after = _WORDS_AFTER.match(rest)
    if after is None:
        return "named"  # end of text, punctuation, possessive, line break
    w1, w2 = after.group(1), after.group(2)
    if w1.lower() in _NAME_CONNECTORS and _foreign_word(w2, after_ok):
        return "other"  # "Ernst and Young" for Ernst
    if not _foreign_word(w1, after_ok):
        return "named"  # "Mercury Careers", "Acme Analytics Inc", "Mercury is ..."
    if title_norm and normalize_text(rest[:len(title_norm) + 20]).startswith(title_norm):
        return "named"  # "Acme Analytics Business Analyst - application received"
    return "other"  # "Mercury Insurance": a longer company name


def employer_mention(text: str | None, employer: str | None, *, title: str | None = None) -> str:
    """How ``text`` refers to the employer: ``"named"``, ``"other"`` or ``"none"``.

    ``"other"`` means the name only appears inside a longer company name, before
    *or* after it ("Mercury Insurance", "Blue Mercury" for Mercury). Once a text
    names the longer company, its other bare mentions ("the Mercury team") are
    taken to mean that company too.
    """
    tokens = normalize_employer(employer).split()
    if not tokens or len("".join(tokens)) < 3:
        return "none"
    hay = _fold(text)
    if not hay:
        return "none"
    own = set(normalize_text(employer).split())
    after_ok = set(_FOLLOWER_OK | _LEGAL_FOLLOWERS) | own
    before_ok = set(_PRECEDER_OK) | own
    title_norm = normalize_text(title)
    pattern = re.compile(r"(?<![A-Za-z0-9])" + r"[^A-Za-z0-9\n]{1,3}".join(re.escape(t) for t in tokens)
                         + r"(?![A-Za-z0-9])", re.I)
    kinds = {_mention_kind(hay, m.start(), m.end(), before_ok=before_ok, after_ok=after_ok, title_norm=title_norm)
             for m in pattern.finditer(hay)}
    if "other" in kinds:
        return "other"
    return "named" if kinds else "none"


def names_employer(text: str | None, employer: str | None, *, title: str | None = None) -> bool:
    """Does ``text`` name this employer -- the full name, not part of a longer company name?

    "Mercury" is *not* named by "Mercury Insurance" or "Blue Mercury" (another
    capitalized word follows or precedes it), but is by "Mercury Careers",
    "Thank you for applying to Mercury", "Mercury's team" or "Mercury." .
    """
    return employer_mention(text, employer, title=title) == "named"


# --------------------------------------------------------------------------- #
# ATS / platform accounts
# --------------------------------------------------------------------------- #
def platform_family(domain: str | None) -> str | None:
    """``"greenhouse"`` for both ``boards.greenhouse.io`` and ``us.greenhouse-mail.io``."""
    reg = registrable_domain(domain)
    if not reg:
        return None
    return _PLATFORM_FAMILY.get(reg, reg)


def _tenant_key(part: str | None) -> str:
    """Company-specific part of a subdomain label, local part or URL segment ("careers-acme" -> "acme")."""
    words = [w for w in re.split(r"[^a-z0-9]+", (part or "").lower())
             if w and w not in _PLATFORM_GENERIC and not w.isdigit() and not re.fullmatch(r"wd\d+", w)]
    key = "".join(words)
    changed = True
    while changed:
        changed = False
        for affix in _TENANT_AFFIXES:
            if key.startswith(affix) and len(key) - len(affix) >= 3:
                key, changed = key[len(affix):], True
            elif key.endswith(affix) and len(key) - len(affix) >= 3:
                key, changed = key[:-len(affix)], True
    return key if len(key) >= 2 else ""


def sender_tenants(from_domain: str | None, local_part: str | None) -> set[str]:
    """Account names a platform sender carries (``acme@myworkday.com``, ``acme.icims.com`` -> ``{"acme"}``)."""
    domain = (from_domain or "").lower().strip(".")
    reg = registrable_domain(domain) or ""
    sub = domain[: -len(reg)].strip(".") if reg and domain.endswith(reg) else ""
    names = {_tenant_key(p) for p in [*sub.split("."), local_part or ""]}
    names.discard("")
    return names


def url_tenant(url: str | None) -> tuple[str, str | None] | None:
    """``(platform family, account)`` of a job URL hosted on an ATS, else ``None``.

    ``boards.greenhouse.io/acme/jobs/1`` -> ``("greenhouse", "acme")``,
    ``acme.wd5.myworkdayjobs.com/...`` -> ``("workday", "acme")``.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return None
    if not host or not is_ats_sender(host):
        return None
    family = platform_family(host) or host
    reg = registrable_domain(host) or ""
    sub = host[: -len(reg)].strip(".") if host.endswith(reg) else ""
    for label in sub.split("."):
        key = _tenant_key(label)
        if key:
            return family, key
    segments = [s for s in parsed.path.split("/") if s]
    if segments and segments[0].lower() == "embed":
        key = _tenant_key((parse_qs(parsed.query).get("for") or [""])[0])
    else:
        key = _tenant_key(segments[0]) if segments else ""
    return family, key or None


def _job_platforms(target: dict[str, Any]) -> dict[str, set[str]]:
    """Platform family -> account names, for the job / application URLs hosted on an ATS."""
    out: dict[str, set[str]] = {}
    for url in (target.get("job_url"), target.get("application_url")):
        found = url_tenant(url)
        if found:
            family, key = found
            out.setdefault(family, set())
            if key:
                out[family].add(key)
    return out


# --------------------------------------------------------------------------- #
# Job titles
# --------------------------------------------------------------------------- #
def _clean_title(words: list[str], *, needs_role_word: bool) -> str | None:
    cut = max((i for i, w in enumerate(words) if w.lower() in _TITLE_BOUNDARY), default=-1)
    words = words[cut + 1:]
    while words and words[0].lower() in {"application", "applying", "interest", "job", "new"}:
        words.pop(0)
    if needs_role_word:
        # "Senior Data Engineer In Our Team" -> "Senior Data Engineer"
        last = max((i for i, w in enumerate(words) if normalize_text(w) in _ROLE_WORDS), default=-1)
        if last < 0:
            return None
        words = words[:last + 1]
    title = " ".join(words).strip(" -/&")
    norm = normalize_text(title)
    if not norm or all(t in _NOT_A_TITLE or t in _PLATFORM_NAMES for t in norm.split()):
        return None
    return title


def mentioned_titles(subject: str | None, body: str | None) -> list[str]:
    """Role names an email refers to ("the Senior Data Engineer role" -> "Senior Data Engineer")."""
    found: list[str] = []

    def add(title: str | None) -> None:
        if title and title not in found:
            found.append(title)

    subj = _fold(subject)
    for text in (subj, _fold(body)):
        if not text:
            continue
        for pattern in _TITLE_PATTERNS:
            for match in pattern.finditer(text):
                noun = (match.groupdict().get("n") or "").lower()
                add(_clean_title(match.group("t").split(), needs_role_word=noun not in _ROLE_NOUNS_AFTER))
    # A subject segment that is nothing but a role: "Senior Data Engineer (Req 7781) - next steps".
    for segment in _SUBJECT_SPLIT.split(_SUBJECT_PREFIX.sub("", subj)):
        segment = segment.strip(" -/&,.;!?")
        words = segment.split()
        if (segment and _TITLE_SEGMENT.fullmatch(segment) and words
                and normalize_text(words[-1]) in _ROLE_WORDS):
            add(_clean_title(words, needs_role_word=True))
    return found


def _title_key(title: str | None) -> tuple[str, ...]:
    return tuple(t for t in normalize_text(title).split() if t not in _TITLE_QUALIFIERS)


def _strip_employer(key: tuple[str, ...], employer: str | None) -> tuple[str, ...]:
    """The title without a leading employer name ("Acme Business Analyst"); empty if it *is* the employer."""
    emp = tuple(normalize_employer(employer).split())
    if emp and key[:len(emp)] == emp:
        key = key[len(emp):]
    while key and key[0] in _LEGAL_FOLLOWERS:
        key = key[1:]
    if emp and key and set(key) <= set(emp) | _LEGAL_FOLLOWERS:
        return ()  # "your Acme application" for Acme Analytics
    return key


def titles_match(mentioned: str, target_title: str | None, *, employer: str | None = None) -> bool:
    """Same role, ignoring case, punctuation, levels like "II" and location qualifiers."""
    m = _strip_employer(_title_key(mentioned), employer)
    if not m:
        return False
    full = _title_key(target_title)
    base = _title_key(re.split(r"\s+[-\u2013|]\s+|\s*\(|,|\s+@\s+", target_title or "", maxsplit=1)[0])
    return m in {full, base}


def _direct_title_mention(text: str, title_norm: str) -> bool:
    """The application's title appears as a phrase that is not part of a more specific title."""
    for match in re.finditer(rf"(?<![a-z0-9]){re.escape(title_norm)}(?![a-z0-9])", text):
        before = text[:match.start()].split()
        after = text[match.end():].split()
        if before and before[-1] in _TITLE_PREFIX_MODIFIERS:
            continue
        if after and after[0] in _TITLE_SUFFIX_MODIFIERS:
            continue
        return True
    return False


def title_status(target_title: str | None, employer: str | None, mentioned: list[str], *, subject: str,
                 body: str) -> tuple[str, list[str]]:
    """``("match"|"mismatch"|"mixed"|"none", other titles named)`` for one application's title.

    ``mixed``: the email names this role *and* another one, so it is unclear which one it is about.
    """
    usable = [m for m in mentioned if _strip_employer(_title_key(m), employer)]
    others = [m for m in usable if not titles_match(m, target_title, employer=employer)]
    matched = len(others) < len(usable)
    if not matched:
        title_norm = normalize_text(target_title)
        base_norm = normalize_text(re.split(r"\s+[-\u2013|]\s+|\s*\(|,|\s+@\s+", target_title or "", maxsplit=1)[0])
        for norm in dict.fromkeys(n for n in (title_norm, base_norm) if len(n) >= 4):
            if _direct_title_mention(normalize_text(subject), norm) or _direct_title_mention(normalize_text(body), norm):
                matched = True
                break
    if matched and others:
        return "mixed", others
    if matched:
        return "match", []
    if others:
        return "mismatch", others
    return "none", []


# --------------------------------------------------------------------------- #
# Candidates
# --------------------------------------------------------------------------- #
@dataclass
class AssociationCandidate:
    job_id: int
    application_id: int | None
    method: str
    confidence: float
    reasons: list[str] = field(default_factory=list)
    employer: str = ""
    title: str = ""
    application_status: str | None = None
    #: Does the email name this application's role? "match" | "mismatch" | "mixed" | "none"
    title_status: str = "none"


def _employer_score(target: dict[str, Any], *, from_domain: str | None, from_name: str | None, local_part: str,
                    subject: str, body: str, confirmed_domains: dict[str, set[str]] | None = None,
                    confirmed_tenants: dict[str, dict[str, set[str]]] | None = None) -> tuple[float, str, list[str]]:
    employer_raw = target["employer"] or ""
    employer = normalize_employer(employer_raw)
    compact = employer.replace(" ", "")
    if not compact:
        return 0.0, "", []
    title = target.get("title") or ""
    sender_reg = registrable_domain(from_domain)
    platform = is_platform_sender(from_domain)
    best: tuple[float, str, list[str]] = (0.0, "", [])

    def consider(score: float, method: str, reason: str) -> None:
        nonlocal best
        if score > best[0]:
            best = (score, method, [reason])

    # 1. The sender's own domain (never an ATS / free-mail / job-board domain).
    if sender_reg and not platform and not is_generic_sender(from_domain):
        job_domains = {d for d in (_host_domain(target.get("job_url")), _host_domain(target.get("application_url")))
                       if d and not is_platform_sender(d) and not is_generic_sender(d) and d not in _SHARED_HOSTS
                       and not any(d == a or d.endswith("." + a) for a in ATS_DOMAINS)}
        if sender_reg in job_domains:
            return 0.9, "domain", [f"sender domain {sender_reg} is the job posting's website"]
        if sender_reg in (confirmed_domains or {}).get(employer, set()):
            return 0.9, "confirmed_domain", [f"you linked mail from {sender_reg} to {employer_raw} before"]
        core = domain_core(from_domain)
        first = employer.split(" ")[0]
        if core and core == compact:
            consider(NAME_DOMAIN_SCORE, "domain_name",
                     f"sender domain {sender_reg} is spelled like the employer's name but is not known to be "
                     "theirs (confirm it once to trust it)")
        elif core and len(core) >= 4 and (compact.startswith(core) or (len(first) >= 4 and core == first)):
            consider(SIMILAR_DOMAIN_SCORE, "domain_similar",
                     f"sender domain {sender_reg} only resembles the employer's name")

    # 2. The employer's full name in the subject / sender name / body.
    header_kinds = {employer_mention(subject, employer_raw, title=title),
                    employer_mention(from_name, employer_raw, title=title)}
    in_header = "named" in header_kinds and "other" not in header_kinds
    body_kind = employer_mention(body, employer_raw, title=title)
    in_body = body_kind == "named"
    if platform:
        family = platform_family(from_domain) or sender_reg or ""
        names = sender_tenants(from_domain, local_part)
        job_platforms = _job_platforms(target)
        accounts = {compact} | job_platforms.get(family, set())
        if "other" in header_kinds or any(n not in accounts and (n.startswith(compact) or n.endswith(compact))
                                          for n in names):
            return 0.0, "", []  # "Blue Mercury" / mercuryinsurance.icims.com writing about another company
        named = in_header or in_body
        confirmed = names & (confirmed_tenants or {}).get(employer, {}).get(family, set())
        same_platform = family in job_platforms
        other_account = (same_platform and bool(names) and bool(job_platforms[family])
                         and not names & accounts)
        if confirmed and named:
            consider(0.9, "confirmed_platform",
                     f"you linked mail from the {family} account '{sorted(confirmed)[0]}' to {employer_raw} before")
        elif same_platform and named and not other_account:
            consider(0.85 if in_header else 0.75, "platform",
                     f"the job was posted on {family}, and this {sender_reg} message names the employer")
        elif other_account:
            consider(REVIEW_CAP, "employer_name",
                     f"sent from the {family} account '{sorted(names)[0]}', not the one the job was posted by "
                     f"('{sorted(job_platforms[family])[0]}')")
        elif named or names & {compact}:
            consider(UNVERIFIED_CAP if in_header else REVIEW_CAP, "employer_name",
                     f"{sender_reg} message naming the employer, but the job was not posted on {family}; anyone "
                     "with an account there can use any company name")
    elif in_header:
        consider(0.7, "employer_name", "employer named in subject/sender")
    elif in_body:
        consider(0.6, "employer_name", "employer named in the body")
    return best


def _confirmed_senders(db: Database) -> tuple[dict[str, set[str]], dict[str, dict[str, set[str]]]]:
    """Senders you linked to an employer yourself.

    Returns ``(employer -> registrable domains, employer -> platform family -> account names)``. A platform
    sender without a company-specific account (``no-reply@us.greenhouse-mail.io``) is never remembered.
    """
    domains: dict[str, set[str]] = {}
    tenants: dict[str, dict[str, set[str]]] = {}
    for row in email_repo.confirmed_sender_domains(db):
        domain = row["from_domain"]
        employer = normalize_employer(row["employer"])
        if not domain or not employer or is_generic_sender(domain):
            continue
        if is_platform_sender(domain):
            local = (row["from_address"] or "").split("@", 1)[0]
            names = sender_tenants(domain, local)
            if names:
                tenants.setdefault(employer, {}).setdefault(platform_family(domain) or "", set()).update(names)
            continue
        reg = registrable_domain(domain)
        if reg and reg not in _SHARED_HOSTS:
            domains.setdefault(employer, set()).add(reg)
    return domains, tenants


def _apply_title(c: AssociationCandidate, status: str, others: list[str]) -> None:
    c.title_status = status
    if status == "match" and len(normalize_text(c.title)) >= 6 and c.method != "thread":
        c.confidence = min(0.97, c.confidence + 0.07)
        c.reasons.append("job title mentioned")
    elif status == "mismatch":
        c.confidence = min(c.confidence, REVIEW_CAP)
        c.reasons.append("the email names a different role: " + ", ".join(others[:3]))
    elif status == "mixed":
        c.confidence = min(c.confidence, REVIEW_CAP)
        c.reasons.append("the email also names another role: " + ", ".join(others[:3]))


def _thread_candidates(db: Database, thread_rows: list[Any], *, from_domain: str | None, subject: str,
                       body: str) -> list[AssociationCandidate]:
    """Targets of linked messages in the same thread.

    Only a message from the same registrable domain as a linked one inherits the
    link (method ``thread``); anyone else's message in the thread is at most a
    suggestion (``thread_other_sender``).
    """
    sender_reg = registrable_domain(from_domain)
    by_target: dict[tuple[int | None, int | None], float] = {}
    domains: dict[tuple[int | None, int | None], set[str]] = {}
    for r in thread_rows:
        key = (r["job_id"], r["application_id"])
        conf = 0.97 if r["confirmed_by_user"] else 0.95
        by_target[key] = max(by_target.get(key, 0.0), conf)
        reg = registrable_domain(r["from_domain"])
        domains.setdefault(key, set()).update({reg} if reg else set())
    targets = [k for k in by_target if k[0] is not None]
    if not targets:
        return []
    ambiguous = len(targets) > 1
    mentioned = mentioned_titles(subject, body)
    out = []
    for job_id, app_id in targets:
        status = None
        if app_id is not None:
            row = db.query_one("SELECT status FROM applications WHERE id = ?", (app_id,))
            status = row["status"] if row else None
        job = db.query_one("SELECT employer, title FROM jobs WHERE id = ?", (job_id,))
        same_sender = sender_reg is not None and sender_reg in domains[(job_id, app_id)]
        conf = REVIEW_CAP if ambiguous or not same_sender else by_target[(job_id, app_id)]
        reasons = ["same Gmail thread as a linked message"]
        if not same_sender:
            reasons.append(f"but sent from {sender_reg or 'an unknown domain'}, not from "
                           f"{', '.join(sorted(domains[(job_id, app_id)])) or 'the linked sender'}")
        c = AssociationCandidate(job_id=int(job_id), application_id=app_id,
                                 method="thread" if same_sender else "thread_other_sender", confidence=conf,
                                 reasons=reasons, employer=job["employer"] if job else "",
                                 title=job["title"] if job else "", application_status=status)
        if job is not None:
            _apply_title(c, *title_status(job["title"], job["employer"], mentioned, subject=subject, body=body))
        out.append(c)
    return sorted(out, key=lambda c: c.confidence, reverse=True)[:3]


def find_candidates(db: Database, *, thread_id: str, from_address: str | None, from_domain: str | None,
                    from_name: str | None, subject: str, body: str, employer_guess: str | None = None,
                    exclude_email_id: int | None = None) -> list[AssociationCandidate]:
    """Ranked association candidates (best first, at most 3)."""
    # Thread continuity (from the same sender domain) beats everything else.
    thread_rows = email_repo.thread_associations(db, thread_id, exclude_email_id=exclude_email_id) if thread_id else []
    threaded = _thread_candidates(db, thread_rows, from_domain=from_domain, subject=subject,
                                  body=body) if thread_rows else []
    if any(c.method == "thread" for c in threaded):
        return threaded

    local_part = (from_address or "").split("@", 1)[0]
    guess = normalize_employer(employer_guess) if employer_guess else ""
    confirmed_domains, confirmed_tenants = _confirmed_senders(db)
    mentioned = mentioned_titles(subject, body)
    targets = [dict(row) for row in email_repo.association_targets(db)]
    apps_per_employer = Counter(normalize_employer(t["employer"]) for t in targets if t["application_id"] is not None)
    candidates: list[AssociationCandidate] = []
    for target in targets:
        score, method, reasons = _employer_score(target, from_domain=from_domain, from_name=from_name,
                                                 local_part=local_part, subject=subject, body=body,
                                                 confirmed_domains=confirmed_domains,
                                                 confirmed_tenants=confirmed_tenants)
        if guess and guess == normalize_employer(target["employer"]) and score < 0.55:
            score, method, reasons = 0.55, "llm_guess", ["employer suggested by the local AI (unverified)"]
        if score <= 0:
            continue
        c = AssociationCandidate(job_id=int(target["job_id"]), application_id=target["application_id"],
                                 method=method, confidence=score, reasons=list(reasons),
                                 employer=target["employer"], title=target["title"],
                                 application_status=target["application_status"])
        # A title only strengthens / disambiguates an employer match; alone it is too weak.
        _apply_title(c, *title_status(target["title"], target["employer"], mentioned, subject=subject, body=body))
        if method not in VERIFIED_METHODS:
            c.confidence = min(c.confidence, UNVERIFIED_CAP)
        if c.title_status == "none":
            # Automatic updates need the email to name this role (only a same-sender thread is exempt).
            c.confidence = min(c.confidence, UNVERIFIED_CAP)
            c.reasons.append(f"the email does not name this application's role ({c.title})")
        several = apps_per_employer[normalize_employer(target["employer"])]
        if c.application_id is not None and several >= 2 and c.title_status != "match":
            c.confidence = min(c.confidence, REVIEW_CAP)
            c.reasons.append(f"you have {several} applications at this employer and the email does not name "
                             "this role")
        c.confidence = round(c.confidence, 3)
        if c.confidence >= MIN_STORE_CONFIDENCE:
            candidates.append(c)

    # Someone else's message in a linked thread: keep the thread's target as a suggestion.
    present = {(c.job_id, c.application_id) for c in candidates}
    candidates.extend(c for c in threaded if (c.job_id, c.application_id) not in present)

    candidates.sort(key=lambda c: (c.confidence, c.application_id is not None), reverse=True)
    if len(candidates) >= 2 and candidates[0].confidence - candidates[1].confidence < AMBIGUITY_MARGIN \
            and candidates[1].confidence >= REVIEW_CAP:
        # Two plausible targets: never auto-apply anything, ask the user.
        top = candidates[0].confidence
        for c in candidates:
            if c.confidence >= top - AMBIGUITY_MARGIN:
                c.confidence = min(c.confidence, REVIEW_CAP)
                c.reasons.append("ambiguous: several jobs at this employer match")
    return candidates[:3]


def store_candidates(db: Database, email_id: int, candidates: list[AssociationCandidate]) -> list[int]:
    ids = []
    for c in candidates:
        ids.append(email_repo.upsert_association(db, email_id=email_id, job_id=c.job_id,
                                                 application_id=c.application_id, method=c.method,
                                                 confidence=c.confidence))
    return ids
