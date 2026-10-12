"""Link emails to the jobs / applications they are about, with a confidence 0..1.

Signals, strongest first:
  * thread   -- an earlier message in the same Gmail thread is already linked
  * domain   -- sender's domain matches the employer (name or job/application URL)
  * employer_name -- ATS / scheduling / assessment platform sender and the
                employer's name appears in the subject, sender name or body
  * job title -- a title mention raises an employer match and disambiguates
                between several jobs at one employer (never used on its own)

The LLM's ``employer_guess`` is untrusted and only used as a weak hint: it can
never raise a candidate to the auto-update threshold by itself.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
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

_LEGAL_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "plc", "gmbh", "ag",
    "sa", "lp", "llp", "pllc", "holdings", "group", "the",
}


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


def _employer_score(target: dict, *, from_domain: str | None, from_name: str | None, local_part: str,
                    subject: str, body: str) -> tuple[float, str, list[str]]:
    employer = normalize_employer(target["employer"])
    compact = employer.replace(" ", "")
    if not compact:
        return 0.0, "", []
    reasons: list[str] = []
    sender_reg = registrable_domain(from_domain)
    core = domain_core(from_domain)

    # 1. Employer's own domain.
    if sender_reg and not is_platform_sender(from_domain) and not is_generic_sender(from_domain):
        job_domains = {d for d in (_host_domain(target["job_url"]), _host_domain(target["application_url"])) if d}
        job_domains = {d for d in job_domains if not any(d == a or d.endswith("." + a) for a in ATS_DOMAINS)}
        if sender_reg in job_domains:
            return 0.9, "domain", [f"sender domain {sender_reg} matches the job's website"]
        if core and core == compact:
            return 0.9, "domain", [f"sender domain {sender_reg} matches employer name"]
        first = employer.split(" ")[0]
        if core and len(core) >= 4 and (compact.startswith(core) or (len(first) >= 4 and core == first)):
            return 0.8, "domain", [f"sender domain {sender_reg} resembles employer name"]

    subj, name_text, body_text = normalize_text(subject), normalize_text(from_name), normalize_text(body)
    local_text = normalize_text(local_part)
    in_header = _mentions(subj, employer) or _mentions(name_text, employer) or (
        is_platform_sender(from_domain) and len(compact) >= 4 and compact in local_text.replace(" ", ""))
    in_body = _mentions(body_text, employer)

    # 2. ATS / platform sender writing on the employer's behalf.
    if is_platform_sender(from_domain):
        if in_header:
            return 0.85, "employer_name", [f"{sender_reg} message naming the employer in subject/sender"]
        if in_body:
            return 0.75, "employer_name", [f"{sender_reg} message naming the employer in the body"]
        return 0.0, "", reasons

    # 3. Any other sender that names the employer.
    if in_header:
        return 0.7, "employer_name", ["employer named in subject/sender"]
    if in_body:
        return 0.6, "employer_name", ["employer named in the body"]
    return 0.0, "", reasons


def find_candidates(db: Database, *, thread_id: str, from_address: str | None, from_domain: str | None,
                    from_name: str | None, subject: str, body: str, employer_guess: str | None = None,
                    exclude_email_id: int | None = None) -> list[AssociationCandidate]:
    """Ranked association candidates (best first, at most 3)."""
    # Thread continuity beats everything else.
    thread_rows = email_repo.thread_associations(db, thread_id, exclude_email_id=exclude_email_id) if thread_id else []
    if thread_rows:
        by_target: dict[tuple[int | None, int | None], float] = {}
        for r in thread_rows:
            key = (r["job_id"], r["application_id"])
            conf = 0.97 if r["confirmed_by_user"] else 0.95
            by_target[key] = max(by_target.get(key, 0.0), conf)
        targets = [k for k in by_target if k[0] is not None]
        if targets:
            ambiguous = len(targets) > 1
            out = []
            for job_id, app_id in targets:
                status = None
                if app_id is not None:
                    row = db.query_one("SELECT status FROM applications WHERE id = ?", (app_id,))
                    status = row["status"] if row else None
                conf = 0.6 if ambiguous else by_target[(job_id, app_id)]
                out.append(AssociationCandidate(job_id=int(job_id), application_id=app_id, method="thread",
                                                confidence=conf, reasons=["same Gmail thread as a linked message"],
                                                application_status=status))
            return sorted(out, key=lambda c: c.confidence, reverse=True)[:3]

    local_part = (from_address or "").split("@", 1)[0]
    subj_norm, body_norm = normalize_text(subject), normalize_text(body)
    guess = normalize_employer(employer_guess) if employer_guess else ""
    candidates: list[AssociationCandidate] = []
    for row in email_repo.association_targets(db):
        target = dict(row)
        score, method, reasons = _employer_score(target, from_domain=from_domain, from_name=from_name,
                                                 local_part=local_part, subject=subject, body=body)
        if guess and guess == normalize_employer(target["employer"]) and score < 0.55:
            score, method, reasons = 0.55, "employer_name", ["employer suggested by the local AI (unverified)"]
        # A title mention only strengthens / disambiguates an employer match; alone it is too weak.
        title = normalize_text(target["title"])
        if score > 0 and len(title) >= 6 and (_mentions(subj_norm, title) or _mentions(body_norm, title)):
            score = min(0.97, score + 0.07)
            reasons.append("job title mentioned")
        if score >= MIN_STORE_CONFIDENCE:
            candidates.append(AssociationCandidate(
                job_id=int(target["job_id"]), application_id=target["application_id"], method=method,
                confidence=round(score, 3), reasons=reasons, employer=target["employer"], title=target["title"],
                application_status=target["application_status"]))

    candidates.sort(key=lambda c: (c.confidence, c.application_id is not None), reverse=True)
    if len(candidates) >= 2 and candidates[0].confidence - candidates[1].confidence < AMBIGUITY_MARGIN \
            and candidates[1].confidence >= 0.6:
        # Two plausible targets: never auto-apply anything, ask the user.
        for c in candidates:
            if c.confidence >= candidates[0].confidence - AMBIGUITY_MARGIN:
                c.confidence = min(c.confidence, 0.6)
                c.reasons.append("ambiguous: several jobs at this employer match")
    return candidates[:3]


def store_candidates(db: Database, email_id: int, candidates: list[AssociationCandidate]) -> list[int]:
    ids = []
    for c in candidates:
        ids.append(email_repo.upsert_association(db, email_id=email_id, job_id=c.job_id,
                                                 application_id=c.application_id, method=c.method,
                                                 confidence=c.confidence))
    return ids
