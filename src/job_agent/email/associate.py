"""Link emails to the jobs / applications they are about, with a confidence 0..1.

Only three signals can reach the auto-update threshold (``AUTO_THRESHOLD``):
  * thread   -- an earlier message in the same Gmail thread is already linked
  * domain   -- the sender's registrable domain is exactly the domain of the
                job / application URL (``acme.com`` for ``careers.acme.com``)
  * confirmed_domain -- you linked mail from this domain to this employer before
  * employer_name (platform) -- an ATS / scheduling / assessment platform sender
                naming the full employer name in the subject or sender

Anything that merely *resembles* the employer stays well below the threshold:
a domain spelled like the employer's name on some TLD (``acme-analytics.xyz``),
a prefix (``better.com`` for Betterment), or the name inside a longer company
name ("Mercury Insurance" for Mercury). Those links are stored as suggestions
for you to confirm.

Job titles: when the email names a role that is not the application's role,
the link is capped below the threshold; when you have several applications at
the same employer, an automatic link additionally needs the email to name the
role.

The LLM's ``employer_guess`` is untrusted and only used as a weak hint: it can
never raise a candidate to the auto-update threshold by itself.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from job_agent.database import Database
from job_agent.email import repository as email_repo
from job_agent.email.classify import ATS_DOMAINS, is_generic_sender, is_platform_sender
from job_agent.email.parse import domain_core, registrable_domain

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
#: Hard ceiling for any link not backed by a verified source (exact job domain, a domain you confirmed,
#: a platform sender naming the employer, or the thread). Anyone can write an employer's name in a subject.
UNVERIFIED_CAP = 0.7

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
#: Labels / local-part words that say nothing about which company a platform account belongs to.
_PLATFORM_GENERIC = frozenset({
    "us", "eu", "uk", "ca", "au", "in", "mail", "email", "emails", "e", "em", "m", "mg", "notifications",
    "notification", "notify", "no", "reply", "noreply", "donotreply", "do", "not", "jobs", "job", "careers",
    "career", "talent", "hire", "hiring", "recruiting", "recruitment", "recruiter", "apply", "candidates",
    "candidate", "app", "apps", "www", "messages", "message", "msg", "system", "alerts", "alert", "info", "hr",
    "team", "support", "bounce", "bounces", "mailer", "sender", "outbound", "send", "scheduling", "schedule",
    "calendar", "invite", "invites", "assessment", "assessments", "test", "tests", "interview", "interviews",
})

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
)
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


def normalize_employer(name: str | None) -> str:
    """``"The Acme Analytics, Inc."`` -> ``"acme analytics"``."""
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    text = text.replace("&", " and ")
    tokens = [t for t in re.split(r"[^a-z0-9]+", text) if t]
    while tokens and tokens[0] == "the":
        tokens.pop(0)
    while len(tokens) > 1 and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def normalize_text(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    text = text.replace("&", " and ")
    return " ".join(t for t in re.split(r"[^a-z0-9]+", text) if t)


def _fold(text: str | None) -> str:
    """ASCII-folded text that keeps letter case (capitalization matters for name boundaries)."""
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().replace("&", " and ")


def _mentions(haystack: str, needle: str) -> bool:
    if not needle or len(needle.replace(" ", "")) < 3:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None


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
def names_employer(text: str | None, employer: str | None, *, title: str | None = None) -> bool:
    """Does ``text`` name this employer -- the full name, not part of a longer company name?

    "Mercury" is *not* named by "Mercury Insurance" (another capitalized word
    follows), but is by "Mercury Careers", "Mercury's team" or "Mercury." .
    """
    tokens = normalize_employer(employer).split()
    if not tokens or len("".join(tokens)) < 3:
        return False
    hay = _fold(text)
    if not hay:
        return False
    allowed = _FOLLOWER_OK | _LEGAL_FOLLOWERS | set(normalize_text(employer).split())
    title_norm = normalize_text(title)
    pattern = re.compile(r"(?<![A-Za-z0-9])" + r"[^A-Za-z0-9\n]{1,3}".join(re.escape(t) for t in tokens)
                         + r"(?![A-Za-z0-9])", re.I)
    for match in pattern.finditer(hay):
        rest = hay[match.end():]
        follower = re.match(r"(?:[ \t]+|-)([A-Z][A-Za-z0-9]*)", rest)
        if follower is None:
            return True  # end of text, punctuation, possessive, line break
        word = follower.group(1).lower()
        if word in allowed:
            return True
        if title_norm and normalize_text(rest[:len(title_norm) + 20]).startswith(title_norm):
            return True  # "Acme Analytics Business Analyst - application received"
    return False


def _platform_identity(from_domain: str | None, local_part: str, compact: str) -> str | None:
    """Which company a platform account belongs to, from its subdomain / local part.

    ``"match"`` (``acmeanalytics.icims.com``), ``"other"`` when it names a longer
    company starting with the employer's name (``mercuryinsurance.icims.com`` for
    Mercury), else ``None`` (nothing company-specific, e.g. ``no-reply@us.greenhouse-mail.io``).
    """
    domain = (from_domain or "").lower().strip(".")
    reg = registrable_domain(domain) or ""
    sub = domain[: -len(reg)].strip(".") if reg and domain.endswith(reg) else ""
    parts = [*(p for p in sub.split(".") if p), local_part.lower()]
    names = set()
    for part in parts:
        words = [w for w in re.split(r"[^a-z0-9]+", part) if w and w not in _PLATFORM_GENERIC and not w.isdigit()]
        if words:
            names.add("".join(words))
            names.update(words)
    if compact in names:
        return "match"
    if any(n != compact and n.startswith(compact) for n in names):
        return "other"
    return None


# --------------------------------------------------------------------------- #
# Job titles
# --------------------------------------------------------------------------- #
def mentioned_titles(subject: str | None, body: str | None) -> list[str]:
    """Role names an email refers to ("the Senior Data Engineer role" -> "Senior Data Engineer")."""
    found: list[str] = []
    for text in (_fold(subject), _fold(body)):
        if not text:
            continue
        for pattern in _TITLE_PATTERNS:
            for match in pattern.finditer(text):
                words = match.group("t").split()
                cut = max((i for i, w in enumerate(words) if w.lower() in _TITLE_BOUNDARY), default=-1)
                words = words[cut + 1:]
                while words and words[0].lower() in {"application", "applying", "interest", "job", "new"}:
                    words.pop(0)
                title = " ".join(words).strip(" -/&")
                norm = normalize_text(title)
                if not norm or all(t in _NOT_A_TITLE or t in _PLATFORM_NAMES for t in norm.split()):
                    continue
                noun = (match.groupdict().get("n") or "").lower()
                if noun not in _ROLE_NOUNS_AFTER and norm.split()[-1] not in _ROLE_WORDS:
                    continue  # "your Acme application", "application for Acme": not clearly a role
                if title not in found:
                    found.append(title)
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
    """``("match"|"mismatch"|"none", other titles named)`` for one application's title."""
    usable = [m for m in mentioned if _strip_employer(_title_key(m), employer)]
    if any(titles_match(m, target_title, employer=employer) for m in usable):
        return "match", []
    if usable:
        return "mismatch", usable
    title_norm = normalize_text(target_title)
    if len(title_norm) >= 4 and (_direct_title_mention(normalize_text(subject), title_norm)
                                 or _direct_title_mention(normalize_text(body), title_norm)):
        return "match", []
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
    #: Does the email name this application's role? "match" | "mismatch" | "none"
    title_status: str = "none"


