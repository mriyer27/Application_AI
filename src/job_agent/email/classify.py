"""Deterministic email classification (+ optional local-LLM fallback).

Rules look at subject, body and sender patterns and produce a category with a
confidence. When the rules are confident (>= ``RULES_CONFIDENT``) they win;
otherwise a local LLM classification is used if one is available, validated
against the ``EmailClassification`` contract. Only *rules*-confident results
can drive automatic status updates (see ``email.status``); an LLM label only
ever marks a message for the user's attention.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import ValidationError

from job_agent.errors import PermanentError
from job_agent.models import EmailCategory, EmailClassification
from job_agent.security import textnorm
from job_agent.security.untrusted import clean_untrusted

C = EmailCategory

#: Rule confidence at or above which the LLM is not consulted.
RULES_CONFIDENT = 0.8

#: Applicant-tracking systems (send on behalf of employers).
ATS_DOMAINS: frozenset[str] = frozenset({
    "greenhouse.io", "greenhouse-mail.io", "lever.co", "ashbyhq.com", "ashbyhq.io", "myworkday.com",
    "myworkdayjobs.com", "workday.com", "icims.com", "smartrecruiters.com", "jobvite.com", "taleo.net",
    "successfactors.com", "successfactors.eu", "bamboohr.com", "workablemail.com", "workable.com", "breezy.hr",
    "recruitee.com", "jazzhr.com", "applytojob.com", "teamtailor.com", "pinpointhq.com", "oraclecloud.com",
    "ultipro.com", "paylocity.com", "dayforcehcm.com", "phenompeople.com", "avature.net", "eightfold.ai",
})
SCHEDULING_DOMAINS: frozenset[str] = frozenset({
    "calendly.com", "goodtime.io", "modernloop.io", "prelude.co", "gem.com", "youcanbook.me", "cal.com",
})
ASSESSMENT_DOMAINS: frozenset[str] = frozenset({
    "hackerrank.com", "codility.com", "codesignal.com", "hirevue.com", "karat.io", "karat.com", "testgorilla.com",
    "pymetrics.ai", "pymetrics.com", "harver.com", "criteriacorp.com", "shl.com", "mettl.com", "coderbyte.com",
    "qualified.io", "modernhire.com",
})
#: Job boards whose bulk mail (alerts, digests) is never an employer response.
JOB_BOARD_DOMAINS: frozenset[str] = frozenset({
    "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com", "monster.com", "dice.com",
    "joinhandshake.com", "handshake.com", "simplyhired.com", "wellfound.com", "builtin.com", "careerbuilder.com",
})
#: Free-mail providers: a sender here says nothing about the employer.
GENERIC_MAIL_DOMAINS: frozenset[str] = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com", "icloud.com",
    "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.com", "mail.com", "zoho.com", "yandex.com",
})

#: Categories that ask the user to do something (reply, schedule, complete).
ACTIONABLE: frozenset[EmailCategory] = frozenset({
    C.RECRUITER_MESSAGE, C.INTERVIEW_INVITATION, C.SCHEDULING_REQUEST, C.ASSESSMENT, C.OFFER,
    C.FOLLOW_UP_OPPORTUNITY,
})

LABELS: dict[EmailCategory, str] = {
    C.APPLICATION_CONFIRMATION: "Application confirmation",
    C.RECRUITER_MESSAGE: "Recruiter message",
    C.INTERVIEW_INVITATION: "Interview invitation",
    C.SCHEDULING_REQUEST: "Scheduling request",
    C.ASSESSMENT: "Assessment",
    C.REJECTION: "Rejection",
    C.OFFER: "Offer",
    C.FOLLOW_UP_OPPORTUNITY: "Follow-up opportunity",
    C.OTHER: "Other",
}

#: Tie-break order (higher wins when scores are equal).
_PRIORITY: dict[EmailCategory, int] = {
    C.OFFER: 8, C.REJECTION: 7, C.INTERVIEW_INVITATION: 6, C.ASSESSMENT: 5, C.SCHEDULING_REQUEST: 4,
    C.APPLICATION_CONFIRMATION: 3, C.RECRUITER_MESSAGE: 2, C.FOLLOW_UP_OPPORTUNITY: 1, C.OTHER: 0,
}

_ROUND = (r"(?:(?:first|second|third|final|next|onsite|on-site|virtual|video|phone|technical|initial|"
          r"hiring manager)[- ](?:round[- ])?)?")


@dataclass(frozen=True)
class _Rule:
    category: EmailCategory
    pattern: re.Pattern[str]
    subject_weight: float
    body_weight: float


def _r(category: EmailCategory, pattern: str, subject_weight: float, body_weight: float) -> _Rule:
    return _Rule(category, re.compile(pattern, re.I), subject_weight, body_weight)


RULES: tuple[_Rule, ...] = (
    # -- application confirmation -------------------------------------------------
    _r(C.APPLICATION_CONFIRMATION,
       r"\bthanks? (?:you )?for (?:applying|your (?:job )?application|submitting your application)\b", 3.0, 2.5),
    _r(C.APPLICATION_CONFIRMATION,
       r"\b(?:we(?:'ve| have)|has been|was) (?:successfully )?received\b.{0,40}\bapplication\b|"
       r"\bapplication (?:has been|was) (?:successfully )?(?:received|submitted)\b|\breceived your application\b",
       3.0, 2.5),
    _r(C.APPLICATION_CONFIRMATION, r"\bapplication (?:received|submitted|confirmation|acknowledg(?:e)?ment)\b",
       3.0, 1.5),
    _r(C.APPLICATION_CONFIRMATION,
       r"\b(?:application|resume) (?:is|will be) (?:under|being) review(?:ed)?\b|"
       r"\b(?:our|the) (?:team|recruiters?|recruiting team|hiring team) (?:will|is going to) (?:review|be reviewing)\b",
       1.5, 1.5),
    _r(C.APPLICATION_CONFIRMATION, r"\bthanks? (?:you )?for your interest\b", 1.0, 0.8),
    # -- rejection ----------------------------------------------------------------
    _r(C.REJECTION,
       r"\b(?:regret to inform|unable to (?:move|proceed) forward|not (?:be )?(?:moving|move|proceeding|proceed) "
       r"forward|won't be (?:moving|proceeding) forward|decided (?:not to (?:move|proceed)|to (?:move|proceed|go) "
       r"forward with (?:other|another))|(?:pursue|pursuing|move forward with) other candidates|(?:position|role) "
       r"has (?:now )?been filled|no longer (?:under consideration|being considered|considering your)|"
       r"(?:not|were not|was not|have not been) selected|other candidates whose|decided to move in a different "
       r"direction|not (?:able|in a position) to offer you|mov(?:ed|ing) forward with (?:another|other|a different) "
       r"(?:candidate|applicant)s?)\b", 3.5, 3.5),
    _r(C.REJECTION, r"\bunfortunately\b", 1.5, 1.0),
    # -- offer ----------------------------------------------------------------------
    _r(C.OFFER,
       r"\b(?:pleased|delighted|happy|excited|thrilled) to (?:extend|make|present) (?:to )?(?:you )?(?:an?|the|this) "
       r"(?:formal |written |verbal |conditional )?(?:job |employment )?offer\b", 4.0, 4.0),
    _r(C.OFFER, r"\b(?:pleased|delighted|happy|excited|thrilled) to offer you (?:(?:the|a|an|this|our) "
                r"(?:[\w-]+ ){0,4}?(?:position|role|job)|(?:full-time |part-time )?employment)\b", 4.0, 4.0),
    # Never a bare "your offer": shops and banks send "your offer is ready" all the time.
    _r(C.OFFER, r"\b(?:offer letter|offer of employment|employment offer|job offer)\b", 3.5, 2.0),
    # -- interview invitation -------------------------------------------------------
    _r(C.INTERVIEW_INVITATION,
       r"\b(?:invite|inviting) you (?:to|for) (?:an? |the |our )?" + _ROUND +
       r"(?:interview|phone screen|screening call|video call)\b", 3.5, 3.5),
    _r(C.INTERVIEW_INVITATION,
       r"\b(?:would like|like|love|want) to (?:invite you|schedule|set up|arrange) (?:you )?(?:for )?(?:an? |a time "
       r"for an? )?" + _ROUND + r"(?:interview|phone screen|screening call)\b", 3.0, 3.0),
    _r(C.INTERVIEW_INVITATION, r"\binterview (?:invitation|invite|request)\b|\binvitation to (?:an )?interview\b",
       3.5, 3.0),
    _r(C.INTERVIEW_INVITATION,
       r"\bnext (?:step|round) (?:is|will be|in our process is) (?:an? )?(?:interview|phone screen)\b|"
       r"\bmove (?:you )?forward (?:to|in|with) (?:the |an )?(?:next round|interview)", 2.5, 2.5),
    _r(C.INTERVIEW_INVITATION, r"\binterview\b", 1.5, 0.5),
    # -- scheduling -----------------------------------------------------------------
    _r(C.SCHEDULING_REQUEST,
       r"(?:calendly\.com|goodtime\.io|cal\.com/|youcanbook\.me|\bscheduling link\b|\bschedule (?:a|your) "
       r"(?:time|call|interview|meeting|conversation)\b|\b(?:book|pick|select|choose) a (?:time|slot)\b)", 2.5, 3.0),
    _r(C.SCHEDULING_REQUEST,
       r"\b(?:share|send|provide|let (?:me|us) know) (?:your|a few|some) (?:availability|available times|times that "
       r"work)\b|\bwhat (?:times|days) (?:work|would work|are you available)\b", 3.0, 3.0),
    _r(C.SCHEDULING_REQUEST, r"\breschedul\w*\b|\bschedul\w* (?:confirmation|request)\b", 2.5, 2.0),
    _r(C.SCHEDULING_REQUEST, r"\bavailab(?:le|ility)\b", 1.0, 0.7),
    # -- assessment -----------------------------------------------------------------
    _r(C.ASSESSMENT,
       r"\b(?:online assessment|coding (?:challenge|assessment|test|exercise)|technical (?:assessment|exercise|"
       r"challenge)|take[- ]home (?:assignment|exercise|challenge|project|assessment|test)|skills? (?:assessment|"
       r"test)|case study (?:assignment|exercise)|assessment (?:link|invitation|invite)|complete (?:the|an|this|your) "
       r"(?:online )?(?:assessment|exercise|challenge|test))\b", 3.5, 3.0),
    _r(C.ASSESSMENT,
       r"\b(?:hackerrank|codility|codesignal|hirevue|karat|testgorilla|pymetrics|harver|coderbyte|mettl)\b", 2.5, 2.0),
    _r(C.ASSESSMENT, r"\bassessment\b", 1.5, 0.5),
    # -- recruiter ------------------------------------------------------------------
    _r(C.RECRUITER_MESSAGE,
       r"\b(?:i(?:'m| am) (?:a|the) (?:\w+ ){0,2}recruiter|recruiter (?:at|with|for)|(?:talent acquisition|talent "
       r"partner|sourcer|recruiting (?:team|partner|coordinator)) (?:at|with|for)|i (?:came across|found) your "
       r"(?:profile|background|resume|linkedin)|your (?:background|profile|experience) (?:caught|stood out)|"
       r"(?:reaching|reach) out (?:to you )?(?:about|regarding|because)|would you be (?:open|interested)|are you "
       r"(?:open|interested) (?:to|in)|i(?:'d| would) love to (?:connect|chat|speak|talk))\b", 2.5, 2.5),
    _r(C.RECRUITER_MESSAGE, r"\b(?:quick|brief|short) (?:call|chat)\b|\bexciting (?:opportunity|role)\b", 1.0, 1.0),
    # -- follow-up opportunity ------------------------------------------------------
    _r(C.FOLLOW_UP_OPPORTUNITY,
       r"\b(?:great|nice|good|pleasure) (?:meeting|to meet|talking|speaking|chatting) (?:with )?you\b|\bthanks? "
       r"(?:you )?for (?:stopping by|visiting|attending|coming by|meeting|connecting|chatting)\b|\b(?:career|job) "
       r"fair\b|\binfo(?:rmation(?:al)?)? session\b|\bnetworking (?:event|session)\b|\bcoffee chat\b", 2.5, 2.5),
    _r(C.FOLLOW_UP_OPPORTUNITY,
       r"\bfeel free to (?:reach out|follow up|apply|connect|send)\b|\b(?:keep|stay) in touch\b|"
       r"\bfollow(?:ing)?[- ]up\b|\bchecking in\b", 1.5, 1.0),
)

_NEWSLETTER = re.compile(
    r"\b(?:unsubscribe|newsletter|job alerts?|jobs? (?:you may|you might) (?:like|be interested in)|recommended "
    r"(?:jobs|for you)|new jobs? (?:for|matching|posted|near)|weekly digest|daily digest|top jobs|view (?:this email )?"
    r"in (?:your )?browser|manage (?:your )?(?:email |notification )?preferences|email preferences)\b",
    re.I,
)
#: "We are unable to extend an offer", "we must rescind your offer": an offer that is *not* being made.
_OFFER_NEGATED = re.compile(
    r"\b(?:unable to|not able to|not in a position to|cannot|can ?not|can't|won't|will not|would not|are not|aren't|"
    r"is not|isn't|not to|do not|don't|no longer)\s+(?:be\s+)?(?:able to\s+)?"
    r"(?:extend(?:ing)?|mak(?:e|ing)|offer(?:ing)?|present(?:ing)?|proceed(?:ing)? with|mov(?:e|ing) forward with)\s+"
    r"(?:you\s+)?(?:an?\s+|the\s+|this\s+|your\s+|any\s+)?(?:formal\s+|written\s+|verbal\s+|conditional\s+|final\s+)?"
    r"(?:job\s+|employment\s+)?offer\b"
    r"|\b(?:rescind(?:s|ed|ing)?|revok(?:e|es|ed|ing)|retract(?:s|ed|ing)?|withdr(?:aw|aws|awn|awing|ew))\s+"
    r"(?:the\s+|your\s+|our\s+|this\s+|its\s+|an?\s+)?(?:job\s+|employment\s+)?offer\b"
    r"|\boffer(?:\s+of\s+employment|\s+letter)?\s+(?:has|have|had)\s+(?:now\s+)?been\s+"
    r"(?:rescinded|revoked|retracted|withdrawn|cancell?ed)\b",
    re.I,
)
#: The offer is being made to *someone else*: "we extended an offer to another candidate, who accepted it",
#: "the position has been filled", "we moved forward with another applicant". That is a rejection.
_OFFER_TO_OTHER = re.compile(
    r"\boffer(?:\s+of\s+employment)?\s+to\s+(?:another|other|a\s+different|the\s+other|one\s+of\s+the\s+other|"
    r"the\s+selected|the\s+successful|the\s+chosen|the|a|an)\s+(?:[a-z-]+\s+){0,3}?"
    r"(?:candidate|applicant|individual|person|finalist)s?\b"
    r"|\b(?:extended|made|presented|given)\s+(?:an?|the|our|this|that)\s+(?:job\s+|employment\s+|final\s+)?offer"
    r"(?:\s+of\s+employment)?\s+to\s+(?!your?\b|yourself\b)[a-z]"
    r"|\b(?:candidate|applicant|individual|person|finalist)s?,?\s+(?:who|that)\s+(?:has\s+|have\s+)?"
    r"(?:since\s+|already\s+|now\s+)?accepted\b"
    r"|\b(?:position|role|opening|vacancy|job|req(?:uisition)?)\s+(?:has|have|had|was|is)\s+(?:now\s+|already\s+|since\s+)?"
    r"(?:been\s+)?filled\b"
    r"|\bmov(?:e|ed|ing)\s+forward\s+with\s+(?:another|other|a\s+different|a\s+more)\b"
    r"|\b(?:decided|chosen|chose|selected)\s+to\s+(?:hire|offer\s+the\s+(?:position|role)\s+to)\s+"
    r"(?:another|a\s+different|someone\s+else|an?\s+other)\b"
    r"|\bhired\s+(?:another|someone\s+else|a\s+different)\b",
    re.I,
)
#: An offer is only an offer *to you* when it is addressed to the reader.
_OFFER_TO_READER = re.compile(
    r"\b(?:extend(?:ed|ing)?|mak(?:e|ing)|made|present(?:ed|ing)?|send(?:ing)?|sent|giv(?:e|ing)|offer(?:ed|ing)?)\s+"
    r"(?:to\s+)?you\s+(?:with\s+)?(?:an?|the|this|our|your)\s+(?:[a-z-]+\s+){0,3}?offer\b"
    r"|\boffer(?:ed|ing)?\s+you\s+(?:the|a|an|this|our)\s+(?:[a-z-]+\s+){0,4}?(?:position|role|job)\b"
    r"|\boffer(?:ed|ing)?\s+you\s+(?:full-time\s+|part-time\s+)?employment\b"
    r"|\boffer(?:\s+of\s+employment)?\s+(?:to|for)\s+you\b"
    r"|\byour\s+(?:[a-z-]+\s+){0,2}?(?:offer\s+letter|offer\s+of\s+employment|job\s+offer|employment\s+offer|"
    r"offer\s+package|offer\s+details)\b"
    r"|\byou(?:'ve|\s+have)?\s+(?:been|are\s+being)\s+(?:extended|offered|made)\s+(?:an?|the)\b",
    re.I,
)
#: "Join our talent community": an invitation, not an offer of employment.
_NOT_AN_OFFER = re.compile(r"\btalent\s+(?:community|pool|network|pipeline)\b|\bjoin\s+our\s+(?:mailing|newsletter)\b",
                           re.I)
#: A conditional earlier in the same clause ("we reserve the right to rescind this offer if ...",
#: "if you decide not to move forward with the offer").
_CONDITIONAL_BEFORE = re.compile(
    r"\b(?:right to|may|might|could|would|can|if|unless|should|in case|whether)\b[^.!?;:\n]*$", re.I)
#: An offer email must be about employment, not a shop's or bank's "offer".
_EMPLOYMENT_CONTEXT = re.compile(
    r"\b(?:position|role|employment|salary|compensation|start(?:ing)? date|base pay|annual base|offer letter|"
    r"job offer|job title|hiring manager|benefits package|signing bonus|sign-on bonus|onboarding|"
    r"background check|reporting to)\b",
    re.I,
)
#: Marketing / bulk markers in the body. Such mail never changes a status automatically.
_PROMOTIONAL = re.compile(
    r"\b(?:unsubscribe|opt[- ]out|view (?:this email |it )?(?:in|on) (?:your |a )?(?:web )?browser|"
    r"manage (?:your )?(?:email |notification |subscription |communication )?preferences|email preferences|"
    r"pre-?approved|pre-?qualified|promo(?:tional)? code|coupon|limited[- ]time|annual fee|shop now|buy now|"
    r"order now|cash ?back|special offer|exclusive offer|credit (?:card|limit)|apr|rewards? points|free shipping)\b"
    r"|\b\d+\s?% off\b",
    re.I,
)
_BULK_LOCALPART = re.compile(r"(?:job-?alerts?|alerts?|newsletters?|news|digest|marketing|promo(?:tions)?|"
                             r"notifications?|updates|jobs-listings|hello|info)", re.I)
_NOREPLY = re.compile(r"(?:no-?reply|do-?not-?reply|donotreply|notifications?)", re.I)


def _norm(text: str | None) -> str:
    """Shared Unicode normalization (look-alike letters, invisible characters), typographic quotes, whitespace."""
    text = textnorm.normalize_text(text or "")
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip()


def _matches_domain(domain: str | None, domains: frozenset[str]) -> bool:
    if not domain:
        return False
    d = domain.lower()
    return any(d == x or d.endswith("." + x) for x in domains)


def is_ats_sender(domain: str | None) -> bool:
    return _matches_domain(domain, ATS_DOMAINS)


def is_platform_sender(domain: str | None) -> bool:
    """ATS, scheduling or assessment platform sending on an employer's behalf."""
    return (_matches_domain(domain, ATS_DOMAINS) or _matches_domain(domain, SCHEDULING_DOMAINS)
            or _matches_domain(domain, ASSESSMENT_DOMAINS))


