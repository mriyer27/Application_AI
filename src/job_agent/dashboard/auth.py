"""Dashboard security primitives: sessions, CSRF, login rate limiting, Host checks,
security headers and a locked-down static file server.

Design notes
------------
* Sessions are an ``itsdangerous`` signed cookie carrying a random session id and a
  CSRF token. The id must also be present in an in-memory registry, so logging
  out revokes the session immediately and restarting the dashboard logs everyone
  out. Cookies are HttpOnly, SameSite=Strict and (optionally) Secure.
* Every state-changing POST must carry the session's CSRF token in a hidden form
  field. The login form, which has no session yet, uses a signed double-submit
  token instead.
* Login attempts are limited per client IP and globally (sliding 60 s window).
* The Host header must name the dashboard itself (DNS-rebinding protection).
"""

from __future__ import annotations

import hmac
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.datastructures import Headers
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SESSION_COOKIE = "ja_session"
LOGIN_COOKIE = "ja_login"
LOGIN_TOKEN_MAX_AGE = 3600
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
SECURITY_HEADERS: dict[str, str] = {
    "content-security-policy": CONTENT_SECURITY_POLICY,
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=()",
}

#: Global login attempts allowed per window = per-IP limit x this factor.
GLOBAL_LOGIN_FACTOR = 4


