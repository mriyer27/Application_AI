"""Dashboard tests: authentication, CSRF, rate limiting, headers, approvals, rendering, downloads."""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from job_agent import tasks
from job_agent.config import Secrets
from job_agent.context import AppContext
from job_agent.dashboard import queries
from job_agent.dashboard.app import create_app
from job_agent.dashboard.auth import CONTENT_SECURITY_POLICY, LoginRateLimiter, SafeStaticFiles, SessionManager, \
    host_without_port, safe_next
from job_agent.dashboard.markdown import render_markdown
from job_agent.database import repo
from job_agent.database.connection import to_json
from job_agent.errors import HumanActionRequired
from job_agent.models import ApprovalType, RunStatus
from job_agent.security.passwords import hash_password
from job_agent.utils.time import utcnow_iso

USERNAME = "owner"
PASSWORD = "correct horse battery staple"
PASSWORD_HASH = hash_password(PASSWORD)
SESSION_SECRET = "test-session-secret-0123456789abcdef"
BASE_URL = "http://127.0.0.1:8765"
PACKAGE_DIR = Path(__file__).resolve().parents[2] / "src" / "job_agent" / "dashboard"

PAGES = ["/", "/jobs", "/applications", "/approvals", "/email", "/followups", "/reports", "/controls"]


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def dash_secrets() -> Secrets:
    return Secrets(dashboard_username=USERNAME, dashboard_password_hash=PASSWORD_HASH, session_secret=SESSION_SECRET)


@pytest.fixture
def no_service_modules(monkeypatch):
    """Make the optional contract modules unimportable so tests are deterministic."""
    for name in ("job_agent.applications.service", "job_agent.email.service", "job_agent.followups.service"):
        monkeypatch.setitem(sys.modules, name, None)


@pytest.fixture
def make_app(config, dash_secrets, no_service_modules):
    created = []

    def _make(cfg=None, secrets=None):
        cfg = cfg or config
        secrets = secrets or dash_secrets
        app = create_app(lambda: AppContext.from_config(cfg, secrets))
        created.append(app)
        return app

    yield _make
    for app in created:
        app.state.task_runner.shutdown(wait_for_tasks=True)


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, base_url=BASE_URL)


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "no csrf token in page"
    return match.group(1)


def login(client: TestClient, username: str = USERNAME, password: str = PASSWORD, next_url: str = "/"):
    page = client.get("/login")
    token = csrf_from(page.text)
    return client.post("/login", data={"username": username, "password": password, "csrf_token": token,
                                       "next": next_url}, follow_redirects=False)


@pytest.fixture
def auth_client(client) -> TestClient:
    resp = login(client)
    assert resp.status_code == 303, resp.text
    return client


def session_csrf(client: TestClient) -> str:
    return csrf_from(client.get("/controls").text)


def make_pending_approval(db, make_job, payload=None):
    job_id = make_job()
    app_id = repo.create_application(db, job_id, actor="system")
    payload = payload or {
        "employer": "Acme Analytics", "role": "Business Analyst",
        "application_url": "https://jobs.example.com/apply/1",
        "answers": [{"label": "How did you hear about us?", "answer": "Company careers page"}],
        "legal_declarations": [{"question": "Will you require sponsorship?", "answer": "Yes (user-provided)"}],
        "unanswered_required": [], "validation_errors": [],
    }
    uid = repo.create_approval(db, ApprovalType.SUBMIT_APPLICATION, payload, summary="Submit Business Analyst @ Acme",
                               actor="system", application_id=app_id)
    return uid, repo.get_approval(db, uid)["payload_hash"], app_id