def is_generic_sender(domain: str | None) -> bool:
    return _matches_domain(domain, GENERIC_MAIL_DOMAINS) or _matches_domain(domain, JOB_BOARD_DOMAINS)


def is_promotional(*, subject: str, body: str) -> bool:
    """Marketing / bulk markers ("unsubscribe", "pre-approved", "% off", ...) in the subject or body."""
    return bool(_PROMOTIONAL.search(_norm(subject)) or _PROMOTIONAL.search(_norm(body)))


def _unconditional(pattern: re.Pattern[str], text: str) -> bool:
    for match in pattern.finditer(text):
        if not _CONDITIONAL_BEFORE.search(text[max(0, match.start() - 60):match.start()]):
            return True
    return False


def offer_negated(text: str) -> bool:
    """An offer that is declined, rescinded, not extended, or made to someone else (conditionals do not count)."""
    return _unconditional(_OFFER_NEGATED, text) or offer_to_other(text)


def offer_to_other(text: str) -> bool:
    """The offer went to someone else ("extended an offer to another candidate", "the position has been filled")."""
    return _unconditional(_OFFER_TO_OTHER, text)


def offer_to_reader(text: str) -> bool:
    """The text makes an offer to *you* ("extend you an offer", "offer you the position", "your offer letter")."""
    return bool(_OFFER_TO_READER.search(text))


