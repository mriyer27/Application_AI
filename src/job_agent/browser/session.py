"""Browser sessions: injectable protocols + the real Playwright (Chromium) implementation.

``BrowserSession.open_application(url, policy)`` opens one application page
under a ``NavigationPolicy``:

* the start URL must be on the job's official host (``policy.check_initial``);
* every top-level navigation is intercepted *before* the request is sent and
  refused if off-host; top-level non-GET navigations (form posts) are refused
  unless the page was explicitly *armed* for the one approved submission click;
* server-side redirects are not interceptable in Playwright, so they are
  observed via ``request`` events and the session refuses to continue
  (``NavigationBlocked``) -- such a redirect has loaded a page via GET, nothing
  was typed into it;
* security challenges / login walls raise ``ChallengeDetected`` (never bypassed).

At most ``max_concurrent`` sessions run per process (a semaphore). The Playwright
sync API must not be used from inside an asyncio event loop; call it from a
worker thread (e.g. FastAPI's threadpool).
"""

from __future__ import annotations

import glob
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol

from job_agent.browser import forms
from job_agent.browser.detection import PageSnapshot, detect_challenge, detect_confirmation
from job_agent.browser.errors import BrowserUnavailable, ChallengeDetected, NavigationBlocked, PageUnavailable
from job_agent.browser.forms import FormField, SubmitControl
from job_agent.browser.policy import NavigationPolicy
from job_agent.errors import RecoverableError
from job_agent.logging_config import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import Browser, BrowserContext, Page, Playwright, Request, Route

    from job_agent.config import AppConfig

log = get_logger(__name__)


@dataclass(frozen=True)
class BrowserOptions:
    headless: bool = True
    timeout_seconds: float = 45.0
    max_concurrent: int = 1
    #: Abort *every* request (not only navigations) to hosts outside the policy.
    #: Used by tests to guarantee no real network; real ATS pages usually need
    #: their CDNs, so it is off by default.
    block_third_party_requests: bool = False

    @classmethod
    def from_config(cls, config: "AppConfig", *, headless: bool | None = None) -> "BrowserOptions":
        a = config.applications
        return cls(headless=a.headless if headless is None else headless, timeout_seconds=a.browser_timeout_seconds,
                   max_concurrent=a.max_concurrent_browsers)


@dataclass(frozen=True)
class SubmitOutcome:
    kind: str  # confirmed | validation_errors | challenge | navigation_blocked | timeout
    url: str
    matched_text: str | None = None
    validation_errors: tuple[str, ...] = ()
    detail: str | None = None


class ApplicationPage(Protocol):
    @property
    def url(self) -> str: ...

    def snapshot(self) -> PageSnapshot: ...

    def inspect_form(self) -> list[FormField]: ...

    def fill(self, fld: FormField, value: str) -> None: ...

    def attach_file(self, fld: FormField, path: Path) -> None: ...

    def read_value(self, fld: FormField) -> str | None: ...

    def validation_errors(self, *, include_constraints: bool = False) -> list[str]: ...

    def screenshot(self, path: Path) -> Path | None: ...

    def find_submit(self) -> SubmitControl | None: ...

    def click_submit(self, control: SubmitControl) -> None:
        """Arm the navigation guard for exactly one form post and click once."""
        ...

    def check_navigation(self) -> None:
        """Raise ``NavigationBlocked`` if the page navigated/redirected off-policy."""
        ...

    def wait(self, seconds: float) -> None: ...


class BrowserSession(Protocol):
    def open_application(self, url: str, policy: NavigationPolicy) -> ApplicationPage: ...

    def close(self) -> None: ...

    def __enter__(self) -> "BrowserSession": ...

    def __exit__(self, *exc: object) -> None: ...


BrowserFactory = Callable[[BrowserOptions], BrowserSession]


def ensure_no_challenge(page: ApplicationPage, snap: PageSnapshot | None = None) -> PageSnapshot:
    snap = snap or page.snapshot()
    challenge = detect_challenge(snap)
    if challenge:
        raise ChallengeDetected(challenge.kind, challenge.detail, snap.url)
    return snap