def constant_time_equals(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
@dataclass
class Session:
    sid: str
    csrf: str
    username: str
    created_at: float
    flashes: list[tuple[str, str]] = field(default_factory=list)


class SessionManager:
    """Signed session cookies backed by an in-memory registry (revocable)."""

    def __init__(self, secret: str, *, max_age_seconds: int, max_sessions: int = 32,
                 clock: Callable[[], float] = time.time) -> None:
        self._serializer = URLSafeTimedSerializer(secret, salt="job-agent.dashboard.session")
        self._login_serializer = URLSafeTimedSerializer(secret, salt="job-agent.dashboard.login")
        self.max_age_seconds = max_age_seconds
        self._max_sessions = max_sessions
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    # -- lifecycle --------------------------------------------------------- #
    def create(self, username: str) -> tuple[Session, str]:
        """Create a new session (fresh id: no session fixation). Returns (session, cookie value)."""
        session = Session(sid=secrets.token_urlsafe(32), csrf=secrets.token_urlsafe(32), username=username,
                          created_at=self._clock())
        with self._lock:
            self._prune_locked()
            while len(self._sessions) >= self._max_sessions:
                oldest = min(self._sessions.values(), key=lambda s: s.created_at)
                del self._sessions[oldest.sid]
            self._sessions[session.sid] = session
        cookie = self._serializer.dumps({"sid": session.sid, "csrf": session.csrf})
        return session, cookie

    def load(self, cookie_value: str | None) -> Session | None:
        if not cookie_value or len(cookie_value) > 4096:
            return None
        try:
            data = self._serializer.loads(cookie_value, max_age=self.max_age_seconds)
        except BadSignature:  # includes SignatureExpired
            return None
        if not isinstance(data, dict):
            return None
        sid, csrf = data.get("sid"), data.get("csrf")
        if not isinstance(sid, str) or not isinstance(csrf, str):
            return None
        with self._lock:
            session = self._sessions.get(sid)
            if session is None:
                return None
            if self._clock() - session.created_at > self.max_age_seconds:
                del self._sessions[sid]
                return None
        if not constant_time_equals(session.csrf, csrf):
            return None
        return session

    def revoke(self, sid: str) -> None:
        with self._lock:
            self._sessions.pop(sid, None)

    def active_count(self) -> int:
        with self._lock:
            self._prune_locked()
            return len(self._sessions)

    def _prune_locked(self) -> None:
        now = self._clock()
        for sid in [s.sid for s in self._sessions.values() if now - s.created_at > self.max_age_seconds]:
            del self._sessions[sid]

    # -- flash messages ------------------------------------------------------ #
    def flash(self, session: Session, level: str, message: str) -> None:
        with self._lock:
            session.flashes.append((level, message[:500]))
            del session.flashes[:-5]

    def pop_flashes(self, session: Session) -> list[tuple[str, str]]:
        with self._lock:
            out = list(session.flashes)
            session.flashes.clear()
            return out

    # -- login-form CSRF (double submit; no session exists yet) ------------- #
    def new_login_token(self) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        return token, self._login_serializer.dumps(token)

    def check_login_token(self, cookie_value: str | None, form_token: str | None) -> bool:
        if not cookie_value or not form_token or len(cookie_value) > 1024:
            return False
        try:
            expected = self._login_serializer.loads(cookie_value, max_age=LOGIN_TOKEN_MAX_AGE)
        except BadSignature:
            return False
        return isinstance(expected, str) and constant_time_equals(expected, form_token)


# --------------------------------------------------------------------------- #
# Login rate limiting
# --------------------------------------------------------------------------- #
class LoginRateLimiter:
    """Sliding-window limiter: ``per_key`` attempts per client and ``global_limit`` overall."""

    def __init__(self, per_key: int, *, global_limit: int | None = None, window_seconds: float = 60.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.per_key = per_key
        self.global_limit = global_limit if global_limit is not None else per_key * GLOBAL_LOGIN_FACTOR
        self.window = window_seconds
        self._clock = clock
        self._by_key: dict[str, deque[float]] = {}
        self._global: deque[float] = deque()
        self._lock = threading.Lock()

    def attempt(self, key: str) -> bool:
        """Record an attempt for ``key``. Returns False (and records nothing) when over a limit."""
        now = self._clock()
        cutoff = now - self.window
        with self._lock:
            while self._global and self._global[0] <= cutoff:
                self._global.popleft()
            q = self._by_key.setdefault(key, deque())
            while q and q[0] <= cutoff:
                q.popleft()
            for k in [k for k, v in self._by_key.items() if not v and k != key]:
                del self._by_key[k]
            if len(q) >= self.per_key or len(self._global) >= self.global_limit:
                return False
            q.append(now)
            self._global.append(now)
            return True


# --------------------------------------------------------------------------- #
# Host header allowlist (DNS-rebinding protection)
# --------------------------------------------------------------------------- #
_PORT_RE = re.compile(r"^[0-9]{1,5}$")


def host_without_port(value: str) -> str | None:
    """``"Example.ts.net:8765"`` -> ``"example.ts.net"``; ``"[::1]:8765"`` -> ``"::1"``.

    Returns None for a malformed value.
    """
    v = value.strip().lower()
    if not v or any(c.isspace() for c in v):
        return None
    if v.startswith("["):
        end = v.find("]")
        if end == -1:
            return None
        rest = v[end + 1:]
        if rest and not (rest.startswith(":") and _PORT_RE.match(rest[1:])):
            return None
        host = v[1:end]
    elif v.count(":") == 1:
        host, port = v.split(":", 1)
        if not _PORT_RE.match(port):
            return None
    else:
        host = v  # plain name or a bare IPv6 literal
    host = host.rstrip(".")
    return host or None


def build_allowed_hosts(bind_host: str, extra: Iterable[str]) -> frozenset[str]:
    hosts = set(LOOPBACK_HOSTS)
    for h in (bind_host, *extra):
        normalized = host_without_port(h)
        if normalized:
            hosts.add(normalized)
    return frozenset(hosts)


def is_loopback(host: str) -> bool:
    return host.strip().lower().strip("[]") in LOOPBACK_HOSTS


# --------------------------------------------------------------------------- #
# ASGI middleware: Host check + security headers
# --------------------------------------------------------------------------- #
class SecurityMiddleware:
    """Rejects unexpected Host headers (400) and adds security headers to every response."""

    def __init__(self, app: ASGIApp, *, allowed_hosts: frozenset[str], static_prefix: str = "/static/") -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts
        self.static_prefix = static_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = host_without_port(headers.get("host", ""))
        if scope["type"] == "websocket":  # the dashboard has no websockets
            await send({"type": "websocket.close", "code": 1008})
            return
        cacheable = scope.get("path", "").startswith(self.static_prefix)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw = [(k, v) for k, v in message.get("headers", [])
                       if k.decode("latin-1").lower() not in SECURITY_HEADERS
                       and (cacheable or k.decode("latin-1").lower() != "cache-control")]
                raw.extend((k.encode("latin-1"), v.encode("latin-1")) for k, v in SECURITY_HEADERS.items())
                if not cacheable:
                    raw.append((b"cache-control", b"no-store"))
                    raw.append((b"pragma", b"no-cache"))
                message["headers"] = raw
            await send(message)

        if host is None or host not in self.allowed_hosts:
            body = b"Bad Request: unexpected Host header"
            await send_with_headers({
                "type": "http.response.start",
                "status": 400,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", str(len(body)).encode())],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send_with_headers)


# --------------------------------------------------------------------------- #
# Static files: package directory only, known asset types only, no dotfiles
# --------------------------------------------------------------------------- #
STATIC_SUFFIXES = frozenset({".css", ".js", ".svg", ".png", ".ico", ".webp", ".woff2", ".txt"})


class SafeStaticFiles(StaticFiles):
    """``StaticFiles`` that refuses dotfiles and any non-asset extension (e.g. ``.db``, ``.env``)."""

    def __init__(self, *, directory: Path) -> None:
        super().__init__(directory=str(directory), follow_symlink=False, check_dir=True)

    def lookup_path(self, path: str) -> tuple[str, Any]:
        parts = Path(path).parts
        if any(p.startswith(".") for p in parts) or Path(path).suffix.lower() not in STATIC_SUFFIXES:
            return "", None
        return super().lookup_path(path)


# --------------------------------------------------------------------------- #
# Redirect targets
# --------------------------------------------------------------------------- #
_SAFE_NEXT_RE = re.compile(r"^/[A-Za-z0-9/_\-.?=&%+]*$")


def safe_next(target: str | None) -> str:
    """Only same-site relative paths are allowed as post-login redirects."""
    if not target or len(target) > 512 or not _SAFE_NEXT_RE.match(target):
        return "/"
    if target.startswith("//") or target.startswith("/login") or target.startswith("/logout"):
        return "/"
    return target