@dataclass
class RuleScores:
    scores: dict[EmailCategory, float]
    newsletter: float
    #: Strong offer *and* strong rejection signals: never confident enough to act on.
    conflict: bool = False
    #: Offer wording that turned out to be negated or meant for someone else, with no rejection wording
    #: of its own: most likely a rejection, but a misread offer must never be filed away automatically.
    negated_offer_only: bool = False


def score_rules(*, subject: str, body: str, from_address: str | None, from_domain: str | None,
                bulk: bool = False) -> RuleScores:
    subj = _norm(subject)
    text = _norm(body)
    scores: dict[EmailCategory, float] = {c: 0.0 for c in C if c != C.OTHER}
    for rule in RULES:
        weight = 0.0
        if subj and rule.pattern.search(subj):
            weight = rule.subject_weight
        if text and rule.pattern.search(text):
            weight = max(weight, rule.body_weight) + (min(weight, rule.body_weight) * 0.25 if weight else 0.0)
        scores[rule.category] += weight

    both = f"{subj}\n{text}"
    conflict = False
    negated_offer_only = False
    if offer_negated(subj) or offer_negated(text):
        if scores[C.OFFER] >= 4.0 and offer_to_reader(both):
            conflict = True  # "pleased to extend you an offer" next to "unable to extend an offer": ask the user
        else:
            # "pleased to extend an offer to another candidate" is a rejection
            to_other = offer_to_other(subj) or offer_to_other(text)
            negated_offer_only = (scores[C.OFFER] > 0 or to_other) and scores[C.REJECTION] < 3.5
            scores[C.OFFER] = 0.0
        scores[C.REJECTION] += 3.5
    elif scores[C.OFFER] > 0 and not _EMPLOYMENT_CONTEXT.search(both):
        scores[C.OFFER] = 0.0  # "an offer" with nothing about a job: marketing, not an employment offer
    if scores[C.OFFER] >= 3.0 and scores[C.REJECTION] >= 3.0:
        conflict = True

    local = (from_address or "").split("@", 1)[0]
    if _matches_domain(from_domain, ASSESSMENT_DOMAINS):
        scores[C.ASSESSMENT] += 2.0
    if _matches_domain(from_domain, SCHEDULING_DOMAINS):
        scores[C.SCHEDULING_REQUEST] += 2.0
    noreply = bool(_NOREPLY.search(local))
    if scores[C.RECRUITER_MESSAGE] > 0:
        if noreply or is_platform_sender(from_domain):
            scores[C.RECRUITER_MESSAGE] = max(0.0, scores[C.RECRUITER_MESSAGE] - 1.0)
        else:
            scores[C.RECRUITER_MESSAGE] += 0.5

    newsletter = 0.0
    if bulk:
        newsletter += 2.0
    if _NEWSLETTER.search(subj):
        newsletter += 2.0
    if _NEWSLETTER.search(text):
        newsletter += 1.5
    if _matches_domain(from_domain, JOB_BOARD_DOMAINS) and (_BULK_LOCALPART.search(local) or noreply):
        newsletter += 3.0
    return RuleScores(scores=scores, newsletter=newsletter, conflict=conflict, negated_offer_only=negated_offer_only)


