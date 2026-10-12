"""Parse Gmail API message resources into a small in-memory structure.

The decoded body text lives only in ``ParsedEmail.body_text`` while a message
is being classified; it is never written to the database, logs or reports.
"""

from __future__ import annotations

import base64
import binascii
import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from job_agent.security.untrusted import clean_untrusted
from job_agent.utils.time import to_iso

SNIPPET_LIMIT = 200
SUBJECT_LIMIT = 300

_MULTI_PART_SLDS = {"co", "com", "ac", "org", "net", "gov", "edu", "ltd", "plc", "or", "ne", "go"}

#: Only authentication results written by Gmail's own receiving servers are trusted.
#: Anyone can put an ``Authentication-Results`` header into a message they send.
TRUSTED_AUTHSERV_IDS: frozenset[str] = frozenset({"mx.google.com"})


@dataclass(frozen=True)
class SenderAuth:
    """Whether Gmail verified that the message really comes from its From domain.

    ``authenticated`` is true only for ``dmarc=pass`` (``header.from`` aligned
    with the From domain) or ``dkim=pass`` with a signing domain aligned with the
    From domain, as reported by a trusted authserv-id. SPF alone never counts:
    it checks the envelope sender, not the From address you see.
    """

    authenticated: bool = False
    method: str | None = None  # "dmarc" | "dkim"
    source: str | None = None  # "authentication-results" | "arc-authentication-results"
    dkim: tuple[str, ...] = ()  # e.g. ("pass:acme.com", "fail:evil.example")
    spf: str | None = None
    dmarc: str | None = None
    reason: str = "no Authentication-Results from Gmail"

    @property
    def label(self) -> str:
        if self.authenticated:
            return f"verified ({self.method})"
        return f"not verified: {self.reason}"


UNAUTHENTICATED = SenderAuth()


@dataclass
class ParsedEmail:
    gmail_message_id: str
    gmail_thread_id: str
    received_at: str | None
    date_header_at: str | None
    from_name: str | None
    from_address: str | None
    from_domain: str | None
    subject: str
    snippet: str
    #: Plain text body, truncated. In memory only -- never persist this field.
    body_text: str = field(repr=False, default="")
    label_ids: tuple[str, ...] = ()
    bulk: bool = False  # List-Unsubscribe / Precedence: bulk / list headers present
    auth: SenderAuth = UNAUTHENTICATED  # DKIM / DMARC verdict from Gmail's headers

    @property
    def is_from_self(self) -> bool:
        return "SENT" in self.label_ids or "DRAFT" in self.label_ids

    @property
    def sender(self) -> str:
        if self.from_name and self.from_address:
            return f"{self.from_name} <{self.from_address}>"
        return self.from_address or self.from_name or ""


# --------------------------------------------------------------------------- #
# Headers
# --------------------------------------------------------------------------- #
def headers_of(payload: dict[str, Any] | None) -> dict[str, str]:
    """Header map with lowercase names (first occurrence wins)."""
    out: dict[str, str] = {}
    for h in (payload or {}).get("headers") or []:
        name = str(h.get("name") or "").lower()
        if name and name not in out:
            out[name] = str(h.get("value") or "")
    return out


def header_values(payload: dict[str, Any] | None, name: str) -> list[str]:
    """Every value of one header, top (most recently added) first."""
    wanted = name.lower()
    return [str(h.get("value") or "") for h in (payload or {}).get("headers") or []
            if str(h.get("name") or "").lower() == wanted]


_AR_COMMENT = re.compile(r"\([^()]*\)")
_AR_METHOD = re.compile(r"^\s*(dkim|spf|dmarc|arc)\s*=\s*([a-z]+)\b(.*)$", re.I | re.S)
_AR_PROP = re.compile(r"\b(header\.[a-z]+|smtp\.[a-z]+)\s*=\s*\"?([^\s;\"]+)\"?", re.I)
_ARC_INSTANCE = re.compile(r"^\s*i\s*=\s*(\d+)\s*$", re.I)


def _strip_comments(value: str) -> str:
    previous = None
    while previous != value:  # nested comments: strip innermost first
        previous, value = value, _AR_COMMENT.sub(" ", value)
    return value


