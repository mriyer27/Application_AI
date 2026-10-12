"""Browser-layer logic that needs no Chromium: URL policy, page classification, field keys, outcome polling."""

from __future__ import annotations

import pytest

from job_agent.browser.detection import PageSnapshot, detect_challenge, detect_closed_listing, detect_confirmation
from job_agent.browser.errors import ChallengeDetected, NavigationBlocked
from job_agent.browser.fakes import FakeBrowser, FakeForm
from job_agent.browser.forms import FieldOption, FormField, build_fields, clean_label, match_option, value_matches
from job_agent.browser.policy import NavigationPolicy
from job_agent.browser.session import BrowserOptions, poll_outcome
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
    by_url = PageSnapshot(url="https://a.com/apply/thank-you", text="")
    assert detect_confirmation(by_url, PageSnapshot(url="https://a.com/apply")) is not None
    assert detect_confirmation(PageSnapshot(url="https://a.com/apply"), PageSnapshot(url="https://a.com/apply")) is None


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
    fake = FakeBrowser(FakeForm(url="https://a.com/apply", fields=[], outcome=outcome))
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