def rules_summary(category: EmailCategory, *, subject: str, from_name: str | None, from_domain: str | None) -> str:
    who = from_name or from_domain or "unknown sender"
    return f"{LABELS[category]} from {who}: {_norm(subject) or '(no subject)'}"[:300]


def classify_rules(*, subject: str, body: str, from_address: str | None = None, from_domain: str | None = None,
                   from_name: str | None = None, bulk: bool = False) -> EmailClassification:
    """Deterministic classification with a confidence in [0, 1]."""
    rs = score_rules(subject=subject, body=body, from_address=from_address, from_domain=from_domain, bulk=bulk)
    penalty = 0.5 * rs.newsletter
    effective = {c: max(0.0, s - penalty) for c, s in rs.scores.items()}
    ranked = sorted(effective.items(), key=lambda kv: (kv[1], _PRIORITY[kv[0]]), reverse=True)
    best, best_score = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0

    if best_score < 1.0 or (rs.newsletter >= 3.0 and best_score < 3.5):
        confidence = min(0.95, 0.55 + 0.1 * rs.newsletter) if rs.newsletter else 0.55
        category = C.OTHER
    else:
        category = best
        confidence = min(0.97, 0.5 + 0.1 * best_score)
        if second >= 1.0:
            confidence -= 0.1 * (second / best_score)
        confidence = max(0.3, confidence)
        if rs.conflict:
            confidence = min(confidence, 0.6)  # contradictory signals: ask the user
        if category == C.OFFER:
            both = f"{_norm(subject)}\n{_norm(body)}"
            if not offer_to_reader(both) or _NOT_AN_OFFER.search(both):
                confidence = min(confidence, 0.6)  # not clearly an offer *to you*: ask the user
        elif category == C.REJECTION and rs.negated_offer_only:
            confidence = min(confidence, 0.6)
    return EmailClassification(
        category=category,
        confidence=round(confidence, 3),
        summary=rules_summary(category, subject=subject, from_name=from_name, from_domain=from_domain),
        employer_guess=None,
        requires_attention=category in ACTIONABLE,
        classifier="rules",
    )