def _employer_score(target: dict[str, Any], *, from_domain: str | None, from_name: str | None, local_part: str,
                    subject: str, body: str,
                    confirmed_domains: dict[str, set[str]] | None = None) -> tuple[float, str, list[str]]:
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
                       if d and not is_platform_sender(d) and not is_generic_sender(d)
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
    in_header = names_employer(subject, employer_raw, title=title) or names_employer(from_name, employer_raw,
                                                                                    title=title)
    in_body = names_employer(body, employer_raw, title=title)
    if platform:
        identity = _platform_identity(from_domain, local_part, compact)
        if identity == "other":
            return 0.0, "", []  # e.g. mercuryinsurance.icims.com writing about "Mercury Insurance"
        if in_header or identity == "match":
            consider(0.85, "employer_name", f"{sender_reg} message naming the employer in subject/sender")
        elif in_body:
            consider(0.75, "employer_name", f"{sender_reg} message naming the employer in the body")
    elif in_header:
        consider(0.7, "employer_name", "employer named in subject/sender")
    elif in_body:
        consider(0.6, "employer_name", "employer named in the body")
    return best


def _confirmed_domains(db: Database) -> dict[str, set[str]]:
    """normalized employer -> registrable sender domains you linked to it yourself."""
    out: dict[str, set[str]] = {}
    for row in email_repo.confirmed_sender_domains(db):
        domain = row["from_domain"]
        if not domain or is_platform_sender(domain) or is_generic_sender(domain):
            continue
        reg = registrable_domain(domain)
        employer = normalize_employer(row["employer"])
        if reg and employer:
            out.setdefault(employer, set()).add(reg)
    return out