def seed_everything(db, make_job, config) -> dict:
    now = utcnow_iso()
    job_id = make_job(title="Data Analyst", employer="Seeded Corp", sponsorship_info="We do not sponsor visas.")
    db.insert("job_scores", {
        "job_id": job_id, "scoring_version": "v1", "profile_hash": "p", "config_hash": "c", "content_hash": "c1",
        "overall_score": 82.4, "category": "strong_match", "shortlisted": 1,
        "factors": to_json([{"name": "skills", "weight": 25, "score": 0.8, "explanation": "SQL and Python match"}]),
        "matching_qualifications": to_json(["SQL"]), "missing_qualifications": to_json(["Tableau certification"]),
        "eligibility_flags": to_json([{"code": "no_sponsorship", "severity": "review",
                                       "excerpt": "We do not sponsor visas.", "message": "Listing says no sponsorship"}]),
        "evidence": to_json([{"requirement": "SQL", "fact_id": "agency_dashboards", "fact_text": "Supported dashboards"}]),
        "reasons_to_apply": to_json(["Strong analytics overlap"]), "reasons_not_to_apply": to_json(["Sponsorship"]),
        "recommended_next_action": "Review sponsorship statement", "scored_at": now,
    })
    db.insert("llm_analyses", {"job_id": job_id, "purpose": "job_analysis", "model": "qwen", "prompt_version": "1",
                               "input_hash": "x", "output": to_json({"fit_explanation": "Good analytics fit.",
                                                                     "summary": "Analyst role", "red_flags": []}),
                               "created_at": now})
    app_id = repo.create_application(db, job_id, actor="system", application_url="https://jobs.example.com/a")
    repo.add_application_event(db, app_id, "form_prepared", actor="browser", details={"fields": 3})
    db.insert("application_answers", {"application_id": app_id, "field_key": "sponsor", "field_label":
                                      "Do you require sponsorship?", "field_type": "select", "required": 1,
                                      "answer": None, "confidence": "unknown", "requires_review": 1,
                                      "is_legal_declaration": 1, "review_reason": "legal", "updated_at": now})
    doc_rel = Path("seeded") / "Seeded-Corp_Data-Analyst_Resume_2026-10-12.pdf"
    (config.documents_dir / doc_rel).parent.mkdir(parents=True, exist_ok=True)
    (config.documents_dir / doc_rel).write_bytes(b"%PDF-1.4 seeded")
    doc_id = db.insert("document_versions", {
        "job_id": job_id, "doc_type": "resume", "version": 1, "format": "pdf", "file_name": doc_rel.name,
        "relative_path": str(doc_rel), "sha256": "0" * 64, "generator": "rules", "profile_hash": "p",
        "created_at": now})
    db.execute("UPDATE applications SET resume_document_id = ? WHERE id = ?", (doc_id, app_id))
    approval_uid = repo.create_approval(db, ApprovalType.SUBMIT_APPLICATION, {"employer": "Seeded Corp"},
                                        summary="Submit Data Analyst @ Seeded Corp", actor="system",
                                        application_id=app_id)
    email_id = db.insert("email_messages", {
        "gmail_message_id": "m1", "gmail_thread_id": "t1", "received_at": now, "from_address": "recruiter@seeded.example",
        "subject": "Interview invitation", "category": "interview_invitation", "category_confidence": 0.9,
        "classifier": "rules", "summary": "Wants to schedule a call", "requires_attention": 1,
        "suggested_reply": "Thank you, I am available Tuesday.", "processed_at": now})
    db.insert("email_associations", {"email_id": email_id, "application_id": app_id, "method": "domain",
                                     "confidence": 0.8, "created_at": now})
    followup_id = db.insert("followups", {
        "application_id": app_id, "followup_type": "recruiter_reply", "due_at": now, "dedupe_key": "f1",
        "suggested_action": "Reply to the recruiter", "draft_text": "Hello, following up.", "created_at": now,
        "updated_at": now})
    xlsx = config.reports_dir / "report_2026-10-11.xlsx"
    md = config.reports_dir / "report_2026-10-11.md"
    xlsx.write_bytes(b"PK fake xlsx")
    md.write_text("# Daily report\n\n**3** new jobs\n\n- <script>alert(1)</script>\n\n| a | b |\n|---|---|\n| 1 | 2 |\n",
                  encoding="utf-8")
    report_id = db.insert("reports", {"report_date": "2026-10-11", "xlsx_file": xlsx.name, "markdown_file": md.name,
                                      "stats": to_json({"new_jobs": 3}), "first_generated_at": now,
                                      "last_generated_at": now})
    run_id, run_uid = repo.start_run(db, "health", "scheduled")
    repo.finish_run(db, run_id, RunStatus.FAILED, error_message="boom")
    error_id = repo.record_error(db, component="discovery:greenhouse", error="HTTP 503", task_name="discover")
    repo.set_state(db, "daemon_heartbeat_at", now)
    db.execute("INSERT INTO scheduled_tasks (task_name, enabled, next_run_at, updated_at) VALUES (?, 1, ?, ?)",
               ("report", "2099-01-01T00:00:00+00:00", now))
    return {"job_id": job_id, "app_id": app_id, "doc_id": doc_id, "approval_uid": approval_uid, "email_id": email_id,
            "followup_id": followup_id, "report_id": report_id, "run_uid": run_uid, "error_id": error_id}


# --------------------------------------------------------------------------- #
# create_app preconditions
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("missing", ["dashboard_username", "dashboard_password_hash", "session_secret"])
def test_create_app_refuses_without_credentials(config, missing):
    values = {"dashboard_username": USERNAME, "dashboard_password_hash": PASSWORD_HASH,
              "session_secret": SESSION_SECRET}
    values[missing] = None
    secrets = Secrets(**values)
    with pytest.raises(HumanActionRequired):
        create_app(lambda: AppContext.from_config(config, secrets))


def test_create_app_refuses_bad_hash_and_short_secret(config):
    for secrets in (Secrets(dashboard_username=USERNAME, dashboard_password_hash="plaintext",
                            session_secret=SESSION_SECRET),
                    Secrets(dashboard_username=USERNAME, dashboard_password_hash=PASSWORD_HASH, session_secret="x")):
        with pytest.raises(HumanActionRequired):
            create_app(lambda s=secrets: AppContext.from_config(config, s))


# --------------------------------------------------------------------------- #
# authentication
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", PAGES + ["/jobs/1", "/reports/1/download/xlsx", "/documents/1/download"])
def test_unauthenticated_pages_redirect_to_login(client, path):
    resp = client.get(path, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_unauthenticated_api_is_401(client):
    resp = client.get("/api/status")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "authentication required"}


