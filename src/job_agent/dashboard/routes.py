"""Dashboard pages and actions.

GET routes only read (``queries``). POST routes require an authenticated
session plus its CSRF token, and consequential ones also require a server-side
``confirm=yes`` field. Business logic lives in ``actions`` / other modules.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from job_agent.context import AppContext
from job_agent.dashboard import actions, queries
from job_agent.dashboard.actions import ActionRejected, FeatureUnavailable, NotFound
from job_agent.dashboard.auth import LOGIN_COOKIE, LOGIN_TOKEN_MAX_AGE, SESSION_COOKIE, Session, \
    constant_time_equals, safe_next
from job_agent.dashboard.markdown import render_markdown
from job_agent.dashboard.web import confirmed, csrf_session, flash, redirect, render, request_ctx, \
    require_session
from job_agent.database import repo
from job_agent.errors import JobAgentError, PermanentError
from job_agent.logging_config import get_logger
from job_agent.models import EmailCategory, MatchCategory, PipelineState
from job_agent.security.passwords import verify_password
from job_agent.security.sanitize import ensure_within

log = get_logger(__name__)
router = APIRouter()

CONFIRM_MESSAGE = "Confirmation required: tick the confirmation box and submit again."
_UID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")
_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._\-]+")
MAX_REPORT_MARKDOWN_BYTES = 2_000_000
DOCUMENT_MEDIA_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "md": "text/markdown; charset=utf-8",
}
REPORT_KINDS = {
    "xlsx": ("xlsx_file", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "md": ("markdown_file", "text/markdown; charset=utf-8"),
}


def _cookie_kwargs(request: Request) -> dict[str, Any]:
    cfg = request.app.state.config.dashboard
    return {"httponly": True, "samesite": "strict", "secure": bool(cfg.secure_cookies), "path": "/"}


def _bad_request(message: str, status_code: int = 400) -> HTTPException:
    return HTTPException(status_code=status_code, detail=message)


def _client_ip(request: Request) -> str:
    # Deliberately not X-Forwarded-For (spoofable). Behind `tailscale serve` all
    # requests come from 127.0.0.1, so the global limit is what protects you.
    return request.client.host if request.client else "unknown"


# --------------------------------------------------------------------------- #
# Login / logout
# --------------------------------------------------------------------------- #
def _login_page(request: Request, *, error: str | None = None, status_code: int = 200, next_url: str = "/",
                headers: dict[str, str] | None = None) -> Response:
    sessions = request.app.state.sessions
    token, cookie = sessions.new_login_token()
    response = render(request, "login.html", {"login_token": token, "error": error, "next": safe_next(next_url)},
                      status_code=status_code, headers=headers)
    response.set_cookie(LOGIN_COOKIE, cookie, max_age=LOGIN_TOKEN_MAX_AGE, **_cookie_kwargs(request))
    return response


@router.get("/login")
def login_form(request: Request, next: str = Query("/")) -> Response:
    if request.app.state.sessions.load(request.cookies.get(SESSION_COOKIE)) is not None:
        return redirect(safe_next(next))
    return _login_page(request, next_url=next)


@router.post("/login")
def login_submit(request: Request, ctx: AppContext = Depends(request_ctx), username: str = Form(""),
                 password: str = Form(""), csrf_token: str = Form(""), next: str = Form("/")) -> Response:
    state = request.app.state
    ip = _client_ip(request)
    if not state.limiter.attempt(ip):
        log.warning("dashboard login rate limit hit (client %s)", ip)
        return _login_page(request, error="Too many login attempts. Wait a minute and try again.",
                           status_code=429, next_url=next, headers={"Retry-After": "60"})
    if not state.sessions.check_login_token(request.cookies.get(LOGIN_COOKIE), csrf_token):
        raise HTTPException(status_code=403, detail="Login form expired or invalid. Reload the login page.")
    expected_user, password_hash = state.credentials
    user_ok = constant_time_equals(username[:256], expected_user)
    password_ok = verify_password(password[:1024], password_hash)  # always computed (timing)
    if not (user_ok and password_ok):
        repo.audit(ctx.db, actor="dashboard", action="dashboard_login", outcome="failure",
                   details={"client": ip})
        return _login_page(request, error="Invalid username or password.", status_code=401, next_url=next)
    session, cookie = state.sessions.create(expected_user)
    repo.audit(ctx.db, actor="dashboard", action="dashboard_login", outcome="success", details={"client": ip})
    response = redirect(safe_next(next))
    response.set_cookie(SESSION_COOKIE, cookie, max_age=state.sessions.max_age_seconds, **_cookie_kwargs(request))
    response.delete_cookie(LOGIN_COOKIE, path="/")
    return response


@router.post("/logout")
def logout(request: Request, session: Session = Depends(csrf_session)) -> Response:
    request.app.state.sessions.revoke(session.sid)
    response = redirect("/login")
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict",
                           secure=bool(request.app.state.config.dashboard.secure_cookies))
    return response


# --------------------------------------------------------------------------- #
# Overview + JSON status
# --------------------------------------------------------------------------- #
@router.get("/")
def overview(request: Request, session: Session = Depends(require_session),
             ctx: AppContext = Depends(request_ctx)) -> Response:
    db = ctx.db
    data = queries.overview(db, ctx.config)
    data["gmail"] = queries.gmail_status(db, ctx.config)
    health = queries.last_health(db)
    data["health"] = health if isinstance(health, dict) else None
    checks = data["health"].get("checks", []) if data["health"] else []
    data["health_problems"] = [c for c in checks if isinstance(c, dict) and c.get("status") in ("warn", "fail")]
    return render(request, "overview.html", data, session=session, ctx=ctx, section="overview")


@router.get("/api/status")
def api_status(session: Session = Depends(require_session), ctx: AppContext = Depends(request_ctx)) -> Response:
    db = ctx.db
    status = queries.agent_status(db)
    return JSONResponse({
        "paused": status["paused"],
        "daemon_running": status["daemon_running"],
        "heartbeat_at": status["heartbeat_at"],
        "last_success": status["last_success"],
        "next_run": status["next_run"],
        "pending_approvals": queries.pending_approval_count(db),
        "unresolved_errors": db.scalar("SELECT COUNT(*) FROM errors WHERE resolved = 0"),
        "gmail_connected": queries.gmail_status(db, ctx.config)["connected"],
    })


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
@router.get("/jobs")
def jobs(request: Request, q: str = "", category: str = "", state: str = "", source: str = "",
         sort: str = "score", page: str = "1", session: Session = Depends(require_session),
         ctx: AppContext = Depends(request_ctx)) -> Response:
    filters = queries.JobFilters(q=q, category=category, state=state, source=source, sort=sort,
                                 page=queries.clamp_page(page)).normalized()
    result = queries.list_jobs(ctx.db, filters)
    page_url = {n: "/jobs?" + urlencode(filters.query_params(page=n))
                for n in (result.page - 1, result.page + 1)}
    return render(request, "jobs.html", {
        "filters": filters, "result": result, "page_url": page_url, "sources": queries.job_sources(ctx.db),
        "categories": [c.value for c in MatchCategory], "states": [s.value for s in PipelineState],
        "sorts": list(queries.JOB_SORTS),
    }, session=session, ctx=ctx, section="jobs")


@router.get("/jobs/{job_id}")
def job_detail(request: Request, job_id: int, session: Session = Depends(require_session),
               ctx: AppContext = Depends(request_ctx)) -> Response:
    detail = queries.job_detail(ctx.db, job_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return render(request, "job_detail.html", detail, session=session, ctx=ctx, section="jobs")


@router.post("/jobs/{job_id}/relevance")
def job_relevance(request: Request, job_id: int, value: str = Form(""), session: Session = Depends(csrf_session),
                  ctx: AppContext = Depends(request_ctx)) -> Response:
    mapping = {"relevant": "relevant", "irrelevant": "irrelevant", "clear": None}
    if value not in mapping:
        raise _bad_request("Choose relevant, irrelevant or clear.")
    _run_action(lambda: actions.set_job_relevance(ctx.db, job_id, mapping[value]))
    flash(request, session, "ok", "Marked as " + (value if value != "clear" else "unrated") + ".")
    return redirect(f"/jobs/{job_id}")


@router.post("/jobs/{job_id}/notes")
def job_notes(request: Request, job_id: int, notes: str = Form(""), session: Session = Depends(csrf_session),
              ctx: AppContext = Depends(request_ctx)) -> Response:
    _run_action(lambda: actions.set_job_notes(ctx.db, job_id, notes))
    flash(request, session, "ok", "Notes saved.")
    return redirect(f"/jobs/{job_id}")


def _run_action(fn: Any) -> Any:
    """Map action-layer errors to HTTP errors (other JobAgentErrors propagate to the error page)."""
    try:
        return fn()
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ActionRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --------------------------------------------------------------------------- #
# Applications
# --------------------------------------------------------------------------- #
@router.get("/applications")
def applications(request: Request, status: str = "", page: str = "1", session: Session = Depends(require_session),
                 ctx: AppContext = Depends(request_ctx)) -> Response:
    result = queries.list_applications(ctx.db, status=status, page=queries.clamp_page(page))
    params = {"status": status} if status else {}
    page_url = {n: "/applications?" + urlencode({**params, "page": n}) for n in (result.page - 1, result.page + 1)}
    return render(request, "applications.html", {
        "result": result, "status": status if status in {s.value for s in PipelineState} else "",
        "states": [s.value for s in PipelineState], "page_url": page_url,
    }, session=session, ctx=ctx, section="applications")


@router.get("/applications/{application_id}")
def application_detail(request: Request, application_id: int, session: Session = Depends(require_session),
                       ctx: AppContext = Depends(request_ctx)) -> Response:
    detail = queries.application_detail(ctx.db, application_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Application not found.")
    return render(request, "application_detail.html", detail, session=session, ctx=ctx, section="applications")


# --------------------------------------------------------------------------- #
# Approvals
# --------------------------------------------------------------------------- #
@router.get("/approvals")
def approvals(request: Request, session: Session = Depends(require_session),
              ctx: AppContext = Depends(request_ctx)) -> Response:
    return render(request, "approvals.html", {
        "pending": queries.list_approvals(ctx.db, status="pending"),
        "recent": queries.list_approvals(ctx.db, status="decided", limit=20),
    }, session=session, ctx=ctx, section="approvals")


def _approval_or_404(ctx: AppContext, approval_uid: str) -> dict[str, Any]:
    detail = queries.approval_detail(ctx.db, approval_uid) if _UID_RE.match(approval_uid) else None
    if detail is None:
        raise HTTPException(status_code=404, detail="Approval request not found.")
    return detail


def _review_page(request: Request, session: Session, ctx: AppContext, approval_uid: str, *,
                 error: str | None = None, status_code: int = 200) -> Response:
    detail = _approval_or_404(ctx, approval_uid)
    return render(request, "approval_review.html", {**detail, "error": error}, session=session, ctx=ctx,
                  status_code=status_code, section="approvals")


@router.get("/approvals/{approval_uid}")
def approval_review(request: Request, approval_uid: str, session: Session = Depends(require_session),
                    ctx: AppContext = Depends(request_ctx)) -> Response:
    return _review_page(request, session, ctx, approval_uid)


@router.post("/approvals/{approval_uid}/decide")
def approval_decide(request: Request, approval_uid: str, decision: str = Form(""), confirm: str = Form(""),
                    note: str = Form(""), expected_payload_hash: str = Form(""),
                    session: Session = Depends(csrf_session), ctx: AppContext = Depends(request_ctx)) -> Response:
    _approval_or_404(ctx, approval_uid)
    if decision not in ("approve", "reject"):
        return _review_page(request, session, ctx, approval_uid, error="Choose Approve or Reject.", status_code=400)
    if not confirmed(confirm):
        return _review_page(request, session, ctx, approval_uid, error=CONFIRM_MESSAGE, status_code=400)
    try:
        handler = actions.decide_approval(ctx, approval_uid, approve=decision == "approve",
                                          note=note.strip()[:1000] or None,
                                          expected_payload_hash=expected_payload_hash.strip())
    except ActionRejected as exc:
        return _review_page(request, session, ctx, approval_uid, error=str(exc), status_code=400)
    except JobAgentError as exc:  # ApprovalError (changed / expired / already decided), InvalidTransition, ...
        log.info("approval %s decision refused: %s", approval_uid, exc)
        return _review_page(request, session, ctx, approval_uid, error=f"Not recorded: {exc}", status_code=409)
    log.info("approval %s %sd via dashboard (%s)", approval_uid, decision, handler)
    flash(request, session, "ok", "Approved. Nothing is submitted until the agent consumes this approval."
          if decision == "approve" else "Rejected.")
    return redirect(f"/approvals/{approval_uid}")


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
@router.get("/email")
def email(request: Request, category: str = "", attention: str = "", page: str = "1",
          session: Session = Depends(require_session), ctx: AppContext = Depends(request_ctx)) -> Response:
    attention_only = attention == "1"
    result = queries.list_emails(ctx.db, category=category, attention_only=attention_only,
                                 page=queries.clamp_page(page))
    params: dict[str, Any] = {}
    if category:
        params["category"] = category
    if attention_only:
        params["attention"] = "1"
    page_url = {n: "/email?" + urlencode({**params, "page": n}) for n in (result.page - 1, result.page + 1)}
    return render(request, "email.html", {
        "result": result, "category": category if category in {c.value for c in EmailCategory} else "",
        "attention_only": attention_only, "categories": [c.value for c in EmailCategory],
        "gmail": queries.gmail_status(ctx.db, ctx.config), "page_url": page_url,
        "associate_available": actions.module_available("job_agent.email.service"),
    }, session=session, ctx=ctx, section="email")


@router.post("/email/{email_id}/associate")
def email_associate(request: Request, email_id: int, job_ref: str = Form(""), application_ref: str = Form(""),
                    apply_status: str = Form(""), confirm: str = Form(""),
                    session: Session = Depends(csrf_session), ctx: AppContext = Depends(request_ctx)) -> Response:
    apply = apply_status == "yes"
    if apply and not confirmed(confirm):
        raise _bad_request(CONFIRM_MESSAGE)
    try:
        _run_action(lambda: actions.associate_email(ctx, email_id, job_ref=job_ref, application_ref=application_ref,
                                                    apply_status=apply))
    except FeatureUnavailable as exc:
        flash(request, session, "warn", str(exc))
        return redirect("/email")
    except JobAgentError as exc:  # refused by the email module (e.g. unknown reference)
        flash(request, session, "error", f"Not associated: {exc}")
        return redirect("/email")
    flash(request, session, "ok", "Email associated." + (" Status update requested." if apply else ""))
    return redirect("/email")


# --------------------------------------------------------------------------- #
# Follow-ups
# --------------------------------------------------------------------------- #
@router.get("/followups")
def followups(request: Request, show: str = "open", session: Session = Depends(require_session),
              ctx: AppContext = Depends(request_ctx)) -> Response:
    show = "all" if show == "all" else "open"
    return render(request, "followups.html", {
        "items": queries.list_followups(ctx.db, show=show), "show": show,
        "actions_available": actions.module_available("job_agent.followups.service"),
    }, session=session, ctx=ctx, section="followups")


@router.post("/followups/{followup_id}/status")
def followup_status(request: Request, followup_id: int, status: str = Form(""), confirm: str = Form(""),
                    session: Session = Depends(csrf_session), ctx: AppContext = Depends(request_ctx)) -> Response:
    if not confirmed(confirm):
        raise _bad_request(CONFIRM_MESSAGE)
    try:
        _run_action(lambda: actions.set_followup_status(ctx, followup_id, status))
    except FeatureUnavailable as exc:
        flash(request, session, "warn", str(exc))
        return redirect("/followups")
    except JobAgentError as exc:  # refused by the follow-ups module
        flash(request, session, "error", f"Not updated: {exc}")
        return redirect("/followups")
    flash(request, session, "ok", f"Follow-up marked {status}.")
    return redirect("/followups")


# --------------------------------------------------------------------------- #
# Reports + downloads
# --------------------------------------------------------------------------- #
@router.get("/reports")
def reports(request: Request, session: Session = Depends(require_session),
            ctx: AppContext = Depends(request_ctx)) -> Response:
    return render(request, "reports.html", {
        "reports": queries.list_reports(ctx.db), "trends": queries.trends(ctx.db, ctx.config),
    }, session=session, ctx=ctx, section="reports")


def _report_file(ctx: AppContext, report: dict[str, Any], column: str) -> Path | None:
    """Resolve a report file strictly inside reports_dir; None if absent/outside/missing."""
    value = report.get(column)
    if not value or not isinstance(value, str):
        return None
    try:
        path = ensure_within(ctx.config.reports_dir, Path(value))
    except PermanentError:
        log.warning("report %s: %s points outside the reports directory; refusing", report.get("id"), column)
        return None
    return path if path.is_file() else None


@router.get("/reports/{report_id}")
def report_detail(request: Request, report_id: int, session: Session = Depends(require_session),
                  ctx: AppContext = Depends(request_ctx)) -> Response:
    report = queries.get_report(ctx.db, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found.")
    md_path = _report_file(ctx, report, "markdown_file")
    summary_html = None
    if md_path is not None:
        with md_path.open("rb") as fh:
            raw = fh.read(MAX_REPORT_MARKDOWN_BYTES)
        summary_html = render_markdown(raw.decode("utf-8", errors="replace"))
    return render(request, "report_detail.html", {
        "report": report, "summary_html": summary_html,
        "has_xlsx": _report_file(ctx, report, "xlsx_file") is not None, "has_md": md_path is not None,
    }, session=session, ctx=ctx, section="reports")


def _download_name(name: str, fallback: str) -> str:
    cleaned = _FILENAME_SAFE.sub("_", Path(name or "").name).strip("._") or fallback
    return cleaned[:150]


@router.get("/reports/{report_id}/download/{kind}")
def report_download(report_id: int, kind: str, session: Session = Depends(require_session),
                    ctx: AppContext = Depends(request_ctx)) -> Response:
    if kind not in REPORT_KINDS:
        raise HTTPException(status_code=404, detail="Not found.")
    column, media_type = REPORT_KINDS[kind]
    report = queries.get_report(ctx.db, report_id)
    path = _report_file(ctx, report, column) if report else None
    if path is None:
        raise HTTPException(status_code=404, detail="Report file not found.")
    return FileResponse(path, media_type=media_type,
                        filename=_download_name(path.name, f"report_{report['report_date']}.{kind}"))


@router.get("/documents/{document_id}/download")
def document_download(document_id: int, session: Session = Depends(require_session),
                      ctx: AppContext = Depends(request_ctx)) -> Response:
    doc = queries.get_document(ctx.db, document_id)
    if doc is None or not doc.get("relative_path"):
        raise HTTPException(status_code=404, detail="Document not found.")
    try:
        path = ensure_within(ctx.config.documents_dir, Path(doc["relative_path"]))
    except PermanentError:
        log.warning("document %s points outside the documents directory; refusing", document_id)
        raise HTTPException(status_code=404, detail="Document not found.") from None
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Document file not found on disk.")
    fmt = doc.get("format") or ""
    return FileResponse(path, media_type=DOCUMENT_MEDIA_TYPES.get(fmt, "application/octet-stream"),
                        filename=_download_name(doc.get("file_name") or path.name, f"document_{document_id}.{fmt}"))


# --------------------------------------------------------------------------- #
# Controls
# --------------------------------------------------------------------------- #
@router.get("/controls")
def controls(request: Request, session: Session = Depends(require_session),
             ctx: AppContext = Depends(request_ctx)) -> Response:
    db = ctx.db
    runs = queries.recent_runs(db)
    for run in runs:
        run["retry_allowed"] = actions.retry_allowed(run)
    states = queries.task_states(db)
    task_list = actions.dashboard_tasks()
    for t in task_list:
        t["state"] = states.get(t["name"])
        t["locked"] = actions.task_locked(db, t["name"])
    return render(request, "controls.html", {
        "status": queries.agent_status(db), "tasks": task_list, "runs": runs,
        "errors": queries.unresolved_errors(db), "queued": request.app.state.task_runner.pending(),
    }, session=session, ctx=ctx, section="controls")


def _start_task(request: Request, session: Session, ctx: AppContext, name: str, *, attempt: int = 1) -> Response:
    try:
        spec = actions.triggerable_spec(name)
    except ActionRejected as exc:
        raise _bad_request(str(exc)) from exc
    if actions.task_locked(ctx.db, spec.name):
        raise _bad_request(f"{spec.name} is already running; wait for it to finish.", status_code=409)
    request.app.state.task_runner.submit(ctx, spec.name, attempt=attempt)
    flash(request, session, "ok", f"Started {spec.name} in the background. Refresh to see the run below.")
    return redirect("/controls")


@router.post("/controls/run")
def control_run(request: Request, task: str = Form(""), session: Session = Depends(csrf_session),
                ctx: AppContext = Depends(request_ctx)) -> Response:
    return _start_task(request, session, ctx, task)


@router.post("/controls/retry")
def control_retry(request: Request, run_uid: str = Form(""), session: Session = Depends(csrf_session),
                  ctx: AppContext = Depends(request_ctx)) -> Response:
    run = queries.get_run(ctx.db, run_uid) if _UID_RE.match(run_uid) else None
    if run is None:
        raise _bad_request("unknown run")
    if not actions.retry_allowed(run):
        raise _bad_request("only failed runs of retry-safe, dashboard-allowed tasks can be retried")
    return _start_task(request, session, ctx, run["task_name"], attempt=int(run.get("attempt") or 1) + 1)


@router.post("/controls/pause")
def control_pause(request: Request, confirm: str = Form(""), reason: str = Form(""),
                  session: Session = Depends(csrf_session), ctx: AppContext = Depends(request_ctx)) -> Response:
    if not confirmed(confirm):
        raise _bad_request(CONFIRM_MESSAGE)
    actions.set_paused(ctx.db, True, reason or "paused from dashboard")
    flash(request, session, "ok", "Automation paused. Scheduled tasks will be skipped until you resume.")
    return redirect("/controls")


@router.post("/controls/resume")
def control_resume(request: Request, confirm: str = Form(""), session: Session = Depends(csrf_session),
                   ctx: AppContext = Depends(request_ctx)) -> Response:
    if not confirmed(confirm):
        raise _bad_request(CONFIRM_MESSAGE)
    actions.set_paused(ctx.db, False)
    flash(request, session, "ok", "Automation resumed.")
    return redirect("/controls")


@router.post("/errors/{error_id}/resolve")
def error_resolve(request: Request, error_id: int, confirm: str = Form(""), session: Session = Depends(csrf_session),
                  ctx: AppContext = Depends(request_ctx)) -> Response:
    if not confirmed(confirm):
        raise _bad_request(CONFIRM_MESSAGE)
    if actions.resolve_error(ctx.db, error_id):
        flash(request, session, "ok", f"Error #{error_id} marked resolved.")
    else:
        flash(request, session, "warn", f"Error #{error_id} was not found or already resolved.")
    return redirect("/controls")