def _parse_ar(value: str, *, arc: bool) -> tuple[int, str, list[tuple[str, str, dict[str, str]]]] | None:
    """``(arc instance, authserv-id, [(method, result, props)])`` of one (ARC-)Authentication-Results value."""
    segments = [s.strip() for s in _strip_comments(value).split(";")]
    instance = 0
    if arc:
        if not segments:
            return None
        match = _ARC_INSTANCE.match(segments[0])
        if not match:
            return None
        instance = int(match.group(1))
        segments = segments[1:]
    if not segments or not segments[0]:
        return None
    authserv = segments[0].split()[0].lower()
    results = []
    for seg in segments[1:]:
        match = _AR_METHOD.match(seg)
        if not match:
            continue
        props = {k.lower(): v.strip().lower() for k, v in _AR_PROP.findall(match.group(3))}
        results.append((match.group(1).lower(), match.group(2).lower(), props))
    return instance, authserv, results


def _aligned(domain: str | None, from_domain: str | None) -> bool:
    """Relaxed DMARC alignment: same registrable domain."""
    if not domain or not from_domain:
        return False
    domain = domain.lstrip("@").rsplit("@", 1)[-1].strip(".")
    return registrable_domain(domain) == registrable_domain(from_domain)


def sender_auth(payload: dict[str, Any] | None, from_domain: str | None) -> SenderAuth:
    """DKIM / DMARC verdict for the From domain, from Gmail's own Authentication-Results.

    Only the topmost ``Authentication-Results`` header is considered (Gmail
    prepends its own; anything below it came with the message and may be
    forged), and only when its authserv-id is Gmail's. Without it, the
    highest-instance ``ARC-Authentication-Results`` written by Gmail is used.
    """
    chosen: list[tuple[str, str, dict[str, str]]] | None = None
    source = None
    top = header_values(payload, "Authentication-Results")
    if top:
        parsed = _parse_ar(top[0], arc=False)
        if parsed and parsed[1] in TRUSTED_AUTHSERV_IDS:
            chosen, source = parsed[2], "authentication-results"
    if chosen is None:
        arcs = [p for p in (_parse_ar(v, arc=True) for v in header_values(payload, "ARC-Authentication-Results"))
                if p and p[1] in TRUSTED_AUTHSERV_IDS]
        if arcs:
            chosen, source = max(arcs, key=lambda p: p[0])[2], "arc-authentication-results"
    if chosen is None:
        reason = ("Authentication-Results not written by Gmail" if top else "no Authentication-Results from Gmail")
        return SenderAuth(reason=reason)

    dkim = tuple(f"{res}:{props.get('header.d') or props.get('header.i', '').lstrip('@') or '?'}"
                 for method, res, props in chosen if method == "dkim")
    spf = next((res for method, res, _ in chosen if method == "spf"), None)
    dmarc_entries = [(res, props) for method, res, props in chosen if method == "dmarc"]
    dmarc = dmarc_entries[0][0] if dmarc_entries else None
    base = dict(source=source, dkim=dkim, spf=spf, dmarc=dmarc)
    if not from_domain:
        return SenderAuth(reason="no sender address", **base)
    if dmarc and dmarc != "pass":
        return SenderAuth(reason=f"dmarc={dmarc}", **base)
    for res, props in dmarc_entries:
        if res == "pass" and _aligned(props.get("header.from"), from_domain):
            return SenderAuth(authenticated=True, method="dmarc", reason="", **base)
    for method, res, props in chosen:
        if method == "dkim" and res == "pass" and _aligned(props.get("header.d") or props.get("header.i"),
                                                           from_domain):
            return SenderAuth(authenticated=True, method="dkim", reason="", **base)
    summary = ", ".join([*(f"dkim={d}" for d in dkim), f"spf={spf or 'none'}", f"dmarc={dmarc or 'none'}"])
    return SenderAuth(reason=f"no DKIM/DMARC pass for {registrable_domain(from_domain)} ({summary})", **base)


def parse_from(value: str | None) -> tuple[str | None, str | None]:
    """``("Jane Doe", "jane@acme.com")`` from a From header value."""
    if not value:
        return None, None
    pairs = getaddresses([value])
    name, address = pairs[0] if pairs else ("", "")
    address = address.strip().strip("<>").lower() or None
    if address and "@" not in address:
        address = None
    name = clean_untrusted(name, max_chars=120) or None
    return name, address


def sender_domain(address: str | None) -> str | None:
    if not address or "@" not in address:
        return None
    domain = address.rsplit("@", 1)[1].strip().strip(">").strip(".").lower()
    return domain or None