def poll_outcome(page: ApplicationPage, baseline: PageSnapshot, *, timeout_seconds: float,
                 interval_seconds: float = 0.5, clock: Callable[[], float] = time.monotonic) -> SubmitOutcome:
    """Watch the page after the submit click until a credible outcome or the timeout.

    Never clicks or reloads anything. Transient errors while the page navigates
    are ignored until the deadline.
    """
    deadline = clock() + timeout_seconds
    last_url = baseline.url
    while True:
        try:
            page.check_navigation()
        except NavigationBlocked as exc:
            return SubmitOutcome("navigation_blocked", exc.url, detail=exc.reason)
        try:
            snap = page.snapshot()
            last_url = snap.url
            confirmation = detect_confirmation(snap, baseline)
            if confirmation:
                return SubmitOutcome("confirmed", confirmation.url, matched_text=confirmation.matched_text)
            challenge = detect_challenge(snap)
            if challenge:
                return SubmitOutcome("challenge", snap.url, detail=f"{challenge.kind}: {challenge.detail}")
            errors = page.validation_errors(include_constraints=True)
            if errors:
                return SubmitOutcome("validation_errors", snap.url, validation_errors=tuple(errors))
        except RecoverableError:
            pass  # page mid-navigation; try again
        if clock() >= deadline:
            return SubmitOutcome("timeout", last_url,
                                 detail=f"no confirmation or error within {timeout_seconds:.0f}s")
        page.wait(interval_seconds)


# --------------------------------------------------------------------------- #
# Concurrency limit
# --------------------------------------------------------------------------- #
_SEMAPHORES: dict[int, threading.BoundedSemaphore] = {}
_SEM_LOCK = threading.Lock()


def _semaphore(limit: int) -> threading.BoundedSemaphore:
    with _SEM_LOCK:
        if limit not in _SEMAPHORES:
            _SEMAPHORES[limit] = threading.BoundedSemaphore(limit)
        return _SEMAPHORES[limit]


# --------------------------------------------------------------------------- #
# Chromium discovery (the bundled revision may differ from the installed one)
# --------------------------------------------------------------------------- #
_REV_RE = re.compile(r"chromium-(\d+)")


