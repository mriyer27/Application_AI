"""Browser-layer logic that needs no Chromium: URL policy, request guard, page classification, field keys,
outcome polling."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from urllib.parse import quote

import pytest

from job_agent.browser.detection import PageSnapshot, detect_challenge, detect_closed_listing, detect_confirmation
from job_agent.browser.errors import ChallengeDetected, NavigationBlocked
from job_agent.browser.fakes import FakeBrowser, FakeForm
from job_agent.browser.forms import (
    FieldOption,
    FormField,
    build_fields,
    clean_label,
    is_final_submit_label,
    match_option,
    value_matches,
)
from job_agent.browser.policy import NavigationPolicy
from job_agent.browser.session import BrowserOptions, _Guard, poll_outcome
from job_agent.errors import HumanActionRequired


# ------------------------------------------------------------- policy ---- #
def _policy(**kw):
    return NavigationPolicy.for_job(job_url=kw.get("job_url", "https://boards.greenhouse.io/acme/jobs/42"),
                                    application_url=kw.get("application_url", "https://careers.acme.com/apply/42"),
                                    source_name=kw.get("source_name", "greenhouse"),
                                    source_identifier=kw.get("source_identifier", "acme"))


def test_initial_url_must_be_on_the_jobs_stored_host():
    p = _policy()
    assert p.initial_problem("https://careers.acme.com/apply/42?x=1") is None
    assert p.initial_problem("https://boards.greenhouse.io/acme/jobs/42") is None
    assert "not the job's official host" in p.initial_problem("https://evil.example.net/apply")
    assert p.initial_problem("https://www.careers.acme.com/apply") is not None  # exact host only
    assert p.initial_problem("ftp://careers.acme.com/x") is not None
    assert p.initial_problem("javascript:alert(1)") is not None
    assert "downgrade" in p.initial_problem("http://careers.acme.com/apply/42")
    with pytest.raises(NavigationBlocked) as exc:
        p.check_initial("https://evil.example.net/")
    assert isinstance(exc.value, HumanActionRequired)


def test_ats_hosts_allowed_only_for_the_same_posting():
    p = _policy(job_url="https://careers.acme.com/jobs/42", application_url=None)
    # Initial open: ATS host is not the stored host -> refused.
    assert p.initial_problem("https://job-boards.greenhouse.io/acme/jobs/42") is not None
    # Navigation/redirect to the same board on the same ATS -> allowed.
    assert p.navigation_problem("https://job-boards.greenhouse.io/acme/jobs/42") is None
    assert p.navigation_problem("https://boards.greenhouse.io/embed/job_app?for=acme&token=42") is None
    # Same ATS, other employer's board -> refused; other ATS -> refused.
    assert p.navigation_problem("https://job-boards.greenhouse.io/othercorp/jobs/1") is not None
    assert p.navigation_problem("https://jobs.lever.co/acme/1") is not None
    assert p.navigation_problem("about:blank") is None
    assert p.navigation_problem("data:text/html,hi") is not None


def test_policy_without_identifier_never_allows_ats_hosts():
    p = NavigationPolicy.for_job(job_url="https://careers.acme.com/j", application_url=None, source_name="greenhouse",
                                 source_identifier=None)
    assert p.navigation_problem("https://boards.greenhouse.io/anything/jobs/1") is not None


def test_policy_ports_must_match_the_stored_url():
    """Another port on the official host is another server (SEC-1 attacker on 127.0.0.1:<other port>)."""
    local = NavigationPolicy.for_job(job_url="http://127.0.0.1:8123/forms/a.html", application_url=None)
    assert local.navigation_problem("http://127.0.0.1:8123/submit/confirm") is None
    assert "port 9999" in local.navigation_problem("http://127.0.0.1:9999/beacon")
    assert local.initial_problem("http://127.0.0.1:9999/forms/a.html") is not None
    assert local.request_host_allowed("http://127.0.0.1:9999/x") is False
    p = _policy()
    assert p.navigation_problem("https://careers.acme.com:443/apply/42") is None  # default port
    assert p.navigation_problem("https://careers.acme.com:8443/apply/42") is not None
    assert p.navigation_problem("https://careers.acme.com:99999/") is not None  # invalid port
    plain = NavigationPolicy.for_job(job_url="http://jobs.acme.com/1", application_url=None)
    assert plain.navigation_problem("https://jobs.acme.com/1") is None  # http -> https upgrade still fine


def test_asset_allowlist_is_static_https_ats_cdn_only():
    gh = NavigationPolicy.for_job(job_url="https://careers.acme.com/j", application_url=None, source_name="greenhouse",
                                  source_identifier="acme")
    assert gh.asset_request_allowed("https://boards.cdn.greenhouse.io/assets/app.js")
    assert gh.asset_request_allowed("https://boards.greenhouse.io/static/app.css")  # its ATS host, no identifier
    assert not gh.asset_request_allowed("http://boards.cdn.greenhouse.io/assets/app.js")
    assert not gh.asset_request_allowed("https://cdn.ashbyprd.com/x.js")  # other ATS
    assert not gh.asset_request_allowed("https://evil.example.net/x.js")
    on_ats = NavigationPolicy.for_job(job_url="https://jobs.ashbyhq.com/acme/1", application_url=None)
    assert on_ats.asset_request_allowed("https://cdn.ashbyprd.com/x.js")
    plain = NavigationPolicy.for_job(job_url="https://careers.acme.com/j", application_url=None)
    assert plain.asset_hosts == frozenset()


# ------------------------------------------------------- request guard ---- #
@dataclass
class _Frame:
    page: object
    parent_frame: object | None = None


@dataclass
class _Req:
    url: str
    method: str = "GET"
    frame: _Frame | None = None
    nav: bool = False
    resource_type: str = "xhr"
    redirected_from: object | None = None

    def is_navigation_request(self) -> bool:
        return self.nav


class _NoFrameReq(_Req):
    @property  # type: ignore[override]
    def frame(self):  # noqa: D401 - mimics Playwright: throws for popups' first request / workers
        raise RuntimeError("frame not available")

    @frame.setter
    def frame(self, value):
        pass


class _Route:
    def __init__(self) -> None:
        self.result: str | None = None

    def continue_(self) -> None:
        self.result = "continue"

    def abort(self, code: str = "failed") -> None:
        self.result = "abort"


OFFICIAL = "https://careers.acme.com/apply/42"


def _guard(strict: bool = False, clock=None) -> tuple[_Guard, object]:
    page = object()
    g = _Guard(policy=_policy(), strict=strict, arm_seconds=30, page=page, **({"clock": clock} if clock else {}))
    return g, page


def _req(g_page, url, method="GET", *, nav=False, iframe=False, rtype="xhr"):
    return _Req(url, method, _Frame(g_page, object() if iframe else None), nav, rtype)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("nav,iframe,rtype", [(False, False, "fetch"), (False, False, "xhr"), (False, False, "ping"),
                                              (False, True, "fetch"), (True, False, "document"),
                                              (True, True, "document")])
def test_guard_refuses_every_non_get_before_the_approved_click(method, nav, iframe, rtype):
    g, page = _guard()
    reason = g.decide(_req(page, OFFICIAL, method, nav=nav, iframe=iframe, rtype=rtype))
    assert reason and "before the approved submission" in reason
    assert g.blocked_posts == ["https://careers.acme.com/apply/42"] and g.posts == []


def test_guard_armed_allows_policy_hosts_only_and_one_form_post():
    clock = _Clock()
    g, page = _guard(clock=clock)
    g.arm()
    assert g.decide(_req(page, "https://evil.example.net/collect", "POST")) is not None  # never off-policy
    assert g.decide(_req(page, "https://careers.acme.com:8443/x", "POST")) is not None  # other port
    assert g.decide(_req(page, "https://careers.acme.com/api/upload", "POST")) is None  # XHR submit while armed
    assert g.armed
    assert g.decide(_req(page, OFFICIAL, "POST", nav=True, rtype="document")) is None  # the form post...
    assert not g.armed  # ...disarms
    assert g.decide(_req(page, OFFICIAL, "POST", nav=True, rtype="document")) is not None
    g.arm()
    clock.t += 31  # the arming window expires
    assert g.decide(_req(page, "https://careers.acme.com/api/upload", "POST")) is not None
    g.arm()
    g.disarm()
    assert g.decide(_req(page, "https://careers.acme.com/api/upload", "POST")) is not None


def test_guard_navigation_rules():
    g, page = _guard()
    assert g.decide(_req(page, OFFICIAL, nav=True, rtype="document")) is None
    # off-policy iframe navigation: aborted, not a session-stopping violation
    assert "frame navigation refused" in g.decide(_req(page, "https://evil.example.net/f?e=x", nav=True,
                                                       iframe=True, rtype="document"))
    assert g.violations == []
    # off-policy top-level navigation: aborted and stops the session
    assert "navigation refused" in g.decide(_req(page, "https://evil.example.net/", nav=True, rtype="document"))
    with pytest.raises(NavigationBlocked):
        g.raise_if_violated()


def test_guard_third_party_gets_stop_once_filling_begins():
    g, page = _guard()
    third = "https://cdn.example.org/app.js"
    assert g.decide(_req(page, third, rtype="script")) is None  # page still rendering, nothing typed
    assert g.decide(_req(page, OFFICIAL + "/data.json")) is None
    g.lock()
    assert "after filling began" in g.decide(_req(page, third, rtype="script"))
    assert "after filling began" in g.decide(_req(page, "https://evil.example.net/p.gif?e=x", rtype="image"))
    assert g.decide(_req(page, OFFICIAL + "/data.json")) is None  # official host GETs still fine
    # the ATS asset allowlist: static types only
    assert g.decide(_req(page, "https://boards.cdn.greenhouse.io/a.js", rtype="script")) is None
    assert g.decide(_req(page, "https://boards.cdn.greenhouse.io/a.json", rtype="fetch")) is not None
    strict, spage = _guard(strict=True)
    assert "strict" in strict.decide(_req(spage, third, rtype="script"))


def test_guard_refuses_requests_from_other_windows_and_unknown_frames():
    g, page = _guard()
    popup = object()
    assert g.decide(_req(popup, OFFICIAL, nav=True, rtype="document")) is not None
    assert g.decide(_NoFrameReq(OFFICIAL, "GET", None, True, "document")) is not None
    route = _Route()
    g.on_route(route, _req(popup, OFFICIAL))
    assert route.result == "abort" and g.blocked[-1][2].startswith("request from another window")


def test_guard_fails_closed_and_never_logs_query_strings():
    g, page = _guard()

    class Broken(_Req):
        def is_navigation_request(self) -> bool:
            raise RuntimeError("boom")

    route = _Route()
    g.on_route(route, Broken(OFFICIAL, "GET", _Frame(page)))
    assert route.result == "abort" and "guard error" in g.blocked[-1][2]
    route = _Route()
    g.on_route(route, _req(page, "https://careers.acme.com/autosave?email=jane%40example.com", "POST"))
    assert route.result == "abort"
    assert "jane" not in repr(g.blocked) and "jane" not in repr(g.blocked_posts)


def test_guard_records_websockets_without_connecting():
    g, _ = _guard()

    class _WS:
        url = "wss://evil.example.net/socket?e=jane"
        closed = False

        def close(self, **kw):
            self.closed = True

    ws = _WS()
    g.on_websocket(ws)  # must not call into Playwright from inside the callback
    assert not ws.closed and g.websockets == ["wss://evil.example.net/socket"]
    g.close_sockets()
    assert ws.closed


# ---------------------------------------------------------- detection ---- #
@pytest.mark.parametrize("snap,kind", [
    (PageSnapshot(url="u", iframe_srcs=("https://www.google.com/recaptcha/api2/anchor?k=x",)), "captcha"),
    (PageSnapshot(url="u", script_srcs=("https://js.hcaptcha.com/1/api.js",)), "captcha"),
    (PageSnapshot(url="u", script_srcs=("https://challenges.cloudflare.com/turnstile/v0/api.js",)), "captcha"),
    (PageSnapshot(url="u", markers=(".cf-turnstile",)), "captcha"),
    (PageSnapshot(url="u", markers=("#challenge-form",)), "cloudflare"),
    (PageSnapshot(url="u", title="Just a moment..."), "cloudflare"),
    (PageSnapshot(url="u", text="Please verify you are human to continue"), "human_verification"),
    (PageSnapshot(url="u", text="Sign in", visible_password_fields=1), "login"),
    (PageSnapshot(url="u", text="Create an account. Confirm password", visible_password_fields=2), "account_creation"),
    (PageSnapshot(url="u", text="You must create an account to apply for this job."), "account_creation"),
    (PageSnapshot(url="u", text="Please sign in to apply"), "login"),
])
def test_challenges_detected(snap, kind):
    found = detect_challenge(snap)
    assert found is not None and found.kind == kind


def test_normal_form_is_not_a_challenge():
    snap = PageSnapshot(url="u", title="Apply - Business Analyst",
                        text="First name Last name Email Resume Submit application. We value your privacy.",
                        script_srcs=("https://cdn.example.com/app.js",), form_control_count=12)
    assert detect_challenge(snap) is None


def test_closed_listing_detection():
    assert detect_closed_listing(PageSnapshot(url="u", status=404))
    assert detect_closed_listing(PageSnapshot(url="u", text="This job is no longer accepting applications."))
    assert detect_closed_listing(PageSnapshot(url="u", text="Apply now")) is None


def test_confirmation_must_be_new_after_the_click():
    form = PageSnapshot(url="https://a.com/apply", text="Apply here. Thank you for applying to Acme!")
    same = PageSnapshot(url="https://a.com/apply", text="Apply here. Thank you for applying to Acme!")
    assert detect_confirmation(same, form) is None  # text was already on the form page
    done = PageSnapshot(url="https://a.com/apply", text="Your application has been received.")
    assert detect_confirmation(done, form).matched_text.startswith("Your application has been received")
    assert detect_confirmation(PageSnapshot(url="https://a.com/apply"), PageSnapshot(url="https://a.com/apply")) is None


FORM_PAGE = PageSnapshot(url="https://a.com/apply", text="Apply: Data Analyst. First name Email Submit application",
                         visible_controls=("first_name", "email", "phone"))


@pytest.mark.parametrize("url,text,controls", [
    # C5 review step: URL says "confirm", nothing was sent
    ("https://a.com/apply/confirm", "Review your application\nNothing has been sent yet. Check your answers.", ()),
    # C5 screening step: "application is submitted" in future/conditional wording
    ("https://a.com/apply/screening", "Before your application is submitted, answer the screening questions below.",
     ("years",)),
    ("https://a.com/apply/2", "Thank you for your application! Before it is submitted, please confirm your details.",
     ()),
    ("https://a.com/apply/2", "Thanks for applying. Your application will be submitted once you click Send.", ()),
    ("https://a.com/apply/2", "Thank you for applying! Your application has not been submitted yet.", ()),
    ("https://a.com/apply/2", "Thank you for your application. Step 2 of 3: additional questions", ()),
    ("https://a.com/apply/2", "Thanks for applying! One more step: upload your transcript.", ()),
    ("https://a.com/apply/2", "Please review your application. We have received your application draft.", ()),
    ("https://a.com/apply/2", "Your application was saved as a draft. Thank you for applying.", ()),
    # URL alone is never evidence
    ("https://a.com/apply/thank-you", "", ()),
    ("https://a.com/thanks?confirmation=1", "Loading...", ()),
    # confirmation wording, but the application form is still on screen
    ("https://a.com/apply", "Thank you for applying! Your application has been received.", ("email",)),
    # weak wording only
    ("https://a.com/apply/done", "Successfully submitted", ()),
    ("https://a.com/apply/done", "Your application is submitted for review after you press Apply.", ()),
])
def test_intermediate_and_ambiguous_pages_are_never_confirmed(url, text, controls):
    snap = PageSnapshot(url=url, text=text, main_text=text, visible_controls=controls)
    assert detect_confirmation(snap, FORM_PAGE) is None
    assert detect_confirmation(snap) is None or controls  # even without a baseline (form check needs one)


@pytest.mark.parametrize("text", [
    "Thank you for applying!\nYour application has been received. We will be in touch.",
    "Application submitted!\nThanks for applying to Acme.",
    "We've received your application. Our recruiting team will review your application and contact you.",
    "Your application was successfully submitted. A confirmation email will be sent to you shortly.",
    "Thanks for your application, Test. Next steps: we review every application within two weeks.",
])
def test_genuine_thank_you_pages_are_confirmed(text):
    snap = PageSnapshot(url="https://a.com/apply/thanks", text=text, main_text=text,
                        visible_controls=("newsletter_email_footer_signup",))
    found = detect_confirmation(snap, FORM_PAGE)
    assert found is not None and found.url.endswith("/thanks")


def test_confirmation_must_be_in_main_content():
    text = "Jobs | Thank you for applying through our careers site\nApply: Data Analyst"
    snap = PageSnapshot(url="https://a.com/apply/2", text=text, main_text="Apply: Data Analyst")
    assert detect_confirmation(snap, PageSnapshot(url="https://a.com/apply")) is None


def test_final_submit_labels_only():
    for ok in ("Submit application", "Submit", "Apply", "Send application", "SUBMIT APPLICATION", " Submit  ",
               "Submit application \u203a", "Apply!"):
        assert is_final_submit_label(ok), ok
    for bad in ("Continue", "Next", "Review", "Save and continue", "Save draft", "Apply now", "Submit later",
                "Next: review application", "", None):
        assert not is_final_submit_label(bad), bad


# --------------------------------------------------------------- forms ---- #
def test_build_fields_keys_are_unique_and_labels_clean():
    raw = [
        {"name": "email", "label": "Email *", "type": "email", "required": True, "locator": "#e"},
        {"name": "email", "label": "Confirm email", "type": "email", "locator": "#e2"},
        {"id": "phone", "label": "Phone (required)", "type": "tel", "locator": "#p"},
        {"label": "Why us?", "type": "textarea", "locator": "xpath=/html/body/textarea[1]"},
        {"label": "", "type": "text", "locator": "xpath=/x"},
    ]
    fields = build_fields(raw)
    assert [f.key for f in fields] == ["email", "email__2", "phone", "label-why-us", "field-5"]
    assert fields[0].label == "Email" and fields[2].label == "Phone"
    assert clean_label("  Resume / CV  ✱ ") == "Resume / CV"


def test_option_matching_is_exact_and_unambiguous():
    opts = (FieldOption("Yes", "1"), FieldOption("No", "0"), FieldOption("yes ", "y"))
    assert match_option("No", opts).value == "0"
    assert match_option("yes", opts) is None  # two options normalize to "yes": ambiguous
    assert match_option("Nope", opts) is None
    assert match_option("0", opts).label == "No"  # by value
    radio = FormField("r", "Q", "radio", options=(FieldOption("Yes", "y"), FieldOption("No", "n")))
    assert value_matches(radio, "No", "No") and not value_matches(radio, "No", "Yes")


# ---------------------------------------------------- outcome polling ---- #
class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _page(outcome: str):
    fields = [FormField("email", "Email", "email", required=True, locator="#email")]
    fake = FakeBrowser(FakeForm(url="https://a.com/apply", fields=fields, outcome=outcome))
    policy = NavigationPolicy.for_job(job_url="https://a.com/apply", application_url=None)
    with fake(BrowserOptions()) as b:
        page = b.open_application("https://a.com/apply", policy)
    baseline = page.snapshot()
    page.click_submit(page.find_submit())
    return page, baseline


@pytest.mark.parametrize("outcome,kind", [("confirm", "confirmed"), ("validation", "validation_errors"),
                                          ("challenge", "challenge"), ("offhost", "navigation_blocked")])
def test_poll_outcome_kinds(outcome, kind):
    page, baseline = _page(outcome)
    assert poll_outcome(page, baseline, timeout_seconds=5).kind == kind


def _ticking(page):
    clock = _Clock()
    page.wait = lambda s: setattr(clock, "t", clock.t + s)  # type: ignore[method-assign]
    return clock


def test_review_step_is_ambiguous_never_confirmed():
    page, baseline = _page("review")
    result = poll_outcome(page, baseline, timeout_seconds=3, interval_seconds=0.5, clock=_ticking(page))
    assert result.kind == "ambiguous" and "intermediate / review step" in result.detail
    assert result.url.endswith("/apply/confirm") and page.browser.submissions == 1


def test_screening_step_with_errors_is_validation_errors_not_confirmed():
    page, baseline = _page("screening")
    result = poll_outcome(page, baseline, timeout_seconds=3, clock=_ticking(page))
    assert result.kind == "validation_errors" and result.validation_errors == ("Please answer this question",)


def test_errors_next_to_confirmation_wording_are_ambiguous():
    page, baseline = _page("hang")
    page.snapshot = lambda: PageSnapshot(  # type: ignore[method-assign]
        url="https://a.com/apply/done", text="Thank you for applying! Your application has been received.",
        visible_controls=("newsletter",))
    page.validation_errors = lambda **kw: ["Please enter a valid email"]  # type: ignore[method-assign]
    result = poll_outcome(page, baseline, timeout_seconds=2, clock=_ticking(page))
    assert result.kind == "ambiguous" and "errors at the same time" in result.detail


def test_errors_without_any_form_are_ambiguous():
    page, baseline = _page("hang")
    page.snapshot = lambda: PageSnapshot(url="https://a.com/error", text="Something went wrong")  # type: ignore
    page.validation_errors = lambda **kw: ["Something went wrong"]  # type: ignore[method-assign]
    assert poll_outcome(page, baseline, timeout_seconds=2, clock=_ticking(page)).kind == "ambiguous"


def test_poll_outcome_disarms_the_guard():
    page, baseline = _page("confirm")
    assert page.armed
    poll_outcome(page, baseline, timeout_seconds=2)
    assert not page.armed


def test_poll_outcome_times_out_without_retrying():
    page, baseline = _page("hang")
    clock = _Clock()
    page.wait = lambda s: setattr(clock, "t", clock.t + s)  # type: ignore[method-assign]
    result = poll_outcome(page, baseline, timeout_seconds=3, interval_seconds=0.5, clock=clock)
    assert result.kind == "timeout"
    assert page.browser.submissions == 1  # observed only; never clicked again


def test_fake_browser_applies_policy_and_challenge_detection():
    policy = NavigationPolicy.for_job(job_url="https://a.com/apply", application_url=None)
    fake = FakeBrowser(FakeForm(url="https://a.com/apply", fields=[], markers=(".g-recaptcha",)),
                       FakeForm(url="https://a.com/redirect", fields=[], redirect_to="https://other.com/x"))
    with pytest.raises(NavigationBlocked):
        fake.open_application("https://other.com/apply", policy)
    with pytest.raises(ChallengeDetected):
        fake.open_application("https://a.com/apply", policy)
    with pytest.raises(NavigationBlocked):
        fake.open_application("https://a.com/redirect", policy)
    assert fake.submissions == 0


# ------------------------------------------- round 2: data-loss prevention ---- #
RESUME = b"%PDF-1.4\n" + bytes(range(256)) * 3 + b"\nSECRET-RESUME-CONTENT 0123456789\n%%EOF"
EMAIL = "test.candidate@example.com"


def _protected():
    from job_agent.browser.dlp import ProtectedData

    p = ProtectedData()
    for value in ("Test", "Candidate", EMAIL, "+1 315 555 0100", "Company careers page", "yes", "No", "checked",
                  "I have led the migration of our reporting stack to dbt and taught the team to use it."):
        p.add_value(value)
    p.add_file(RESUME, "Test_Candidate_resume.pdf")
    return p


@pytest.mark.parametrize("url", [
    f"https://careers.acme.com/collect/img?d={quote(EMAIL)}",
    f"https://careers.acme.com/collect?d={quote(quote(EMAIL))}",  # double-encoded
    "https://careers.acme.com/c?d=Test+Candidate",  # form encoding, short value on a token boundary
    "https://careers.acme.com/c?d=" + base64.b64encode(f"x|{EMAIL}|y".encode()).decode(),
    "https://careers.acme.com/c?d=" + base64.urlsafe_b64encode(f"ab{EMAIL}".encode()).decode().rstrip("="),
    "https://careers.acme.com/c?d=" + base64.b64encode(f"{EMAIL}".encode()).decode()[4:],  # sliced mid-string
    "https://careers.acme.com/c/" + EMAIL.encode().hex() + ".gif",
    "https://careers.acme.com/c?d=" + quote(json.dumps({"e": "\\u0074est.candidate@example.com"})),
    "https://careers.acme.com/c?p=315-555-0100",  # phone digits, reformatted
    "https://careers.acme.com/c?u=TEST.CANDIDATE",  # e-mail local part, case-insensitive
    "https://careers.acme.com/c?t=" + quote("the migration of our reporting stack to dbt"),  # fragment of a long answer
])
def test_dlp_recognises_typed_values_in_every_encoding(url):
    assert _protected().match(url) == "typed data"


@pytest.mark.parametrize("encode", [
    lambda b: "c=" + quote(base64.b64encode(b[40:120]).decode()),
    lambda b: "c=" + quote(base64.b64encode(b[41:121]).decode()),  # another byte alignment
    lambda b: "c=" + base64.urlsafe_b64encode(b[200:300]).decode(),
    lambda b: "c=" + b[300:360].hex(),
    lambda b: "c=" + quote(b[500:560].decode("latin-1")),  # FileReader.readAsBinaryString + encodeURIComponent
])
def test_dlp_recognises_chunks_of_an_uploaded_file(encode):
    assert _protected().match("https://careers.acme.com/collect/file?" + encode(RESUME)) == "uploaded file content"


@pytest.mark.parametrize("url", [
    "https://careers.acme.com/static/latest.js",  # "test" inside a word is not the value "Test"
    "https://careers.acme.com/static/app.3f9a8b7c6d5e4f30.js",
    "https://careers.acme.com/api/jobs/42?lang=en&yes=1&checked=false",  # short / generic values ignored
    "https://cdn.example.org/fonts/inter.woff2",
    "https://careers.acme.com/img/" + base64.b64encode(b"just some other bytes here!!").decode() + ".png",
])
def test_dlp_ignores_ordinary_urls(url):
    assert _protected().match(url) is None


def _locked_guard(*, referenced=(), strict=False):
    g, page = _guard(strict=strict)
    g.decide(_req(page, OFFICIAL + "/pixel.gif?seen=1", rtype="image"))  # requested before filling began
    g.lock(referenced)
    g.protect_value(EMAIL)
    g.protect_file(RESUME, "resume.pdf")
    return g, page


def test_guard_blocks_same_host_gets_carrying_typed_data_or_file_chunks():
    """Reviewer repro (SEC-1): same-host image / fetch GETs with the e-mail or résumé chunks after filling."""
    g, page = _locked_guard()
    leak = f"https://careers.acme.com/collect/img?d={quote(EMAIL)}"
    assert "carries typed data" in g.decide(_req(page, leak, rtype="image"))
    chunk = "https://careers.acme.com/collect/file?i=0&c=" + quote(base64.b64encode(RESUME[:90]).decode())
    assert "carries uploaded file content" in g.decide(_req(page, chunk, rtype="image"))
    path_leak = f"https://careers.acme.com/collect/{quote(EMAIL)}.png"  # no query string: content check
    assert "carries typed data" in g.decide(_req(page, path_leak, rtype="image"))
    assert len(g.leaks) == 3 and all("test.candidate" not in u and "?" not in u for u in g.leaks)
    assert g.safe_url(path_leak) == "https://careers.acme.com/[typed data removed]"


def test_guard_blocks_every_navigation_after_filling_began():
    g, page = _locked_guard()
    # GET form posted into an iframe (reviewer repro), to the job's own host
    form_get = "https://careers.acme.com/collect/form?d=x"
    assert "frame navigation after filling began" in g.decide(_req(page, form_get, nav=True, iframe=True,
                                                                     rtype="document"))
    assert g.violations == []
    # top-level navigation: aborted and stops the session; the recorded URL has no query / typed data
    nav = f"https://careers.acme.com/next?d={quote(EMAIL)}"
    assert "navigation after filling began" in g.decide(_req(page, nav, nav=True, rtype="document"))
    with pytest.raises(NavigationBlocked) as exc:
        g.raise_if_violated()
    assert "test.candidate" not in exc.value.url and "?" not in exc.value.url


def test_guard_structural_rules_after_filling_began():
    """Data a content check cannot recognise (2-character chunks, transformations) has no channel left except
    a few new asset paths."""
    g, page = _locked_guard(referenced=[OFFICIAL + "/lazy.png?v=7#frag"])
    assert "query string" in g.decide(_req(page, OFFICIAL + "/c?i=0&c=te", rtype="image"))
    assert "query string" in g.decide(_req(page, OFFICIAL + "/c?", rtype="image"))
    assert "new fetch request" in g.decide(_req(page, OFFICIAL + "/api/state", rtype="fetch"))
    assert "new eventsource request" in g.decide(_req(page, OFFICIAL + "/sse", rtype="eventsource"))
    # known before filling: requested earlier, or referenced by the page (fragment ignored)
    assert g.decide(_req(page, OFFICIAL + "/pixel.gif?seen=1", rtype="image")) is None
    assert g.decide(_req(page, OFFICIAL + "/lazy.png?v=7", rtype="image")) is None
    # a few new static assets without a query string keep loading ...
    for i in range(g.new_asset_budget):
        assert g.decide(_req(page, f"{OFFICIAL}/chunk{i}.js", rtype="script")) is None
    assert g.decide(_req(page, f"{OFFICIAL}/chunk0.js", rtype="script")) is None  # same URL again
    # ... but not an unbounded number
    assert "too many new requests" in g.decide(_req(page, f"{OFFICIAL}/one-more.css", rtype="stylesheet"))


def test_guard_before_filling_and_after_the_approved_click():
    g, page = _guard()
    leak = f"https://careers.acme.com/collect?d={quote(EMAIL)}"
    # before filling: nothing typed yet, navigations and fetches on the official host are fine
    assert g.decide(_req(page, OFFICIAL + "/step?x=1", nav=True, rtype="document")) is None
    assert g.decide(_req(page, OFFICIAL + "/api/x?y=1", rtype="fetch")) is None
    g.lock()
    g.protect_value(EMAIL)
    assert g.decide(_req(page, leak, rtype="image")) is not None
    g.arm()  # the approved click: you approved sending these answers to the employer
    assert g.released and g.decide(_req(page, leak, rtype="image")) is None
    assert g.decide(_req(page, OFFICIAL + "/thanks?ref=done", nav=True, rtype="document")) is None
    g.disarm()  # non-GET rules are unchanged: nothing more is posted after the approved post
    assert g.decide(_req(page, OFFICIAL + "/autosave", "POST")) is not None
    assert g.decide(_req(page, "https://evil.example.net/p.gif", rtype="image")) is not None  # third party: never


# ------------------------------------------- round 2: visible text / help text ---- #
def test_measured_empty_main_content_never_falls_back_to_page_chrome():
    chrome = "Thank you for applying through our careers site!"
    snap = PageSnapshot(url="https://a.com/apply/2", text=chrome, main_text="", main_measured=True)
    assert detect_confirmation(snap, PageSnapshot(url="https://a.com/apply")) is None
    legacy = PageSnapshot(url="https://a.com/apply/2", text=chrome)  # fakes / old snapshots: text is used
    assert detect_confirmation(legacy, PageSnapshot(url="https://a.com/apply")) is not None


def test_help_text_is_carried_by_fields_and_makes_them_legal():
    from job_agent.applications.field_mapping import field_is_legal

    raw = [{"name": "relocate", "label": "Are you willing to relocate?", "type": "radio", "locator": "#r",
            "help_text": "  Answering Yes also confirms that you will not\n require visa sponsorship. "},
           {"name": "city", "label": "City", "type": "text", "locator": "#c"}]
    relocate, city = build_fields(raw)
    assert relocate.help_text == "Answering Yes also confirms that you will not require visa sponsorship."
    assert relocate.describe()["help_text"] == relocate.help_text
    assert city.help_text is None and city.describe()["help_text"] is None
    assert field_is_legal(relocate) and not field_is_legal(city)
    assert not field_is_legal(FormField("relocate", "Are you willing to relocate?", "radio"))