def validate_llm_classification(raw: Any) -> EmailClassification:
    """Re-validate an LLM result: enum category, confidence in [0, 1], bounded text."""
    data = raw.model_dump() if hasattr(raw, "model_dump") else raw
    if not isinstance(data, dict):
        raise PermanentError("LLM email classification is not an object")
    data = dict(data)
    try:
        data["category"] = EmailCategory(str(getattr(data.get("category"), "value", data.get("category"))))
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError) as exc:
        raise PermanentError(f"invalid LLM email classification: {exc}") from None
    if confidence != confidence:  # NaN
        confidence = 0.0
    data["confidence"] = min(1.0, max(0.0, confidence))
    data["summary"] = clean_untrusted(str(data.get("summary") or ""), max_chars=1000).replace("\n", " ")[:300]
    guess = data.get("employer_guess")
    data["employer_guess"] = clean_untrusted(str(guess), max_chars=500)[:120] or None if guess else None
    classifier = str(data.get("classifier") or "")
    data["classifier"] = classifier[:80] if classifier.startswith("llm:") else "llm:unknown"
    data["requires_attention"] = bool(data.get("requires_attention")) or data["category"] in ACTIONABLE
    try:
        return EmailClassification.model_validate(data)
    except ValidationError as exc:
        raise PermanentError(f"invalid LLM email classification: {exc.error_count()} error(s)") from None


LLMClassifier = Callable[..., Any]


def combine(rules: EmailClassification, llm: EmailClassification | None) -> EmailClassification:
    """Confident rules win; otherwise a validated LLM result (if any); else the rules result."""
    if rules.confidence >= RULES_CONFIDENT or llm is None:
        return rules
    return llm