def registrable_domain(domain: str | None) -> str | None:
    """Best-effort registrable domain: ``mail.jobs.acme.co.uk`` -> ``acme.co.uk``."""
    if not domain:
        return None
    labels = [p for p in domain.lower().strip(".").split(".") if p]
    if len(labels) < 2:
        return domain.lower() or None
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _MULTI_PART_SLDS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def domain_core(domain: str | None) -> str | None:
    """``acme-analytics.co.uk`` -> ``acmeanalytics`` (letters/digits only)."""
    reg = registrable_domain(domain)
    if not reg:
        return None
    first = reg.split(".")[0]
    core = re.sub(r"[^a-z0-9]", "", first)
    return core or None


def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return to_iso(dt)


def _internal_date(value: Any) -> str | None:
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    return to_iso(datetime.fromtimestamp(ms / 1000, tz=timezone.utc))


# --------------------------------------------------------------------------- #
# Body
# --------------------------------------------------------------------------- #
def decode_base64url(data: str | None) -> bytes:
    if not data:
        return b""
    text = data.strip().replace("\n", "")
    text += "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text)
    except (binascii.Error, ValueError):
        return b""


def _charset(part: dict[str, Any]) -> str:
    ctype = headers_of(part).get("content-type", "")
    match = re.search(r'charset="?([A-Za-z0-9_\-:.]+)"?', ctype, re.I)
    return match.group(1) if match else "utf-8"


def _decode_part(part: dict[str, Any]) -> str:
    raw = decode_base64url((part.get("body") or {}).get("data"))
    if not raw:
        return ""
    try:
        return raw.decode(_charset(part), errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _walk(part: dict[str, Any], plain: list[str], htmls: list[str]) -> None:
    mime = str(part.get("mimeType") or "").lower()
    if part.get("filename") or (part.get("body") or {}).get("attachmentId"):
        return  # attachments are never downloaded or read
    if mime.startswith("multipart/"):
        for child in part.get("parts") or []:
            _walk(child, plain, htmls)
        return
    if mime == "text/plain":
        plain.append(_decode_part(part))
    elif mime == "text/html":
        htmls.append(_decode_part(part))


class _TextExtractor(HTMLParser):
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "section",
              "article", "header", "footer", "blockquote"}
    _SKIP = {"script", "style", "head", "title", "noscript", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # malformed HTML: fall back to tag stripping
        return html.unescape(re.sub(r"<[^>]+>", " ", markup))
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_body(payload: dict[str, Any] | None, limit: int) -> str:
    """Plain-text body: text/plain parts preferred, else HTML converted to text."""
    if not payload:
        return ""
    plain: list[str] = []
    htmls: list[str] = []
    _walk(payload, plain, htmls)
    text = "\n".join(p for p in plain if p.strip())
    if not text.strip() and htmls:
        text = "\n".join(html_to_text(h) for h in htmls if h.strip())
    return clean_untrusted(text, max_chars=limit)


# --------------------------------------------------------------------------- #
def parse_message(message: dict[str, Any], *, body_char_limit: int) -> ParsedEmail:
    payload = message.get("payload") or {}
    headers = headers_of(payload)
    from_name, from_address = parse_from(headers.get("from"))
    date_header_at = parse_date(headers.get("date"))
    # internalDate is when Gmail received the message (not sender-controlled);
    # the Date header is used only when it is missing.
    received_at = _internal_date(message.get("internalDate")) or date_header_at
    subject = clean_untrusted(headers.get("subject", ""), max_chars=5_000).replace("\n", " ")[:SUBJECT_LIMIT]
    snippet = clean_untrusted(html.unescape(str(message.get("snippet") or "")), max_chars=5_000)[:SNIPPET_LIMIT]
    precedence = headers.get("precedence", "").lower()
    bulk = bool(headers.get("list-unsubscribe") or headers.get("list-id")) or precedence in {"bulk", "list", "junk"}
    from_domain = sender_domain(from_address)
    return ParsedEmail(
        gmail_message_id=str(message.get("id") or ""),
        gmail_thread_id=str(message.get("threadId") or message.get("id") or ""),
        received_at=received_at,
        date_header_at=date_header_at,
        from_name=from_name,
        from_address=from_address,
        from_domain=from_domain,
        subject=subject,
        snippet=snippet,
        body_text=extract_body(payload, body_char_limit),
        label_ids=tuple(str(x) for x in message.get("labelIds") or []),
        bulk=bulk,
        auth=sender_auth(payload, from_domain),
    )
