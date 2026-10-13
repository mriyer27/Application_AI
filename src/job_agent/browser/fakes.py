"""In-memory test double for the browser layer (no Chromium, no network).

``FakeBrowser`` implements ``BrowserSession`` and doubles as a
``BrowserFactory`` (``browser_factory=fake``). It applies the same
``NavigationPolicy`` and challenge detection as the real session, records
what was filled / uploaded and counts submit clicks, and simulates scripted
outcomes after the click. Used by unit tests and safe for dashboard demos.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from job_agent.browser.detection import PageSnapshot
from job_agent.browser.errors import FillError, NavigationBlocked, PageUnavailable
from job_agent.browser.forms import FALSY, TEXT_TYPES, TRUTHY, FormField, SubmitControl, match_option, norm
from job_agent.browser.policy import NavigationPolicy
from job_agent.browser.session import BrowserOptions, ensure_no_challenge

#: confirm: a thank-you page; validation: the form shows errors; hang: nothing changes;
#: challenge: a CAPTCHA appears; offhost: the page navigates off the policy;
#: review: a "review your application - nothing has been sent yet" step;
#: screening: "before your application is submitted, answer..." plus a validation error.
OUTCOMES = {"confirm", "validation", "hang", "challenge", "offhost", "review", "screening"}

_AFTER_SUBMIT_PAGES = {
    "review": ("/apply/confirm", "Review your application",
               "Review your application. Nothing has been sent yet. Check your answers, then confirm."),
    "screening": ("/apply/screening", "Screening questions",
                  "Before your application is submitted, answer the screening questions below."),
}


@dataclass
class FakeForm:
    url: str
    fields: list[FormField]
    title: str = "Apply"
    text: str = "Apply for this job"
    iframe_srcs: tuple[str, ...] = ()
    script_srcs: tuple[str, ...] = ()
    markers: tuple[str, ...] = ()
    visible_password_fields: int = 0
    status: int = 200
    validation_errors_before: list[str] = field(default_factory=list)
    validation_errors_after: list[str] = field(default_factory=lambda: ["Phone number is invalid"])
    submit: SubmitControl | None = field(default_factory=lambda: SubmitControl("button[type=submit]",
                                                                             "Submit application"))
    outcome: str = "confirm"
    redirect_to: str | None = None
    refuse_fill: set[str] = field(default_factory=set)
    blocked_leaks: list[str] = field(default_factory=list)  # requests the data-loss guard would abort
    #: What the form shows once something was typed into it (a script rewriting questions); None = unchanged.
    fields_after_fill: list[FormField] | None = None

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"unknown fake outcome {self.outcome!r}")


class FakePage:
    def __init__(self, browser: "FakeBrowser", form: FakeForm, url: str) -> None:
        self.browser = browser
        self.form = form
        self._url = url
        self.values: dict[str, str] = {}
        self.submitted = False
        self.armed = False

    @property
    def url(self) -> str:
        return self._url

    @property
    def blocked_leaks(self) -> list[str]:
        return list(self.form.blocked_leaks)

    def snapshot(self) -> PageSnapshot:
        f = self.form
        base = self._url.split("?")[0].rsplit("/", 1)[0]
        if self.submitted and f.outcome == "confirm":
            text = "Thank you for applying! Your application has been received."
            return PageSnapshot(url=base + "/thank-you", title="Thanks", text=text, main_text=text, status=200)
        if self.submitted and f.outcome in _AFTER_SUBMIT_PAGES:
            path, title, text = _AFTER_SUBMIT_PAGES[f.outcome]
            controls = ("screening_q1",) if f.outcome == "screening" else ()
            return PageSnapshot(url=base + path, title=title, text=text, main_text=text, status=200,
                                form_control_count=len(controls), visible_controls=controls)
        markers = f.markers + ((".g-recaptcha",) if self.submitted and f.outcome == "challenge" else ())
        controls = tuple(dict.fromkeys(fld.name or fld.key for fld in f.fields if fld.visible))
        return PageSnapshot(url=self._url, title=f.title, text=f.text, main_text=f.text, iframe_srcs=f.iframe_srcs,
                            script_srcs=f.script_srcs, markers=markers,
                            visible_password_fields=f.visible_password_fields,
                            form_control_count=len(f.fields), status=f.status, visible_controls=controls)

    def inspect_form(self) -> list[FormField]:
        self.check_navigation()
        if self.values and self.form.fields_after_fill is not None:
            return list(self.form.fields_after_fill)
        return list(self.form.fields)

    def fill(self, fld: FormField, value: str) -> None:
        self.check_navigation()
        if fld.key in self.form.refuse_fill:
            raise FillError(f"{fld.key}: widget did not accept the value")
        if fld.type in {"select", "radio"}:
            option = match_option(value, fld.options)
            if option is None:
                raise FillError(f"{fld.key}: {value!r} is not one of the available options")
            stored = option.label
        elif fld.type == "checkbox":
            if norm(value) not in TRUTHY | FALSY:
                raise FillError(f"{fld.key}: checkbox answer must be yes/no")
            stored = "yes" if norm(value) in TRUTHY else "no"
        elif fld.type in TEXT_TYPES or fld.type in {"checkbox_group", "select-multiple"}:
            stored = value
        else:
            raise FillError(f"{fld.key}: unsupported field type {fld.type!r}")
        self.values[fld.key] = stored
        self.browser.filled[fld.key] = stored

    def attach_file(self, fld: FormField, path: Path) -> None:
        self.check_navigation()
        if not fld.is_file:
            raise FillError(f"{fld.key} is not a file input")
        self.values[fld.key] = path.name
        self.browser.uploaded[fld.key] = path

    def read_value(self, fld: FormField) -> str | None:
        if fld.type == "checkbox":
            return self.values.get(fld.key, "no")
        return self.values.get(fld.key, "")

    def validation_errors(self, *, include_constraints: bool = False) -> list[str]:
        if self.submitted and self.form.outcome == "validation":
            return list(self.form.validation_errors_after)
        if self.submitted and self.form.outcome == "screening":
            return ["Please answer this question"]
        if self.submitted and self.form.outcome in {"confirm", "review"}:
            return []
        return list(self.form.validation_errors_before)

    def screenshot(self, path: Path) -> Path | None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG fake screenshot")
        self.browser.screenshots.append(path)
        return path

    def find_submit(self) -> SubmitControl | None:
        return self.form.submit

    def click_submit(self, control: SubmitControl) -> None:
        self.check_navigation()
        self.armed = True
        self.submitted = True
        self.browser.submissions += 1
        self.browser.submitted_values.append(dict(self.values))

    def disarm(self) -> None:
        self.armed = False

    def check_navigation(self) -> None:
        if self.submitted and self.form.outcome == "offhost":
            raise NavigationBlocked("https://elsewhere.invalid/", "host 'elsewhere.invalid' is not the job's host")

    def wait(self, seconds: float) -> None:
        time.sleep(min(seconds, 0.02))


class FakeBrowser:
    def __init__(self, *forms: FakeForm) -> None:
        self.forms = {f.url: f for f in forms}
        self.options: BrowserOptions | None = None
        self.opened: list[str] = []
        self.filled: dict[str, str] = {}
        self.uploaded: dict[str, Path] = {}
        self.screenshots: list[Path] = []
        self.submissions = 0
        self.submitted_values: list[dict[str, str]] = []
        self.sessions = 0
        self.pages: list[FakePage] = []

    # BrowserFactory
    def __call__(self, options: BrowserOptions) -> "FakeBrowser":
        self.options = options
        return self

    def __enter__(self) -> "FakeBrowser":
        self.sessions += 1
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        return None

    def open_application(self, url: str, policy: NavigationPolicy) -> FakePage:
        policy.check_initial(url)
        self.opened.append(url)
        form = self.forms.get(url)
        if form is None:
            raise PageUnavailable(f"{url} returned HTTP 404")
        page = FakePage(self, form, url)
        self.pages.append(page)
        if form.redirect_to:
            problem = policy.navigation_problem(form.redirect_to)
            if problem:
                raise NavigationBlocked(form.redirect_to, f"redirect: {problem}")
            page._url = form.redirect_to
        ensure_no_challenge(page)
        if form.status in {404, 410}:
            raise PageUnavailable(f"{url} returned HTTP {form.status}")
        return page
