"""FastAPI application factory for the dashboard."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from job_agent.context import AppContext
from job_agent.dashboard.actions import TaskRunner
from job_agent.dashboard.auth import LoginRateLimiter, SafeStaticFiles, SecurityMiddleware, SessionManager, \
    build_allowed_hosts
from job_agent.dashboard.web import STATIC_DIR, NotAuthenticated, make_templates, redirect, render, \
    require_session
from job_agent.errors import HumanActionRequired, JobAgentError
from job_agent.logging_config import get_logger

log = get_logger(__name__)

MIN_SESSION_SECRET_CHARS = 16
_STATUS_TITLES = {400: "Bad request", 401: "Not signed in", 403: "Forbidden", 404: "Not found",
                  405: "Method not allowed", 409: "Conflict", 429: "Too many requests", 500: "Error"}


def _check_credentials(ctx: AppContext) -> tuple[str, str, str]:
    s = ctx.secrets
    missing = [name for name, value in (("JOB_AGENT_DASHBOARD_USERNAME", s.dashboard_username),
                                        ("JOB_AGENT_DASHBOARD_PASSWORD_HASH", s.dashboard_password_hash),
                                        ("JOB_AGENT_SESSION_SECRET", s.session_secret)) if not value]
    if missing:
        raise HumanActionRequired(
            f"dashboard login is not configured (missing {', '.join(missing)}); refusing to start",
            human_action="Run `job-agent dashboard set-password` (it also creates the session secret).",
        )
    assert s.dashboard_username and s.dashboard_password_hash and s.session_secret
    parts = s.dashboard_password_hash.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        raise HumanActionRequired("JOB_AGENT_DASHBOARD_PASSWORD_HASH is not a job-agent scrypt hash",
                                  human_action="Run `job-agent dashboard set-password` again.")
    if len(s.session_secret) < MIN_SESSION_SECRET_CHARS:
        raise HumanActionRequired(
            f"JOB_AGENT_SESSION_SECRET is too short (< {MIN_SESSION_SECRET_CHARS} characters)",
            human_action="Delete it from .env and run `job-agent dashboard set-password` to generate a new one.",
        )
    return s.dashboard_username, s.dashboard_password_hash, s.session_secret


def create_app(ctx_factory: Callable[[], AppContext] | None = None) -> FastAPI:
    """Build the dashboard app.

    ``ctx_factory`` returns a fresh ``AppContext`` (own DB connection) per request;
    the default loads the context from the environment once and forks it per
    request. Raises ``HumanActionRequired`` if the login credentials or session
    secret are not configured.
    """
    if ctx_factory is None:
        base = AppContext.load()
        ctx_factory = base.fork
    probe = ctx_factory()
    try:
        cfg = probe.config
        username, password_hash, session_secret = _check_credentials(probe)
    finally:
        probe.close()

    runner = TaskRunner()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        runner.shutdown(wait_for_tasks=False)

    app = FastAPI(title="job-agent dashboard", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.ctx_factory = ctx_factory
    app.state.config = cfg
    app.state.credentials = (username, password_hash)
    app.state.sessions = SessionManager(session_secret, max_age_seconds=cfg.dashboard.session_max_age_minutes * 60)
    app.state.limiter = LoginRateLimiter(cfg.dashboard.login_attempts_per_minute)
    app.state.task_runner = runner
    app.state.templates = make_templates(cfg)

    app.add_middleware(SecurityMiddleware,
                       allowed_hosts=build_allowed_hosts(cfg.dashboard.host, cfg.dashboard.allowed_hosts))
    app.mount("/static", SafeStaticFiles(directory=STATIC_DIR), name="static")

    from job_agent.dashboard.routes import router

    app.include_router(router)
    _install_error_handlers(app)
    return app


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def _error_page(request: Request, status_code: int, message: str) -> Response:
    if _wants_json(request):
        return JSONResponse({"detail": message}, status_code=status_code)
    session = None
    try:
        session = require_session(request)
    except NotAuthenticated:
        pass
    return render(request, "error.html", {"status_code": status_code,
                                          "title": _STATUS_TITLES.get(status_code, "Error"), "message": message},
                  session=session, status_code=status_code)


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(NotAuthenticated)
    async def _not_authenticated(request: Request, exc: NotAuthenticated) -> Response:
        if _wants_json(request):
            return JSONResponse({"detail": "authentication required"}, status_code=401)
        if request.method == "GET":
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            from urllib.parse import quote

            return redirect("/login" + ("" if target == "/" else f"?next={quote(target, safe='')}"))
        return redirect("/login")

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        default = {404: "Not found.", 405: "Method not allowed."}.get(exc.status_code, "Request refused.")
        message = exc.detail if isinstance(exc.detail, str) and exc.detail not in ("Not Found",) else default
        response = _error_page(request, exc.status_code, message)
        for k, v in (exc.headers or {}).items():
            response.headers[k] = v
        return response

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> Response:
        # A malformed id in the URL is simply "not found"; malformed forms/queries are bad requests.
        if any(err.get("loc", ("",))[0] == "path" for err in exc.errors()):
            return _error_page(request, 404, "Not found.")
        return _error_page(request, 400, "Invalid request parameters.")

    @app.exception_handler(JobAgentError)
    async def _job_agent_error(request: Request, exc: JobAgentError) -> Response:
        log.error("dashboard request %s %s failed: %s", request.method, request.url.path, exc)
        message = str(exc) + (f" — {exc.human_action}" if exc.human_action else "")
        return _error_page(request, 500, message)