def _apply_title(c: AssociationCandidate, status: str, others: list[str]) -> None:
    c.title_status = status
    if status == "match" and len(normalize_text(c.title)) >= 6 and c.method != "thread":
        c.confidence = min(0.97, c.confidence + 0.07)
        c.reasons.append("job title mentioned")
    elif status == "mismatch":
        c.confidence = min(c.confidence, REVIEW_CAP)
        c.reasons.append("the email names a different role: " + ", ".join(others[:3]))


def _thread_candidates(db: Database, thread_rows: list[Any], *, subject: str,
                       body: str) -> list[AssociationCandidate]:
    by_target: dict[tuple[int | None, int | None], float] = {}
    for r in thread_rows:
        key = (r["job_id"], r["application_id"])
        conf = 0.97 if r["confirmed_by_user"] else 0.95
        by_target[key] = max(by_target.get(key, 0.0), conf)
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
        conf = REVIEW_CAP if ambiguous else by_target[(job_id, app_id)]
        c = AssociationCandidate(job_id=int(job_id), application_id=app_id, method="thread", confidence=conf,
                                 reasons=["same Gmail thread as a linked message"],
                                 employer=job["employer"] if job else "", title=job["title"] if job else "",
                                 application_status=status)
        if job is not None:
            _apply_title(c, *title_status(job["title"], job["employer"], mentioned, subject=subject, body=body))
        out.append(c)
    return sorted(out, key=lambda c: c.confidence, reverse=True)[:3]


def find_candidates(db: Database, *, thread_id: str, from_address: str | None, from_domain: str | None,
                    from_name: str | None, subject: str, body: str, employer_guess: str | None = None,
                    exclude_email_id: int | None = None) -> list[AssociationCandidate]:
    """Ranked association candidates (best first, at most 3)."""
    # Thread continuity beats everything else.
    thread_rows = email_repo.thread_associations(db, thread_id, exclude_email_id=exclude_email_id) if thread_id else []
    if thread_rows:
        threaded = _thread_candidates(db, thread_rows, subject=subject, body=body)
        if threaded:
            return threaded

    local_part = (from_address or "").split("@", 1)[0]
    guess = normalize_employer(employer_guess) if employer_guess else ""
    confirmed = _confirmed_domains(db)
    mentioned = mentioned_titles(subject, body)
    targets = [dict(row) for row in email_repo.association_targets(db)]
    apps_per_employer = Counter(normalize_employer(t["employer"]) for t in targets if t["application_id"] is not None)
    candidates: list[AssociationCandidate] = []
    for target in targets:
        score, method, reasons = _employer_score(target, from_domain=from_domain, from_name=from_name,
                                                 local_part=local_part, subject=subject, body=body,
                                                 confirmed_domains=confirmed)
        if guess and guess == normalize_employer(target["employer"]) and score < 0.55:
            score, method, reasons = 0.55, "employer_name", ["employer suggested by the local AI (unverified)"]
        if score <= 0:
            continue
        c = AssociationCandidate(job_id=int(target["job_id"]), application_id=target["application_id"],
                                 method=method, confidence=score, reasons=list(reasons),
                                 employer=target["employer"], title=target["title"],
                                 application_status=target["application_status"])
        # A title only strengthens / disambiguates an employer match; alone it is too weak.
        _apply_title(c, *title_status(target["title"], target["employer"], mentioned, subject=subject, body=body))
        verified = method in {"domain", "confirmed_domain"} or (method == "employer_name"
                                                                and is_platform_sender(from_domain))
        if not verified:
            c.confidence = min(c.confidence, UNVERIFIED_CAP)
        several = apps_per_employer[normalize_employer(target["employer"])]
        if c.application_id is not None and several >= 2 and c.title_status != "match":
            c.confidence = min(c.confidence, REVIEW_CAP)
            c.reasons.append(f"you have {several} applications at this employer and the email does not name "
                             "this role")
        c.confidence = round(c.confidence, 3)
        if c.confidence >= MIN_STORE_CONFIDENCE:
            candidates.append(c)

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
