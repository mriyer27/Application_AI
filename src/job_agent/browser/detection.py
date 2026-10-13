"""Pure-Python classification of page snapshots.

The browser collects a ``PageSnapshot`` (URL, title, rendered *visible* text,
iframe and script sources, a few marker selectors); these functions decide whether the
page is a security challenge / login wall, a closed listing, or a submission
confirmation. Keeping this logic browser-free makes it unit-testable.

Detection is heuristic and errs on the side of stopping: a false positive
means *you* finish the application by hand; a false negative is caught by the
fact that nothing is ever submitted without your approval.

Confirmation detection is deliberately strict, because "confirmed" records the
application as SUBMITTED: it needs strong past-tense confirmation wording in the
page's main content that was not on the form page, no "not yet / before it is
submitted / review your application" wording anywhere, and the application
form must be gone. A URL alone never confirms anything. Anything less is
ambiguous and becomes an unknown outcome (``NEEDS_MANUAL_ACTION``).

All text in a snapshot taken by the real browser is rendered, visible text
(``forms._VISIBLE_TEXT_JS``): text that is off-screen, transparent, tiny,
clipped away, covered or the colour of its background is not in it, so such
text can never confirm a submission (or hide a challenge from the check).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PageSnapshot:
    url: str
    title: str = ""
    text: str = ""  # rendered, visible text of the whole page, truncated
    iframe_srcs: tuple[str, ...] = ()
    script_srcs: tuple[str, ...] = ()
    markers: tuple[str, ...] = ()  # CSS selectors of challenge widgets present in the DOM
    visible_password_fields: int = 0
    form_control_count: int = 0
    status: int | None = None  # HTTP status of the main document, when known
    #: Visible text of the main content (``<main>`` / ``[role=main]``, else the body
    #: without header/nav/footer). Empty and not ``main_measured`` = unknown: ``text`` is used.
    main_text: str = ""
    #: name/id of every *visible* form control outside search boxes and page chrome.
    visible_controls: tuple[str, ...] = ()
    #: ``main_text`` was measured by the browser: an empty value means "no visible main content".
    main_measured: bool = False


@dataclass(frozen=True)
class Challenge:
    kind: str  # captcha | cloudflare | human_verification | login | account_creation
    detail: str


@dataclass(frozen=True)
class Confirmation:
    url: str
    matched_text: str


# Widgets / providers. Matching a script is enough: invisible reCAPTCHA v3 and
# Turnstile still gate the submission, and we never try to get past them.
_CAPTCHA_SRC = re.compile(
    r"(google\.com/recaptcha|recaptcha\.net|gstatic\.com/recaptcha|hcaptcha\.com|challenges\.cloudflare\.com|"
    r"/cdn-cgi/challenge-platform/|arkoselabs\.com|funcaptcha\.com|captcha-delivery\.com|px-captcha|"
    r"perimeterx\.net/.*captcha|friendlycaptcha|mtcaptcha)",
    re.I,
)
_CAPTCHA_MARKERS = {
    ".g-recaptcha": "reCAPTCHA widget",
    ".grecaptcha-badge": "reCAPTCHA badge",
    "[name=\"g-recaptcha-response\"]": "reCAPTCHA response field",
    ".h-captcha": "hCaptcha widget",
    "[name=\"h-captcha-response\"]": "hCaptcha response field",
    ".cf-turnstile": "Cloudflare Turnstile widget",
    "[name=\"cf-turnstile-response\"]": "Cloudflare Turnstile response field",
    "#px-captcha": "PerimeterX challenge",
    "[data-sitekey]": "CAPTCHA site key",
}
_CLOUDFLARE_MARKERS = {
    "#challenge-form": "Cloudflare challenge form",
    "#challenge-running": "Cloudflare challenge",
    "#cf-challenge-running": "Cloudflare challenge",
    ".cf-browser-verification": "Cloudflare browser verification",
}
#: Every selector the snapshot script should look for.
MARKER_SELECTORS: tuple[str, ...] = tuple(_CAPTCHA_MARKERS) + tuple(_CLOUDFLARE_MARKERS)

_HUMAN_TEXT = re.compile(
    r"(verify (that )?you are (a )?human|verifying you are human|are you a robot|i'?m not a robot|"
    r"checking if the site connection is secure|checking your browser|press (&|and) hold|"
    r"complete the security check|confirm you are not a robot|unusual traffic from your)",
    re.I,
)
_CLOUDFLARE_TITLE = re.compile(r"^\s*(just a moment\.*|attention required!? \| cloudflare)\s*$", re.I)
_LOGIN_TEXT = re.compile(
    r"\b((sign|log) ?in|login) to (apply|continue)\b|\bplease (sign|log) ?in\b|\blogin required\b|"
    r"\byou must (be )?(signed|logged) in\b",
    re.I,
)
_ACCOUNT_TEXT = re.compile(
    r"\bcreate (an |your )?account to (apply|continue)\b|\b(register|sign up) to apply\b|"
    r"\byou (must|need to) (create|have) an account\b",
    re.I,
)
_ACCOUNT_WITH_PASSWORD = re.compile(r"\b(create (an |your )?account|sign up|register|confirm password)\b", re.I)

_CLOSED_TEXT = re.compile(
    r"(no longer (accepting applications|available|open|active)|"
    r"(job|position|posting|role|requisition|opening) (has been|is|was) (closed|filled|removed|no longer available)|"
    r"this (job|position|posting|opening) (has )?(expired|closed)|"
    r"(job|position|posting) (could not be|was not) found|the job you are looking for)",
    re.I,
)

#: Strong, past-tense confirmation wording only.
_CONFIRM_TEXT = re.compile(
    r"\b(?:thanks?(?: you)? for (?:applying|your application|submitting your application)"
    r"|(?:your )?application (?:has been|was) (?:successfully )?(?:received|submitted|sent)"
    r"|we(?:'|\u2019| ha)ve (?:successfully )?received your application"
    r"|application (?:successfully )?(?:received|submitted)(?=\s*(?:[.!]|$)))",
    re.I | re.M,
)
#: Wording of review steps, wizards and "not sent yet" pages. Any match anywhere
#: on the page means the submission is *not* confirmed.
_NOT_YET_TEXT = re.compile(
    r"\bbefore\b[^.!?\n]{0,80}?\b(?:submit\w*|sen[dt]\w*)\b"
    r"|\bwill\s+(?:only\s+|then\s+)?be\s+submitted\b"
    r"|\b(?:application|answers|information|details)\b[^.!?\n]{0,40}?\b(?:will|would)\s+(?:only\s+|then\s+)?be"
    r"\s+sent\b"
    r"|\bnot\s+(?:yet\s+)?(?:been\s+)?(?:submitted|sent)\b"
    r"|n['\u2019]t\s+(?:yet\s+)?(?:been\s+)?(?:submitted|sent)\b"
    r"|\bnothing\s+(?:has\s+been|was|is|will\s+be)\s+(?:sent|submitted)\b"
    r"|\byet\s+to\s+be\s+(?:submitted|sent)\b"
    r"|(?:^|[.!?:]\s+|\bplease\s+|\bcarefully\s+)review\s+(?:your|the)\s+"
    r"(?:application|answers|details|information|submission)\b"
    r"|\bconfirm\s+(?:your|the)\s+(?:details|information|application|answers|submission)\b"
    r"|\b(?:click|press|tap|select|hit)\s+(?:the\s+)?[\"'\u201c]?(?:submit|send|apply)\b"
    r"|\b(?:one\s+more|final|last)\s+step\b|\balmost\s+(?:done|there|finished)\b"
    r"|\bstep\s+\d+\s+(?:of|/)\s+\d+\b"
    r"|\bapplication\s+(?:is\s+)?(?:not\s+(?:yet\s+)?complete|incomplete)\b"
    r"|\bsaved\s+as\s+(?:a\s+)?draft\b",
    re.I | re.M,
)


def detect_challenge(snap: PageSnapshot) -> Challenge | None:
    """Return the first security challenge / login wall found, or None."""
    for src in (*snap.iframe_srcs, *snap.script_srcs):
        if _CAPTCHA_SRC.search(src or ""):
            return Challenge("captcha", f"page loads a CAPTCHA/anti-bot component ({_short(src)})")
    for sel in snap.markers:
        if sel in _CLOUDFLARE_MARKERS:
            return Challenge("cloudflare", _CLOUDFLARE_MARKERS[sel])
        if sel in _CAPTCHA_MARKERS:
            return Challenge("captcha", _CAPTCHA_MARKERS[sel])
    if _CLOUDFLARE_TITLE.search(snap.title or ""):
        return Challenge("cloudflare", f"Cloudflare interstitial ({snap.title.strip()!r})")
    m = _HUMAN_TEXT.search(snap.text or "")
    if m:
        return Challenge("human_verification", f"page asks to {m.group(0)!r}")
    if snap.visible_password_fields:
        if _ACCOUNT_WITH_PASSWORD.search(snap.text or ""):
            return Challenge("account_creation", "page requires creating an account (password field)")
        return Challenge("login", "page requires signing in (password field)")
    m = _ACCOUNT_TEXT.search(snap.text or "")
    if m:
        return Challenge("account_creation", f"page says {m.group(0)!r}")
    m = _LOGIN_TEXT.search(snap.text or "")
    if m:
        return Challenge("login", f"page says {m.group(0)!r}")
    return None


def detect_closed_listing(snap: PageSnapshot) -> str | None:
    if snap.status in {404, 410}:
        return f"the page returned HTTP {snap.status}"
    m = _CLOSED_TEXT.search(snap.text or "")
    if m:
        return f"page says {m.group(0)!r}"
    return None


def confirmation_wording(snap: PageSnapshot, baseline: PageSnapshot | None = None) -> str | None:
    """Strong confirmation wording in the main content that was not on the form page (context), or None."""
    base_text = ((baseline.text if baseline else "") or "").lower()
    text = snap.main_text if snap.main_measured else (snap.main_text or snap.text or "")
    for m in _CONFIRM_TEXT.finditer(text):
        if m.group(0).lower() not in base_text:
            return _context(text, m.start(), m.end())
    return None


def not_yet_submitted_wording(snap: PageSnapshot) -> str | None:
    """Review-step / wizard / "nothing has been sent" wording anywhere on the page, or None."""
    m = _NOT_YET_TEXT.search(snap.text or "") or _NOT_YET_TEXT.search(snap.main_text or "")
    return " ".join(m.group(0).split()) if m else None


def form_still_present(snap: PageSnapshot, baseline: PageSnapshot | None) -> bool:
    """Is a visible control of the baseline (application) form still shown?"""
    if baseline is None or not baseline.visible_controls:
        return False
    return bool(set(snap.visible_controls) & set(baseline.visible_controls))


def confirmation_problem(snap: PageSnapshot, baseline: PageSnapshot | None = None) -> str | None:
    """Why ``snap`` is not a credible confirmation (None = it is one)."""
    wording = not_yet_submitted_wording(snap)
    if wording:
        return f"the page says {wording!r} (an intermediate / review step)"
    if form_still_present(snap, baseline):
        return "the application form is still shown"
    if confirmation_wording(snap, baseline) is None:
        return "no confirmation wording on the page"
    return None


def detect_confirmation(snap: PageSnapshot, baseline: PageSnapshot | None = None) -> Confirmation | None:
    """A credible submission confirmation that was *not* already on the form page.

    Requires strong past-tense wording in the main content, no "not yet sent /
    review your application / before it is submitted" wording, and the
    application form gone. The URL is never enough.
    """
    if confirmation_problem(snap, baseline) is not None:
        return None
    matched = confirmation_wording(snap, baseline)
    return Confirmation(snap.url, matched) if matched else None


def _context(text: str, start: int, end: int, width: int = 60) -> str:
    lo = max(0, start - width)
    hi = min(len(text), end + width)
    return " ".join(text[lo:hi].split())[:240]


def _short(url: str, limit: int = 80) -> str:
    return url if len(url) <= limit else url[:limit] + "..."