def find_installed_chromium() -> Path | None:
    """Newest Chromium installed under PLAYWRIGHT_BROWSERS_PATH / the default cache."""
    roots = []
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        roots.append(Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]))
    home = Path.home()
    roots += [home / ".cache" / "ms-playwright", home / "Library" / "Caches" / "ms-playwright",
              home / "AppData" / "Local" / "ms-playwright"]
    patterns = ["chromium-*/chrome-linux*/chrome", "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
                "chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                "chromium-*/chrome-win*/chrome.exe"]
    found: list[tuple[int, Path]] = []
    for root in roots:
        for pattern in patterns:
            for match in glob.glob(str(root / pattern)):
                m = _REV_RE.search(match)
                if m and os.access(match, os.X_OK):
                    found.append((int(m.group(1)), Path(match)))
    return max(found)[1] if found else None


def launch_chromium(pw: "Playwright", *, headless: bool) -> "Browser":
    from playwright.sync_api import Error as PlaywrightError

    explicit = os.environ.get("JOB_AGENT_CHROMIUM_PATH")
    try:
        if explicit:
            return pw.chromium.launch(headless=headless, executable_path=explicit)
        try:
            return pw.chromium.launch(headless=headless)
        except PlaywrightError as exc:
            if "Executable doesn't exist" not in str(exc):
                raise
            fallback = find_installed_chromium()
            if fallback is None:
                raise
            log.warning("bundled Chromium missing; using installed %s", fallback)
            return pw.chromium.launch(headless=headless, executable_path=str(fallback))
    except PlaywrightError as exc:
        raise BrowserUnavailable(
            f"could not launch Chromium: {forms.first_line(exc)}",
            human_action='Install the browser: pip install -e ".[browser]" && python -m playwright install chromium'
                         + ("" if headless else " (headed mode also needs a display)"),
        ) from exc


# --------------------------------------------------------------------------- #
# Playwright implementation
# --------------------------------------------------------------------------- #
@dataclass
class _Guard:
    policy: NavigationPolicy
    strict: bool
    armed: bool = False
    violations: list[tuple[str, str]] = field(default_factory=list)
    blocked_posts: list[str] = field(default_factory=list)
    posts: list[str] = field(default_factory=list)
    dialogs: list[str] = field(default_factory=list)

    @staticmethod
    def _main_frame_nav(request: "Request") -> bool:
        try:
            return request.is_navigation_request() and request.frame.parent_frame is None
        except Exception:  # noqa: BLE001 - service-worker requests have no frame
            return False

    def on_route(self, route: "Route", request: "Request") -> None:
        url = request.url
        if self._main_frame_nav(request):
            problem = self.policy.navigation_problem(url)
            if problem:
                self.violations.append((url, problem))
                route.abort("blockedbyclient")
                return
            if request.method not in {"GET", "HEAD"}:
                if not self.armed:
                    self.blocked_posts.append(url)
                    route.abort("blockedbyclient")
                    return
                self.armed = False  # one approved post only
                self.posts.append(url)
        elif self.strict and not self.policy.request_host_allowed(url):
            route.abort("blockedbyclient")
            return
        route.continue_()

    def on_request(self, request: "Request") -> None:
        # Redirect hops are not routed; observe them here.
        if request.redirected_from is not None and self._main_frame_nav(request):
            problem = self.policy.navigation_problem(request.url)
            if problem:
                self.violations.append((request.url, f"redirect: {problem}"))

    def on_popup(self, popup: "Page") -> None:
        # New windows/tabs are not under this guard: close them unused.
        self.violations.append((popup.url or "about:blank", "the page opened a new window"))
        try:
            popup.close()
        except Exception as exc:  # noqa: BLE001 - already closed
            log.debug("popup close: %s", exc)

    def raise_if_violated(self) -> None:
        if self.violations:
            url, reason = self.violations[0]
            raise NavigationBlocked(url, reason)


class PlaywrightApplicationPage:
    def __init__(self, page: "Page", guard: _Guard, timeout_ms: float, status: int | None) -> None:
        self._page = page
        self._guard = guard
        self._timeout_ms = timeout_ms
        self._status = status

    @property
    def url(self) -> str:
        return self._page.url

    @property
    def raw_page(self) -> "Page":
        return self._page

    def _call(self, fn: Callable[[], Any]) -> Any:
        from playwright.sync_api import Error as PlaywrightError

        try:
            return fn()
        except PlaywrightError as exc:
            raise RecoverableError(f"browser error: {forms.first_line(exc)}") from exc

    def snapshot(self) -> PageSnapshot:
        return self._call(lambda: forms.take_snapshot(self._page, self._status))

    def inspect_form(self) -> list[FormField]:
        self.check_navigation()
        return self._call(lambda: forms.inspect_form(self._page))

    def fill(self, fld: FormField, value: str) -> None:
        self.check_navigation()
        forms.fill_field(self._page, fld, value, timeout_ms=min(self._timeout_ms, 10_000))

    def attach_file(self, fld: FormField, path: Path) -> None:
        self.check_navigation()
        forms.attach_file(self._page, fld, path, timeout_ms=min(self._timeout_ms, 10_000))

    def read_value(self, fld: FormField) -> str | None:
        return forms.read_field_value(self._page, fld)

    def validation_errors(self, *, include_constraints: bool = False) -> list[str]:
        return self._call(lambda: forms.visible_validation_errors(self._page,
                                                                  include_constraints=include_constraints))

    def screenshot(self, path: Path) -> Path | None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._call(lambda: self._page.screenshot(path=str(path), full_page=True, timeout=self._timeout_ms))
        return path

    def find_submit(self) -> SubmitControl | None:
        return self._call(lambda: forms.find_submit_control(self._page))

    def click_submit(self, control: SubmitControl) -> None:
        self.check_navigation()
        self._guard.armed = True
        self._page.locator(control.locator).first.click(timeout=min(self._timeout_ms, 10_000))

    def check_navigation(self) -> None:
        self._guard.raise_if_violated()
        current = self._page.url
        problem = self._guard.policy.navigation_problem(current)
        if problem:
            raise NavigationBlocked(current, problem)

    def wait(self, seconds: float) -> None:
        # Never time.sleep(): Playwright's sync dispatcher must keep running.
        self._page.wait_for_timeout(seconds * 1000)

    @property
    def submitted_posts(self) -> list[str]:
        return list(self._guard.posts)

    @property
    def blocked_posts(self) -> list[str]:
        return list(self._guard.blocked_posts)


class PlaywrightBrowser:
    """One Chromium instance + context; use as a context manager."""

    def __init__(self, options: BrowserOptions) -> None:
        self.options = options
        self._sem = _semaphore(options.max_concurrent)
        self._acquired = False
        self._pw_cm: Any = None
        self._pw: "Playwright | None" = None
        self._browser: "Browser | None" = None
        self._context: "BrowserContext | None" = None

    def __enter__(self) -> "PlaywrightBrowser":
        if not self._sem.acquire(timeout=self.options.timeout_seconds):
            raise RecoverableError("another browser session is running (applications.max_concurrent_browsers)")
        self._acquired = True
        try:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise BrowserUnavailable(
                    "Playwright is not installed", human_action='pip install -e ".[browser]"') from exc
            self._pw_cm = sync_playwright()
            self._pw = self._pw_cm.__enter__()
            self._browser = launch_chromium(self._pw, headless=self.options.headless)
            self._context = self._browser.new_context(accept_downloads=False, java_script_enabled=True,
                                                      service_workers="block")
            timeout_ms = self.options.timeout_seconds * 1000
            self._context.set_default_timeout(timeout_ms)
            self._context.set_default_navigation_timeout(timeout_ms)
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def open_application(self, url: str, policy: NavigationPolicy) -> PlaywrightApplicationPage:
        from playwright.sync_api import Error as PlaywrightError

        policy.check_initial(url)
        if self._context is None:
            raise BrowserUnavailable("browser session is not started")
        guard = _Guard(policy=policy, strict=self.options.block_third_party_requests)
        page = self._context.new_page()
        page.route("**/*", guard.on_route)
        page.on("request", guard.on_request)
        page.on("dialog", lambda d: (guard.dialogs.append(d.message[:200]), d.dismiss()))
        page.on("popup", guard.on_popup)
        timeout_ms = self.options.timeout_seconds * 1000
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except PlaywrightError as exc:
            guard.raise_if_violated()
            raise RecoverableError(f"could not load {url}: {forms.first_line(exc)}",
                                   human_action="Check the URL in a normal browser; retry later.") from exc
        _best_effort(lambda: page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 5000)))
        _best_effort(lambda: page.wait_for_selector("input, select, textarea", state="attached",
                                                    timeout=min(timeout_ms, 3000)))
        status = response.status if response is not None else None
        app_page = PlaywrightApplicationPage(page, guard, timeout_ms, status)
        app_page.check_navigation()
        ensure_no_challenge(app_page)
        if status is not None and status >= 500:
            raise RecoverableError(f"{url} returned HTTP {status}")
        if status in {404, 410}:
            raise PageUnavailable(f"{url} returned HTTP {status}; the listing may be closed",
                                  human_action="Check the posting on the employer's site.")
        return app_page

    def close(self) -> None:
        for closer in (lambda: self._context and self._context.close(),
                       lambda: self._browser and self._browser.close(),
                       lambda: self._pw_cm and self._pw_cm.__exit__(None, None, None)):
            try:
                closer()
            except Exception as exc:  # noqa: BLE001 - shutting down; log and continue
                log.debug("browser shutdown: %s", exc)
        self._context = self._browser = None
        self._pw = self._pw_cm = None
        if self._acquired:
            self._acquired = False
            self._sem.release()


def _best_effort(fn: Callable[[], Any]) -> None:
    from playwright.sync_api import Error as PlaywrightError

    try:
        fn()
    except PlaywrightError:
        pass  # optional wait (network idle / form render); the page is inspected regardless


def default_factory(options: BrowserOptions) -> BrowserSession:
    return PlaywrightBrowser(options)


def chromium_launchable(headless: bool = True) -> tuple[bool, str]:
    """Can a Chromium be launched here? (doctor / test skip helper)."""
    try:
        with PlaywrightBrowser(BrowserOptions(headless=headless, timeout_seconds=30)):
            return True, "ok"
    except Exception as exc:  # noqa: BLE001 - diagnostic helper
        return False, f"{type(exc).__name__}: {exc}"


__all__ = [
    "ApplicationPage", "BrowserFactory", "BrowserOptions", "BrowserSession", "PlaywrightApplicationPage",
    "PlaywrightBrowser", "SubmitOutcome", "chromium_launchable", "default_factory", "ensure_no_challenge",
    "find_installed_chromium", "launch_chromium", "poll_outcome",
]