def test_unauthenticated_post_does_not_act(client, db):
    resp = client.post("/controls/pause", data={"confirm": "yes", "csrf_token": "x"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/login"
    assert not repo.is_paused(db)


def test_login_success_sets_hardened_cookie(client):
    resp = login(client)
    assert resp.status_code == 303 and resp.headers["location"] == "/"
    cookie = next(v for k, v in resp.headers.multi_items() if k == "set-cookie" and v.startswith("ja_session="))
    lowered = cookie.lower()
    assert "httponly" in lowered and "samesite=strict" in lowered and "max-age=43200" in lowered
    assert "secure" not in lowered.replace("samesite", "")
    assert client.get("/").status_code == 200
    assert client.get("/api/status").json()["paused"] is False


def test_secure_cookie_flag_when_configured(make_app, config):
    cfg = config.model_copy(deep=True)
    cfg.dashboard.secure_cookies = True
    client = TestClient(make_app(cfg=cfg), base_url="https://127.0.0.1:8765")
    resp = login(client)
    cookie = next(v for k, v in resp.headers.multi_items() if k == "set-cookie" and v.startswith("ja_session="))
    assert "; secure" in cookie.lower()


def test_wrong_password_and_wrong_username(client, db):
    for user, pw in ((USERNAME, "wrong password!!"), ("intruder", PASSWORD), ("", "")):
        resp = login(client, user, pw)
        assert resp.status_code == 401
        assert "Invalid username or password" in resp.text
        assert "ja_session=" not in resp.headers.get("set-cookie", "")
    assert client.get("/", follow_redirects=False).status_code == 303
    assert db.scalar("SELECT COUNT(*) FROM audit_events WHERE action = 'dashboard_login' AND outcome = 'failure'") == 3


def test_login_requires_login_csrf(client):
    client.get("/login")
    resp = client.post("/login", data={"username": USERNAME, "password": PASSWORD, "csrf_token": "forged"},
                       follow_redirects=False)
    assert resp.status_code == 403
    fresh = TestClient(client.app, base_url=BASE_URL)  # no login cookie at all
    resp = fresh.post("/login", data={"username": USERNAME, "password": PASSWORD, "csrf_token": "x"})
    assert resp.status_code == 403


def test_login_rate_limit_returns_429(client, config):
    limit = config.dashboard.login_attempts_per_minute
    for _ in range(limit):
        assert login(client, USERNAME, "wrong password!!").status_code == 401
    resp = login(client)  # even the right password is refused while limited
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "60"
    assert client.get("/", follow_redirects=False).status_code == 303


def test_rate_limiter_per_ip_global_and_window():
    now = [0.0]
    limiter = LoginRateLimiter(2, global_limit=3, window_seconds=60, clock=lambda: now[0])
    assert limiter.attempt("a") and limiter.attempt("a")
    assert not limiter.attempt("a")  # per-IP
    assert limiter.attempt("b")
    assert not limiter.attempt("c")  # global
    now[0] = 61.0
    assert limiter.attempt("a") and limiter.attempt("c")


def test_logout_revokes_session(auth_client):
    token = session_csrf(auth_client)
    stolen = auth_client.cookies.get("ja_session")
    assert auth_client.post("/logout", data={}, follow_redirects=False).status_code == 403  # CSRF required
    resp = auth_client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/login"
    replay = TestClient(auth_client.app, base_url=BASE_URL, cookies={"ja_session": stolen})
    assert replay.get("/", follow_redirects=False).status_code == 303


def test_tampered_session_cookie_rejected(app):
    c = TestClient(app, base_url=BASE_URL, cookies={"ja_session": "eyJzaWQiOiJ4In0.forged.sig"})
    assert c.get("/", follow_redirects=False).status_code == 303


def test_session_expiry_in_registry():
    now = [1000.0]
    manager = SessionManager("s" * 32, max_age_seconds=60, clock=lambda: now[0])
    session, cookie = manager.create("owner")
    assert manager.load(cookie) is session
    now[0] += 61
    assert manager.load(cookie) is None


def test_next_redirect_is_same_site_only(client):
    resp = login(client, next_url="//evil.example/x")
    assert resp.headers["location"] == "/"
    assert safe_next("/jobs?q=x") == "/jobs?q=x"
    assert safe_next("https://evil.example") == "/"
    assert safe_next("/\\evil") == "/"


# --------------------------------------------------------------------------- #
# CSRF + confirmation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("token", [None, "", "wrong-token"])
def test_csrf_missing_or_wrong_is_403(auth_client, db, token):
    data = {"confirm": "yes"}
    if token is not None:
        data["csrf_token"] = token
    resp = auth_client.post("/controls/pause", data=data)
    assert resp.status_code == 403
    assert not repo.is_paused(db)


def test_pause_requires_confirmation_then_works(auth_client, db):
    token = session_csrf(auth_client)
    resp = auth_client.post("/controls/pause", data={"csrf_token": token})
    assert resp.status_code == 400 and not repo.is_paused(db)
    resp = auth_client.post("/controls/pause", data={"csrf_token": token, "confirm": "yes", "reason": "travel"},
                            follow_redirects=False)
    assert resp.status_code == 303
    assert repo.is_paused(db)
    assert db.scalar("SELECT actor FROM audit_events WHERE action = 'automation_paused'") == "dashboard"
    resp = auth_client.post("/controls/resume", data={"csrf_token": token, "confirm": "yes"}, follow_redirects=False)
    assert resp.status_code == 303 and not repo.is_paused(db)


def test_resolve_error_requires_confirm(auth_client, db):
    error_id = repo.record_error(db, component="x", error="something broke")
    token = session_csrf(auth_client)
    assert auth_client.post(f"/errors/{error_id}/resolve", data={"csrf_token": token}).status_code == 400
    assert db.scalar("SELECT resolved FROM errors WHERE id = ?", (error_id,)) == 0
    resp = auth_client.post(f"/errors/{error_id}/resolve", data={"csrf_token": token, "confirm": "yes"})
    assert resp.status_code == 200 and "marked resolved" in resp.text
    assert db.scalar("SELECT resolved FROM errors WHERE id = ?", (error_id,)) == 1
    assert db.scalar("SELECT actor FROM audit_events WHERE action = 'error_resolved'") == "dashboard"


# --------------------------------------------------------------------------- #
# approvals
# --------------------------------------------------------------------------- #
def test_approval_review_shows_payload_and_hash(auth_client, db, make_job):
    uid, phash, _ = make_pending_approval(db, make_job)
    page = auth_client.get(f"/approvals/{uid}")
    assert page.status_code == 200
    assert f'name="expected_payload_hash" value="{phash}"' in page.text
    assert "Legal declarations" in page.text and "Will you require sponsorship?" in page.text
    assert 'href="https://jobs.example.com/apply/1" rel="noopener noreferrer" target="_blank"' in page.text
    assert uid in auth_client.get("/approvals").text


def test_approve_with_confirm_and_hash(auth_client, db, make_job):
    uid, phash, _ = make_pending_approval(db, make_job)
    token = session_csrf(auth_client)
    resp = auth_client.post(f"/approvals/{uid}/decide", data={
        "csrf_token": token, "decision": "approve", "confirm": "yes", "expected_payload_hash": phash,
        "note": "looks right"}, follow_redirects=False)
    assert resp.status_code == 303
    row = repo.get_approval(db, uid)
    assert row["status"] == "approved" and row["decided_by"] == "dashboard"
    audit = db.query_one("SELECT * FROM audit_events WHERE action = 'approval_decided' AND entity_id = ?", (uid,))
    assert audit["actor"] == "dashboard" and audit["outcome"] == "approved"
    page = auth_client.get(f"/approvals/{uid}")
    assert "Approved." in page.text and 'value="approve"' not in page.text  # no form once decided


def test_reject_flow(auth_client, db, make_job):
    uid, phash, _ = make_pending_approval(db, make_job)
    token = session_csrf(auth_client)
    auth_client.post(f"/approvals/{uid}/decide", data={"csrf_token": token, "decision": "reject", "confirm": "yes",
                                                      "expected_payload_hash": phash})
    assert repo.get_approval(db, uid)["status"] == "rejected"


def test_tampered_expected_hash_is_refused(auth_client, db, make_job):
    uid, phash, _ = make_pending_approval(db, make_job)
    token = session_csrf(auth_client)
    tampered = ("0" if phash[0] != "0" else "1") + phash[1:]
    resp = auth_client.post(f"/approvals/{uid}/decide", data={
        "csrf_token": token, "decision": "approve", "confirm": "yes", "expected_payload_hash": tampered})
    assert resp.status_code == 409
    assert "changed since you reviewed it" in resp.text
    assert repo.get_approval(db, uid)["status"] == "pending"
    for bad in ("", "not-a-hash"):
        resp = auth_client.post(f"/approvals/{uid}/decide", data={
            "csrf_token": token, "decision": "approve", "confirm": "yes", "expected_payload_hash": bad})
        assert resp.status_code == 400
    assert repo.get_approval(db, uid)["status"] == "pending"


def test_approval_requires_confirm_checkbox(auth_client, db, make_job):
    uid, phash, _ = make_pending_approval(db, make_job)
    token = session_csrf(auth_client)
    resp = auth_client.post(f"/approvals/{uid}/decide", data={
        "csrf_token": token, "decision": "approve", "expected_payload_hash": phash})
    assert resp.status_code == 400 and "Confirmation required" in resp.text
    assert repo.get_approval(db, uid)["status"] == "pending"


def test_approval_csrf_required(auth_client, db, make_job):
    uid, phash, _ = make_pending_approval(db, make_job)
    resp = auth_client.post(f"/approvals/{uid}/decide", data={
        "decision": "approve", "confirm": "yes", "expected_payload_hash": phash})
    assert resp.status_code == 403
    assert repo.get_approval(db, uid)["status"] == "pending"


def test_approval_prefers_applications_service(auth_client, db, make_job, monkeypatch):
    calls = []

    def decide(ctx, approval_uid, *, approve, actor, note, expected_payload_hash):
        calls.append((approval_uid, approve, actor, expected_payload_hash))
        repo.decide_approval(ctx.db, approval_uid, approve=approve, actor=actor, note=note,
                             expected_payload_hash=expected_payload_hash)

    fake = types.ModuleType("job_agent.applications.service")
    fake.decide = decide
    monkeypatch.setitem(sys.modules, "job_agent.applications.service", fake)
    if "job_agent.applications" not in sys.modules:
        monkeypatch.setitem(sys.modules, "job_agent.applications", types.ModuleType("job_agent.applications"))
    uid, phash, _ = make_pending_approval(db, make_job)
    token = session_csrf(auth_client)
    auth_client.post(f"/approvals/{uid}/decide", data={"csrf_token": token, "decision": "approve",
                                                      "confirm": "yes", "expected_payload_hash": phash})
    assert calls == [(uid, True, "dashboard", phash)]
    assert repo.get_approval(db, uid)["status"] == "approved"


def test_unknown_approval_404(auth_client):
    assert auth_client.get("/approvals/AP-doesnotexist").status_code == 404
    assert auth_client.get("/approvals/..%2F..%2Fetc").status_code == 404


# --------------------------------------------------------------------------- #
# XSS / links
# --------------------------------------------------------------------------- #
def test_xss_in_job_title_is_escaped(auth_client, make_job):
    job_id = make_job(title="<script>alert(1)</script>", employer="<img src=x onerror=alert(2)>",
                      description_text="<b>bold</b><script>alert(3)</script>")
    for path in ("/jobs", f"/jobs/{job_id}", "/jobs?q=%3Cscript%3E"):
        html = auth_client.get(path).text
        assert "<script>alert(1)</script>" not in html
        assert "<img src=x" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    detail = auth_client.get(f"/jobs/{job_id}").text
    assert "<script>alert(3)</script>" not in detail and "<b>bold</b>" not in detail


def test_javascript_urls_not_rendered_as_links(auth_client, make_job):
    job_id = make_job(job_url="javascript:alert(1)", job_url_canonical=None,
                      application_url="  JaVaScRiPt:alert(2)")
    html = auth_client.get(f"/jobs/{job_id}").text
    assert not re.search(r'href\s*=\s*"\s*javascript:', html, re.I)
    assert "javascript:alert(1)" in html  # shown as inert text
    ok_id = make_job(job_url="https://boards.example.com/job/9", job_url_canonical="https://boards.example.com/job/9")
    ok_html = auth_client.get(f"/jobs/{ok_id}").text
    assert '<a href="https://boards.example.com/job/9" rel="noopener noreferrer" target="_blank">' in ok_html


def test_markdown_renderer_is_safe():
    html = str(render_markdown("# Title\n\n**bold** and `<code>`\n\n<script>alert(1)</script>\n\n"
                               "[ok](https://example.com/a?b=1&c=2) [bad](javascript:alert(1)) "
                               "[q](https://x.example/\"onmouseover=alert(1))\n\n- item <img src=x>\n"))
    assert "<script>" not in html and "<img" not in html
    assert "<h2>Title</h2>" in html and "<strong>bold</strong>" in html and "<code>&lt;code&gt;</code>" in html
    assert '<a href="https://example.com/a?b=1&amp;c=2" rel="noopener noreferrer" target="_blank">ok</a>' in html
    assert 'href="javascript' not in html and "onmouseover=alert(1)\"" not in html
    assert "<li>item &lt;img src=x&gt;</li>" in html


# --------------------------------------------------------------------------- #
# downloads / static / headers / host
# --------------------------------------------------------------------------- #
def test_document_download_and_traversal(auth_client, db, make_job, config):
    seeded = seed_everything(db, make_job, config)
    resp = auth_client.get(f"/documents/{seeded['doc_id']}/download")
    assert resp.status_code == 200 and resp.content == b"%PDF-1.4 seeded"
    assert "attachment" in resp.headers["content-disposition"]
    (config.home / "secret.txt").write_text("top secret")
    job_id = seeded["job_id"]
    now = utcnow_iso()
    for i, rel in enumerate(("../secret.txt", "../../home/secret.txt", str(config.home / "secret.txt"),
                             "../.env", "/etc/passwd")):
        bad = db.insert("document_versions", {
            "job_id": job_id, "doc_type": "cover_letter", "version": 10 + i, "format": "pdf", "file_name": "x.pdf",
            "relative_path": rel, "sha256": "0", "generator": "rules", "profile_hash": "p", "created_at": now})
        resp = auth_client.get(f"/documents/{bad}/download")
        assert resp.status_code == 404, rel
        assert "top secret" not in resp.text
    for path in ("/documents/999999/download", "/documents/..%2F..%2F.env/download", "/documents/abc/download",
                 "/documents/-1/download"):
        assert auth_client.get(path).status_code == 404, path


def test_report_download_and_traversal(auth_client, db, make_job, config):
    seeded = seed_everything(db, make_job, config)
    rid = seeded["report_id"]
    assert auth_client.get(f"/reports/{rid}/download/xlsx").content == b"PK fake xlsx"
    assert auth_client.get(f"/reports/{rid}/download/md").status_code == 200
    assert auth_client.get(f"/reports/{rid}/download/exe").status_code == 404
    (config.home / ".env").write_text("JOB_AGENT_SESSION_SECRET='leak'")
    now = utcnow_iso()
    bad = db.insert("reports", {"report_date": "2001-01-01", "xlsx_file": "../.env", "markdown_file": "../.env",
                                "first_generated_at": now, "last_generated_at": now})
    for kind in ("xlsx", "md"):
        resp = auth_client.get(f"/reports/{bad}/download/{kind}")
        assert resp.status_code == 404 and "leak" not in resp.text
    detail = auth_client.get(f"/reports/{bad}")
    assert detail.status_code == 200 and "leak" not in detail.text


def test_static_serves_assets_only(client):
    css = client.get("/static/style.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert client.get("/static/app.js").status_code == 200
    for path in ("/static/.env", "/static/job_agent.db", "/static/..%2F..%2F..%2Fdata%2Fjob_agent.db",
                 "/static/%2e%2e/%2e%2e/config.py", "/static/../app.py", "/static/"):
        assert client.get(path).status_code == 404, path


def test_safe_static_files_refuses_db_and_env(tmp_path):
    from starlette.applications import Starlette
    from starlette.routing import Mount

    (tmp_path / "job_agent.db").write_bytes(b"SQLite")
    (tmp_path / ".env").write_text("SECRET=1")
    (tmp_path / "ok.css").write_text("body{}")
    (tmp_path / "notes.yaml").write_text("x: 1")
    c = TestClient(Starlette(routes=[Mount("/static", SafeStaticFiles(directory=tmp_path))]))
    assert c.get("/static/ok.css").status_code == 200
    for name in ("job_agent.db", ".env", "notes.yaml"):
        assert c.get(f"/static/{name}").status_code == 404


def test_security_headers_on_all_responses(auth_client):
    for path in ("/login", "/", "/jobs", "/does-not-exist", "/static/style.css"):
        resp = auth_client.get(path)
        assert resp.headers["content-security-policy"] == CONTENT_SECURITY_POLICY, path
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["x-frame-options"] == "DENY"
        assert resp.headers["referrer-policy"] == "no-referrer"
    assert auth_client.get("/").headers["cache-control"] == "no-store"
    assert auth_client.get("/jobs").headers["cache-control"] == "no-store"


def test_no_inline_scripts_or_styles(auth_client, db, make_job, config):
    seed_everything(db, make_job, config)
    for path in PAGES + ["/login"]:
        html = auth_client.get(path).text
        assert "<style" not in html and " style=" not in html, path
        for tag in re.findall(r"<script[^>]*>", html):
            assert 'src="/static/' in tag, path
        assert " onclick=" not in html and " onload=" not in html


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8765", "127.0.0.1.evil.example", "", "[::1"])
def test_bad_host_header_is_400(client, host):
    resp = client.get("/login", headers={"host": host})
    assert resp.status_code == 400
    assert resp.headers["x-frame-options"] == "DENY"


def test_allowed_hosts(make_app, config):
    cfg = config.model_copy(deep=True)
    cfg.dashboard.allowed_hosts = ["laptop.tailnet-name.ts.net"]
    c = TestClient(make_app(cfg=cfg), base_url=BASE_URL)
    for host in ("localhost", "localhost:8765", "127.0.0.1", "[::1]:8765", "laptop.tailnet-name.ts.net",
                 "LAPTOP.tailnet-name.ts.net:443"):
        assert c.get("/login", headers={"host": host}).status_code == 200, host
    assert c.get("/login", headers={"host": "other.ts.net"}).status_code == 400


def test_host_parsing():
    assert host_without_port("Example.ts.net:8765") == "example.ts.net"
    assert host_without_port("[::1]:8765") == "::1"
    assert host_without_port("::1") == "::1"
    assert host_without_port("a.b:notaport") is None
    assert host_without_port("a b") is None


# --------------------------------------------------------------------------- #
# tasks
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_tasks(monkeypatch):
    calls = []
    mod = types.ModuleType("fake_dashboard_tasks")

    def ok(ctx, run):
        calls.append((run.task_name, run.trigger, run.attempt))
        return {"done": True}

    def fails(ctx, run):
        calls.append((run.task_name, run.trigger, run.attempt))
        raise RuntimeError("kaboom")

    mod.ok, mod.fails = ok, fails
    monkeypatch.setitem(sys.modules, "fake_dashboard_tasks", mod)
    monkeypatch.setitem(tasks.TASKS, "dash_ok", tasks.TaskSpec("dash_ok", "fake_dashboard_tasks:ok", "ok"))
    monkeypatch.setitem(tasks.TASKS, "dash_secret", tasks.TaskSpec("dash_secret", "fake_dashboard_tasks:ok",
                                                                   "hidden", dashboard_allowed=False))
    monkeypatch.setitem(tasks.TASKS, "dash_fails", tasks.TaskSpec("dash_fails", "fake_dashboard_tasks:fails", "x"))
    monkeypatch.setitem(tasks.TASKS, "dash_unsafe", tasks.TaskSpec("dash_unsafe", "fake_dashboard_tasks:fails", "x",
                                                                   retry_safe=False))
    return calls


@pytest.mark.parametrize("name", ["nope", "dash_secret", "", "health; rm -rf /", "../health"])
def test_task_trigger_rejects_unknown_or_non_dashboard(auth_client, db, fake_tasks, name):
    token = session_csrf(auth_client)
    resp = auth_client.post("/controls/run", data={"csrf_token": token, "task": name})
    assert resp.status_code == 400
    auth_client.app.state.task_runner.wait(timeout=5)
    assert fake_tasks == []
    assert db.scalar("SELECT COUNT(*) FROM agent_runs") == 0


def test_task_trigger_runs_in_background(auth_client, db, fake_tasks):
    token = session_csrf(auth_client)
    resp = auth_client.post("/controls/run", data={"csrf_token": token, "task": "dash_ok"}, follow_redirects=False)
    assert resp.status_code == 303
    assert auth_client.app.state.task_runner.wait(timeout=10)
    assert fake_tasks == [("dash_ok", "dashboard", 1)]
    row = db.query_one("SELECT * FROM agent_runs WHERE task_name = 'dash_ok'")
    assert row["status"] == "succeeded" and row["trigger"] == "dashboard"


def test_task_trigger_ignores_pause(auth_client, db, fake_tasks):
    repo.set_paused(db, True, actor="cli")
    token = session_csrf(auth_client)
    auth_client.post("/controls/run", data={"csrf_token": token, "task": "dash_ok"})
    assert auth_client.app.state.task_runner.wait(timeout=10)
    assert fake_tasks == [("dash_ok", "dashboard", 1)]


def test_task_trigger_requires_csrf(auth_client, db, fake_tasks):
    assert auth_client.post("/controls/run", data={"task": "dash_ok"}).status_code == 403
    assert db.scalar("SELECT COUNT(*) FROM agent_runs") == 0


def test_retry_failed_run(auth_client, db, fake_tasks):
    token = session_csrf(auth_client)
    auth_client.post("/controls/run", data={"csrf_token": token, "task": "dash_fails"})
    assert auth_client.app.state.task_runner.wait(timeout=10)
    failed = db.query_one("SELECT run_uid, status FROM agent_runs WHERE task_name = 'dash_fails'")
    assert failed["status"] == "failed"
    assert 'name="run_uid" value="%s"' % failed["run_uid"] in auth_client.get("/controls").text
    resp = auth_client.post("/controls/retry", data={"csrf_token": token, "run_uid": failed["run_uid"]},
                            follow_redirects=False)
    assert resp.status_code == 303
    assert auth_client.app.state.task_runner.wait(timeout=10)
    assert [c[2] for c in fake_tasks] == [1, 2]


def test_retry_refused_for_unsafe_or_successful_runs(auth_client, db, fake_tasks):
    token = session_csrf(auth_client)
    run_id, unsafe_uid = repo.start_run(db, "dash_unsafe", "scheduled")
    repo.finish_run(db, run_id, RunStatus.FAILED)
    run_id, ok_uid = repo.start_run(db, "dash_ok", "scheduled")
    repo.finish_run(db, run_id, RunStatus.SUCCEEDED)
    run_id, secret_uid = repo.start_run(db, "dash_secret", "scheduled")
    repo.finish_run(db, run_id, RunStatus.FAILED)
    for uid in (unsafe_uid, ok_uid, secret_uid, "R-missing"):
        assert auth_client.post("/controls/retry", data={"csrf_token": token, "run_uid": uid}).status_code == 400
    assert fake_tasks == []


def test_locked_task_is_not_started_twice(auth_client, db, fake_tasks):
    assert repo.acquire_task_lock(db, "dash_ok", "someone-else", 30)
    token = session_csrf(auth_client)
    assert auth_client.post("/controls/run", data={"csrf_token": token, "task": "dash_ok"}).status_code == 409
    assert fake_tasks == []


def test_no_shell_or_eval_in_dashboard_package():
    offenders = []
    files = [p for p in PACKAGE_DIR.rglob("*") if p.suffix in {".py", ".html", ".js"}]
    assert any(p.name == "app.py" for p in files)
    for path in files:
        text = path.read_text(encoding="utf-8")
        for needle in ("subprocess", "os.system", "eval(", "exec(", "os.popen", "pty.spawn", "__import__("):
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert offenders == []


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", PAGES)
def test_pages_render_with_empty_database(auth_client, path):
    resp = auth_client.get(path)
    assert resp.status_code == 200, resp.text
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in resp.text


def test_login_page_has_viewport(client):
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in client.get("/login").text


def test_pages_render_with_seeded_data(auth_client, db, make_job, config):
    s = seed_everything(db, make_job, config)
    expectations = {
        "/": ["Seeded Corp", "Running", "Submit Data Analyst @ Seeded Corp", "Reply to the recruiter",
              "Interview invitation", "discovery:greenhouse"],
        "/jobs": ["Data Analyst", ">82<", "strong match"],
        "/jobs?q=seeded&category=strong_match&sort=employer": ["Seeded Corp"],
        "/jobs?q=nomatchhere": ["No jobs match"],
        f"/jobs/{s['job_id']}": ["Good analytics fit.", "SQL and Python match", "We do not sponsor visas.",
                                 "Tableau certification", "Supported dashboards", "Review sponsorship statement",
                                 f"/documents/{s['doc_id']}/download"],
        "/applications": ["Seeded Corp"],
        f"/applications/{s['app_id']}": ["Do you require sponsorship?", "legal declaration", "form prepared",
                                         f"/documents/{s['doc_id']}/download", "Interview invitation"],
        "/approvals": [s["approval_uid"]],
        f"/approvals/{s['approval_uid']}": ["Seeded Corp", "expected_payload_hash"],
        "/email": ["Interview invitation", "Thank you, I am available Tuesday.", "Gmail integration disabled",
                   "not available yet"],
        "/email?category=interview_invitation&attention=1": ["Interview invitation"],
        "/followups": ["Reply to the recruiter", "Hello, following up.", "not available yet"],
        "/followups?show=all": ["Reply to the recruiter"],
        "/reports": ["2026-10-11", "<svg", "Jobs discovered", "new jobs: 3"],
        f"/reports/{s['report_id']}": ["Daily report", "<strong>3</strong>", "&lt;script&gt;", "<table"],
        "/controls": ["discovery:greenhouse", "boom", "Mark resolved", "Run"],
    }
    for path, needles in expectations.items():
        resp = auth_client.get(path)
        assert resp.status_code == 200, (path, resp.text[:500])
        for needle in needles:
            assert needle in resp.text, (path, needle)
    assert "<script>alert(1)</script>" not in auth_client.get(f"/reports/{s['report_id']}").text


def test_api_status_with_data(auth_client, db, make_job, config):
    seed_everything(db, make_job, config)
    data = auth_client.get("/api/status").json()
    assert data["daemon_running"] is True and data["pending_approvals"] == 1 and data["unresolved_errors"] == 1
    assert data["gmail_connected"] is False


def test_not_found_pages(auth_client):
    for path in ("/jobs/999999", "/applications/999999", "/reports/999999", "/jobs/notanumber", "/nope"):
        assert auth_client.get(path).status_code == 404, path


def test_job_relevance_and_notes(auth_client, db, make_job):
    job_id = make_job()
    token = session_csrf(auth_client)
    resp = auth_client.post(f"/jobs/{job_id}/relevance", data={"csrf_token": token, "value": "irrelevant"},
                            follow_redirects=False)
    assert resp.status_code == 303
    assert db.scalar("SELECT user_relevance FROM jobs WHERE id = ?", (job_id,)) == "irrelevant"
    assert auth_client.post(f"/jobs/{job_id}/relevance", data={"csrf_token": token, "value": "bogus"}).status_code \
        == 400
    auth_client.post(f"/jobs/{job_id}/notes", data={"csrf_token": token, "notes": "Ask about <team>"})
    assert db.scalar("SELECT user_notes FROM jobs WHERE id = ?", (job_id,)) == "Ask about <team>"
    assert "Ask about &lt;team&gt;" in auth_client.get(f"/jobs/{job_id}").text
    assert auth_client.post(f"/jobs/{job_id}/notes", data={"csrf_token": token, "notes": "x" * 6000}).status_code \
        == 400
    assert auth_client.post("/jobs/999999/relevance", data={"csrf_token": token, "value": "relevant"}).status_code \
        == 404
    actions = {r[0] for r in db.query_all("SELECT action FROM audit_events WHERE actor = 'dashboard'")}
    assert {"job_relevance_set", "job_notes_updated"} <= actions
    auth_client.post(f"/jobs/{job_id}/relevance", data={"csrf_token": token, "value": "clear"})
    assert db.scalar("SELECT user_relevance FROM jobs WHERE id = ?", (job_id,)) is None


def test_email_and_followup_actions_degrade_when_modules_missing(auth_client, db, make_job, config):
    s = seed_everything(db, make_job, config)
    token = session_csrf(auth_client)
    resp = auth_client.post(f"/email/{s['email_id']}/associate", data={"csrf_token": token, "job_ref": "1"})
    assert resp.status_code == 200 and "not available yet" in resp.text
    resp = auth_client.post(f"/followups/{s['followup_id']}/status",
                            data={"csrf_token": token, "status": "completed", "confirm": "yes"})
    assert resp.status_code == 200 and "not available yet" in resp.text
    assert db.scalar("SELECT status FROM followups WHERE id = ?", (s["followup_id"],)) == "pending"


def test_email_and_followup_actions_use_contracts(auth_client, db, make_job, config, monkeypatch):
    s = seed_everything(db, make_job, config)
    calls = []
    email_mod = types.ModuleType("job_agent.email.service")
    email_mod.associate_email = lambda ctx, email_id, job_ref=None, application_ref=None, apply_status=False: \
        calls.append(("assoc", email_id, job_ref, application_ref, apply_status))
    follow_mod = types.ModuleType("job_agent.followups.service")
    follow_mod.set_followup_status = lambda ctx, followup_id, status, actor: \
        calls.append(("followup", followup_id, status.value, actor))
    for name, mod in (("job_agent.email.service", email_mod), ("job_agent.followups.service", follow_mod)):
        monkeypatch.setitem(sys.modules, name, mod)
        parent = name.rsplit(".", 1)[0]
        if parent not in sys.modules:
            monkeypatch.setitem(sys.modules, parent, types.ModuleType(parent))
    token = session_csrf(auth_client)
    # status update via email requires confirmation
    assert auth_client.post(f"/email/{s['email_id']}/associate", data={
        "csrf_token": token, "application_ref": "A-abc", "apply_status": "yes"}).status_code == 400
    auth_client.post(f"/email/{s['email_id']}/associate", data={
        "csrf_token": token, "job_ref": str(s["job_id"]), "apply_status": "yes", "confirm": "yes"})
    assert auth_client.post(f"/followups/{s['followup_id']}/status", data={
        "csrf_token": token, "status": "dismissed"}).status_code == 400  # no confirm
    assert auth_client.post(f"/followups/{s['followup_id']}/status", data={
        "csrf_token": token, "status": "pending", "confirm": "yes"}).status_code == 400  # not allowed
    auth_client.post(f"/followups/{s['followup_id']}/status", data={
        "csrf_token": token, "status": "dismissed", "confirm": "yes"})
    assert calls == [("assoc", s["email_id"], s["job_id"], None, True),
                     ("followup", s["followup_id"], "dismissed", "dashboard")]
    assert auth_client.post(f"/email/{s['email_id']}/associate", data={
        "csrf_token": token, "job_ref": "x'; DROP TABLE jobs;--"}).status_code == 400


def test_gmail_status_logic(db, config):
    cfg = config.model_copy(deep=True)
    cfg.gmail.enabled = True
    assert queries.gmail_status(db, cfg)["label"] == "Gmail not connected"
    repo.set_state(db, "gmail_last_success_at", "2026-01-01T00:00:00+00:00")
    assert queries.gmail_status(db, cfg)["connected"] is True
    repo.set_state(db, "gmail_auth_error", "token revoked")
    status = queries.gmail_status(db, cfg)
    assert status["connected"] is False and "token revoked" in status["detail"]
    repo.set_state(db, "gmail_last_success_at", "2999-01-01T00:00:00+00:00")
    assert queries.gmail_status(db, cfg)["connected"] is True


def test_trends_counts_recent_days(db, make_job, config):
    make_job()
    make_job()
    series = {s.key: s for s in queries.trends(db, config)}
    assert len(series["jobs"].points) == 30
    assert series["jobs"].total == 2 and series["jobs"].points[-1][1] == 2
    assert series["applications"].total == 0


def test_default_factory_loads_environment_once_and_forks(home, monkeypatch, no_service_modules):
    with pytest.raises(HumanActionRequired):
        create_app()
    monkeypatch.setenv("JOB_AGENT_DASHBOARD_USERNAME", USERNAME)
    monkeypatch.setenv("JOB_AGENT_DASHBOARD_PASSWORD_HASH", PASSWORD_HASH)
    monkeypatch.setenv("JOB_AGENT_SESSION_SECRET", SESSION_SECRET)
    app = create_app()
    try:
        c = TestClient(app, base_url=BASE_URL)
        assert login(c).status_code == 303
        assert c.get("/").status_code == 200
        first, second = app.state.ctx_factory(), app.state.ctx_factory()
        assert first.config is second.config and first.db is not second.db  # shared config, own connections
        first.close()
        second.close()
    finally:
        app.state.task_runner.shutdown(wait_for_tasks=True)
