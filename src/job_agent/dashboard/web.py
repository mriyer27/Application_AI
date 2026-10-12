"""Request plumbing shared by the dashboard routes: templates, sessions, CSRF, contexts."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from job_agent.dashboard.auth import SESSION_COOKIE, Session, constant_time_equals
from job_agent.utils.time import parse_iso, to_local, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from job_agent.config import AppConfig
    from job_agent.context import AppContext

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"


class NotAuthenticated(Exception):
    """Raised by ``require_session``; turned into a redirect to /login (or 401 for /api)."""


# --------------------------------------------------------------------------- #
# Template filters
# --------------------------------------------------------------------------- #
def http_url(value: Any) -> str | None:
    """Return ``value`` only if it is an absolute http(s) URL without whitespace/control chars."""
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or len(url) > 2048 or any(ord(c) < 33 or c in "<>\"'`\\" for c in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc or parts.netloc.startswith("@"):
        return None
    return url


def _parse(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return parse_iso(value)
    except ValueError:
        return None


def make_templates(cfg: "AppConfig") -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,  # every {{ value }} is HTML-escaped
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    tz = cfg.app.timezone

    def localtime(value: Any, fmt: str = "%Y-%m-%d %H:%M") -> str:
        dt = _parse(value)
        if dt is None:
            return str(value) if value else "—"
        local = to_local(dt, tz)
        return local.strftime(fmt + " %Z") if local else "—"

    def ago(value: Any) -> str:
        dt = _parse(value)
        if dt is None:
            return "never"
        seconds = int((utcnow() - dt).total_seconds())
        future = seconds < 0
        seconds = abs(seconds)
        if seconds < 60:
            text = f"{seconds}s"
        elif seconds < 3600:
            text = f"{seconds // 60} min"
        elif seconds < 86400:
            text = f"{seconds // 3600} h"
        else:
            text = f"{seconds // 86400} d"
        return f"in {text}" if future else f"{text} ago"

    def humanize(value: Any) -> str:
        return str(value).replace("_", " ") if value is not None else "—"

    def score(value: Any) -> str:
        try:
            return f"{float(value):.0f}"
        except (TypeError, ValueError):
            return "—"

    def pretty_json(value: Any) -> str:
        return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, default=str)

    def is_mapping(value: Any) -> bool:
        return isinstance(value, dict)

    def is_sequence(value: Any) -> bool:
        return isinstance(value, (list, tuple))

    env.filters.update(localtime=localtime, ago=ago, humanize=humanize, score=score, pretty_json=pretty_json,
                       http_url=http_url)
    env.tests.update(mapping_value=is_mapping, sequence_value=is_sequence)
    env.globals.update(timezone=tz)
    return env


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #
def request_ctx(request: Request) -> Iterator["AppContext"]:
    """A per-request AppContext (own DB connection), closed after the request."""
    ctx = request.app.state.ctx_factory()
    try:
        yield ctx
    finally:
        ctx.close()


def require_session(request: Request) -> Session:
    session = request.app.state.sessions.load(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise NotAuthenticated()
    return session


async def csrf_session(request: Request, session: Session = Depends(require_session)) -> Session:
    """Authenticated session whose CSRF token matches the POSTed ``csrf_token`` field (else 403)."""
    form = await request.form()
    token = form.get("csrf_token")
    if not isinstance(token, str) or not constant_time_equals(token, session.csrf):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid. Reload the page and try again.")
    return session


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render(request: Request, template: str, context: dict[str, Any] | None = None, *,
           session: Session | None = None, ctx: "AppContext | None" = None, status_code: int = 200,
           section: str = "", headers: dict[str, str] | None = None) -> HTMLResponse:
    state = request.app.state
    nav_pending = 0
    if ctx is not None and session is not None:
        from job_agent.dashboard import queries

        nav_pending = queries.pending_approval_count(ctx.db)
    data: dict[str, Any] = {
        "section": section,
        "user": session.username if session else None,
        "csrf_token": session.csrf if session else "",
        "flashes": state.sessions.pop_flashes(session) if session else [],
        "nav_pending": nav_pending,
        "path": request.url.path,
    }
    data.update(context or {})
    html = state.templates.get_template(template).render(**data)
    return HTMLResponse(html, status_code=status_code, headers=headers)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def flash(request: Request, session: Session, level: str, message: str) -> None:
    request.app.state.sessions.flash(session, level, message)


def confirmed(value: str | None) -> bool:
    return value == "yes"
