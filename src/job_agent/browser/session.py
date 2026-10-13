"""Browser sessions: injectable protocols + the real Playwright (Chromium) implementation.

``BrowserSession.open_application(url, policy)`` opens one application page in
its own browser context under a ``NavigationPolicy``. The guard is installed on
the *context* (``context.route``), so it sees every request of the page, of
every frame in it and of any window it opens, before the request is sent:

* the start URL must be on the job's official host (``policy.check_initial``);
* top-level navigations and iframe navigations off the policy are refused (a
  refused top-level navigation stops the session with ``NavigationBlocked``);
* **every non-GET/HEAD request** -- form posts from any frame, ``fetch``/XHR,
  ``sendBeacon``, pings -- is aborted unless the page was *armed* by the approved
  submit click; armed requests may only go to policy hosts, and the first
  navigation post (the form submission) disarms it again;
* once filling has begun, GET requests may only go to policy hosts (plus static
  files from the job's ATS hosts / ``policy.ATS_ASSET_HOSTS``); before that,
  third-party GETs needed to render the page are allowed unless
  ``block_third_party_requests`` (strict mode, used by the tests) is set;
* **data-loss prevention** from the moment filling begins until the approved
  click, for every host *including the job's own*: every navigation of the
  page or of any frame in it (GET form posts, ``location`` changes, iframe
  loads) is aborted -- a top-level one also stops the session; a URL that was
  requested or referenced by the page before anything was typed may be fetched
  again; any other request is aborted if its decoded URL or body contains a
  typed value or a chunk of an uploaded file (``dlp.ProtectedData``), if it has
  a query string, if it is not a static asset (fetch / XHR / beacon /
  EventSource...), or once ``NEW_ASSET_BUDGET`` new asset URLs were loaded;
* WebSockets are always refused (``context.route_web_socket``), new windows /
  popups are closed and every request from them is aborted;
* server-side redirects are not interceptable in Playwright, so they are
  observed via ``request`` events and the session refuses to continue
  (``NavigationBlocked``) -- such a redirect has loaded a page via GET, nothing
  was typed into it;
* security challenges / login walls raise ``ChallengeDetected`` (never bypassed).

Not covered, because Chromium does not route them through Playwright:
speculation-rules prefetch/prerender, WebSockets opened inside Web Workers,
DNS prefetches and WebRTC. Closing those needs network-level enforcement (a
filtering proxy); SharedWorkers are disabled outright (``CHROMIUM_ARGS``).
Also not covered: a script that encodes typed data in a form the content check
does not decode (e.g. reversed, as character codes) in the *path* of one of
the ``NEW_ASSET_BUDGET`` new asset URLs allowed after filling began.

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
from urllib.parse import urlsplit

from job_agent.browser import forms
from job_agent.browser.detection import (
    PageSnapshot,
    confirmation_problem,
    confirmation_wording,
    detect_challenge,
    detect_confirmation,
    form_still_present,
    not_yet_submitted_wording,
)
from job_agent.browser.dlp import ProtectedData
from job_agent.browser.errors import BrowserUnavailable, ChallengeDetected, NavigationBlocked, PageUnavailable
from job_agent.browser.forms import FormField, SubmitControl, match_option
from job_agent.browser.policy import NavigationPolicy
from job_agent.errors import RecoverableError
from job_agent.logging_config import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import Browser, BrowserContext, Page, Playwright, Request, Route, WebSocketRoute

    from job_agent.config import AppConfig

log = get_logger(__name__)


@dataclass(frozen=True)
class BrowserOptions:
    headless: bool = True
    timeout_seconds: float = 45.0
    max_concurrent: int = 1
    #: Abort every request to hosts outside the policy from the very first page
    #: load (strict / offline mode, used by tests). By default third-party GETs
    #: are allowed only until filling begins, because real ATS pages load their
    #: scripts and styles from CDNs; non-GET requests are never allowed either way.
    block_third_party_requests: bool = False

    @classmethod
    def from_config(cls, config: "AppConfig", *, headless: bool | None = None) -> "BrowserOptions":
        a = config.applications
        return cls(headless=a.headless if headless is None else headless, timeout_seconds=a.browser_timeout_seconds,
                   max_concurrent=a.max_concurrent_browsers)


@dataclass(frozen=True)
class SubmitOutcome:
    kind: str  # confirmed | validation_errors | challenge | navigation_blocked | ambiguous | timeout
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
        """Arm the request guard for the one approved submission and click once."""
        ...

    def disarm(self) -> None:
        """End the approved-submission window: non-GET requests are refused again."""
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

    Never clicks or reloads anything. Order of checks: navigation guard,
    security challenge, validation errors, then a *strict* confirmation
    (``detection.detect_confirmation``). A page that shows errors next to
    confirmation wording, or a review / intermediate step, is ambiguous: it is
    polled until the deadline and then reported as ``ambiguous`` (an unknown
    outcome, never "confirmed"). Transient errors while the page navigates are
    ignored until the deadline. The guard is disarmed when polling ends.
    """
    try:
        return _poll(page, baseline, timeout_seconds=timeout_seconds, interval_seconds=interval_seconds,
                     clock=clock)
    finally:
        disarm = getattr(page, "disarm", None)
        if callable(disarm):
            disarm()


def _poll(page: ApplicationPage, baseline: PageSnapshot, *, timeout_seconds: float, interval_seconds: float,
          clock: Callable[[], float]) -> SubmitOutcome:
    deadline = clock() + timeout_seconds
    last_url = baseline.url
    ambiguity: str | None = None
    while True:
        try:
            page.check_navigation()
        except NavigationBlocked as exc:
            return SubmitOutcome("navigation_blocked", exc.url, detail=exc.reason)
        try:
            snap = page.snapshot()
            last_url = snap.url
            challenge = detect_challenge(snap)
            if challenge:
                return SubmitOutcome("challenge", snap.url, detail=f"{challenge.kind}: {challenge.detail}")
            errors = page.validation_errors(include_constraints=form_still_present(snap, baseline))
            if errors:
                if confirmation_wording(snap, baseline):
                    ambiguity = "the page shows confirmation wording and errors at the same time"
                elif snap.visible_controls or form_still_present(snap, baseline):
                    return SubmitOutcome("validation_errors", snap.url, validation_errors=tuple(errors))
                else:
                    ambiguity = f"the page shows errors but no form: {'; '.join(errors[:2])}"[:300]
            else:
                confirmation = detect_confirmation(snap, baseline)
                if confirmation:
                    return SubmitOutcome("confirmed", confirmation.url, matched_text=confirmation.matched_text)
                if snap.url != baseline.url or confirmation_wording(snap, baseline) \
                        or not_yet_submitted_wording(snap):
                    ambiguity = confirmation_problem(snap, baseline)
        except RecoverableError:
            pass  # page mid-navigation; try again
        if clock() >= deadline:
            if ambiguity:
                return SubmitOutcome("ambiguous", last_url, detail=f"no credible confirmation: {ambiguity}"[:300])
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


#: SharedWorker requests are not routed by Playwright (dedicated workers are), so
#: the API is removed from every page. (Playwright sets no --disable-blink-features.)
CHROMIUM_ARGS: tuple[str, ...] = ("--disable-blink-features=SharedWorker",)


def launch_chromium(pw: "Playwright", *, headless: bool) -> "Browser":
    from playwright.sync_api import Error as PlaywrightError

    explicit = os.environ.get("JOB_AGENT_CHROMIUM_PATH")
    args = list(CHROMIUM_ARGS)
    try:
        if explicit:
            return pw.chromium.launch(headless=headless, executable_path=explicit, args=args)
        try:
            return pw.chromium.launch(headless=headless, args=args)
        except PlaywrightError as exc:
            if "Executable doesn't exist" not in str(exc):
                raise
            fallback = find_installed_chromium()
            if fallback is None:
                raise
            log.warning("bundled Chromium missing; using installed %s", fallback)
            return pw.chromium.launch(headless=headless, executable_path=str(fallback), args=args)
    except PlaywrightError as exc:
        raise BrowserUnavailable(
            f"could not launch Chromium: {forms.first_line(exc)}",
            human_action='Install the browser: pip install -e ".[browser]" && python -m playwright install chromium'
                         + ("" if headless else " (headed mode also needs a display)"),
        ) from exc


# --------------------------------------------------------------------------- #
# Playwright implementation
# --------------------------------------------------------------------------- #
_SAFE_METHODS = frozenset({"GET", "HEAD"})
#: Resource types that may be fetched from ``policy.asset_request_allowed`` hosts after filling began.
_STATIC_TYPES = frozenset({"stylesheet", "image", "media", "font", "script", "texttrack"})
#: New (not requested / referenced before filling began) asset URLs allowed before the approved click.
NEW_ASSET_BUDGET = 16

#: Every URL the document references before anything is typed (elements, inline styles, same-origin CSS).
#: Fetching one of these later cannot carry typed data, so it is exempt from the data-loss checks.
_REFERENCED_URLS_JS = r"""() => {
  const out = new Set();
  const add = (u, base) => {
    if (!u || out.size >= 5000) return;
    try {
      const x = new URL(String(u).trim(), base || document.baseURI);
      if (x.protocol === 'http:' || x.protocol === 'https:') { x.hash = ''; out.add(x.href); }
    } catch (e) {}
  };
  const cssUrls = (text, base) => {
    const re = /url\(\s*(['"]?)([^'")]+)\1\s*\)/g;
    let m;
    while ((m = re.exec(text || '')) && out.size < 5000) add(m[2], base);
  };
  for (const el of document.querySelectorAll('[src], [href], [poster], [data], [srcset], [imagesrcset], [style]')) {
    for (const a of ['src', 'href', 'poster', 'data']) if (el.hasAttribute(a)) add(el.getAttribute(a));
    for (const a of ['srcset', 'imagesrcset']) {
      const v = el.getAttribute(a);
      if (v) v.split(',').forEach(part => add(part.trim().split(/\s+/)[0]));
    }
    if (el.hasAttribute('style')) cssUrls(el.getAttribute('style'));
    if (out.size >= 5000) break;
  }
  const walk = (rules, base, depth) => {
    for (const r of Array.from(rules || [])) {
      try {
        if (r.styleSheet && depth < 4) {
          add(r.href, base);
          try { walk(r.styleSheet.cssRules, r.styleSheet.href || base, depth + 1); } catch (e) {}
        }
        if (r.cssRules) walk(r.cssRules, base, depth + 1);
        cssUrls(r.cssText, base);
      } catch (e) {}
    }
  };
  for (const sheet of Array.from(document.styleSheets)) {
    try { walk(sheet.cssRules, sheet.href || document.baseURI, 0); } catch (e) {}  // cross-origin: not readable
  }
  return Array.from(out);
}"""


def _url_key(url: str) -> str:
    """A URL without its fragment (what the request actually fetches)."""
    return url.split("#", 1)[0]


def _post_body(request: "Request") -> bytes | None:
    try:
        return request.post_data_buffer
    except Exception:  # noqa: BLE001 - body not available for this request type
        return None


def _clip(url: str) -> str:
    """scheme://host[:port]/path of a URL -- never the query string (it may carry typed data)."""
    parts = urlsplit(url)
    if not parts.netloc:
        return url.split("?", 1)[0].split(",", 1)[0][:120]
    return f"{parts.scheme}://{parts.netloc}{parts.path}"[:200]


@dataclass
class _Guard:
    """Request guard for one application page (and everything it opens).

    ``decide`` is the single rule set; see the module docstring. ``locked`` is
    set when filling begins, ``armed_until`` by the approved submit click.
    """

    policy: NavigationPolicy
    strict: bool
    arm_seconds: float = 60.0
    clock: Callable[[], float] = time.monotonic
    page: "Page | None" = None
    locked: bool = False
    armed_until: float | None = None
    violations: list[tuple[str, str]] = field(default_factory=list)
    blocked: list[tuple[str, str, str]] = field(default_factory=list)  # (method, clipped url, reason)
    blocked_posts: list[str] = field(default_factory=list)
    posts: list[str] = field(default_factory=list)
    websockets: list[str] = field(default_factory=list)
    dialogs: list[str] = field(default_factory=list)
    _open_sockets: list["WebSocketRoute"] = field(default_factory=list, repr=False)

    # -- state ----------------------------------------------------------- #
    def lock(self) -> None:
        """Filling begins: from now on nothing may go to third-party hosts."""
        self.locked = True

    def arm(self) -> None:
        """The approved submission click: allow its non-GET request(s) to policy hosts, for a short window."""
        self.locked = True
        self.armed_until = self.clock() + self.arm_seconds

    def disarm(self) -> None:
        self.armed_until = None

    @property
    def armed(self) -> bool:
        return self.armed_until is not None and self.clock() <= self.armed_until

    # -- rules ----------------------------------------------------------- #
    def _owner(self, request: "Request") -> "Page | None":
        try:
            return request.frame.page
        except Exception:  # noqa: BLE001 - service worker / frame not created yet: unknown owner
            return None

    def decide(self, request: "Request") -> str | None:
        """Why ``request`` must be aborted (None = it may be sent)."""
        url = request.url
        method = request.method.upper()
        if self.page is None or self._owner(request) is not self.page:
            return "request from another window, a worker or an unknown frame"
        is_nav = request.is_navigation_request()
        if is_nav:
            main = request.frame.parent_frame is None
            problem = self.policy.navigation_problem(url)
            if problem:
                if main:
                    self.violations.append((url, problem))
                return f"{'navigation' if main else 'frame navigation'} refused: {problem}"
        if method not in _SAFE_METHODS:
            if not self.armed:
                self.blocked_posts.append(_clip(url))
                return f"{method} request before the approved submission"
            problem = self.policy.navigation_problem(url)
            if problem:
                return f"{method} request outside the policy: {problem}"
            if is_nav:
                self.disarm()  # the one approved form post
            self.posts.append(_clip(url))
            return None
        if is_nav or self.policy.request_host_allowed(url):
            return None
        if self.strict:
            return "third-party request (strict mode)"
        if not self.locked:
            return None  # still rendering the page; nothing has been typed yet
        if request.resource_type in _STATIC_TYPES and self.policy.asset_request_allowed(url):
            return None
        return "third-party request after filling began"

    # -- Playwright callbacks -------------------------------------------- #
    def on_route(self, route: "Route", request: "Request") -> None:
        try:
            reason = self.decide(request)
        except Exception as exc:  # noqa: BLE001 - fail closed on any guard error
            reason = f"request guard error ({type(exc).__name__})"
        try:
            if reason is None:
                route.continue_()
                return
            self.blocked.append((request.method, _clip(request.url), reason))
            log.debug("blocked %s %s: %s", request.method, _clip(request.url), reason)
            route.abort("blockedbyclient")
        except Exception as exc:  # noqa: BLE001 - page/context closed while routing
            log.debug("route: %s", exc)

    def on_websocket(self, ws: "WebSocketRoute") -> None:
        # Not calling ws.connect_to_server() means no connection to the server is
        # ever made. Closing the page side from inside this callback would
        # deadlock the sync dispatcher, so it is done later (close_sockets).
        self.websockets.append(_clip(ws.url))
        self.blocked.append(("WEBSOCKET", _clip(ws.url), "WebSockets are never allowed"))
        self._open_sockets.append(ws)

    def close_sockets(self) -> None:
        while self._open_sockets:
            ws = self._open_sockets.pop()
            try:
                ws.close(code=1008, reason="blocked by job-agent")
            except Exception as exc:  # noqa: BLE001 - page already gone
                log.debug("websocket close: %s", exc)

    def on_request(self, request: "Request") -> None:
        # Redirect hops are not routed; observe them here.
        if request.redirected_from is None or self._owner(request) is not self.page:
            return
        try:
            main_nav = request.is_navigation_request() and request.frame.parent_frame is None
        except Exception:  # noqa: BLE001 - frame gone
            return
        problem = self.policy.navigation_problem(request.url)
        if problem and (main_nav or request.method.upper() not in _SAFE_METHODS):
            self.violations.append((request.url, f"redirect: {problem}"))

    def on_new_page(self, new_page: "Page") -> None:
        # New windows/tabs/popups: every request from them is aborted; close them unused.
        if new_page is self.page:
            return
        self.violations.append((_clip(new_page.url or "about:blank"), "the page opened a new window"))
        try:
            new_page.close()
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
        self._guard.lock()
        self.check_navigation()
        forms.fill_field(self._page, fld, value, timeout_ms=min(self._timeout_ms, 10_000))

    def attach_file(self, fld: FormField, path: Path) -> None:
        self._guard.lock()
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
        self._guard.arm()
        self._page.locator(control.locator).first.click(timeout=min(self._timeout_ms, 10_000))

    def disarm(self) -> None:
        self._guard.disarm()

    def check_navigation(self) -> None:
        self._guard.close_sockets()
        self._guard.raise_if_violated()
        current = self._page.url
        problem = self._guard.policy.navigation_problem(current)
        if problem:
            raise NavigationBlocked(current, problem)

    def wait(self, seconds: float) -> None:
        # Never time.sleep(): Playwright's sync dispatcher must keep running.
        self._page.wait_for_timeout(seconds * 1000)
        self._guard.close_sockets()

    @property
    def submitted_posts(self) -> list[str]:
        """Non-GET requests let through while armed (scheme://host/path only)."""
        return list(self._guard.posts)

    @property
    def blocked_posts(self) -> list[str]:
        """Non-GET requests aborted because no approved submission was armed."""
        return list(self._guard.blocked_posts)

    @property
    def blocked_requests(self) -> list[tuple[str, str, str]]:
        """Every aborted request / WebSocket: (method, scheme://host/path, reason)."""
        return list(self._guard.blocked)


class PlaywrightBrowser:
    """One Chromium instance; each application page gets its own guarded context. Use as a context manager."""

    def __init__(self, options: BrowserOptions) -> None:
        self.options = options
        self._sem = _semaphore(options.max_concurrent)
        self._acquired = False
        self._pw_cm: Any = None
        self._pw: "Playwright | None" = None
        self._browser: "Browser | None" = None
        self._contexts: list["BrowserContext"] = []

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
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _guarded_context(self, guard: _Guard) -> "BrowserContext":
        """A fresh context (no cookies, no service workers, no downloads) with the guard installed."""
        if self._browser is None:
            raise BrowserUnavailable("browser session is not started")
        context = self._browser.new_context(accept_downloads=False, java_script_enabled=True,
                                            service_workers="block")
        self._contexts.append(context)
        if not hasattr(context, "route_web_socket"):
            raise BrowserUnavailable("this Playwright version cannot block WebSockets",
                                     human_action='pip install -U "playwright>=1.48"')
        timeout_ms = self.options.timeout_seconds * 1000
        context.set_default_timeout(timeout_ms)
        context.set_default_navigation_timeout(timeout_ms)
        context.route("**/*", guard.on_route)
        context.route_web_socket(re.compile(r".*"), guard.on_websocket)
        context.on("request", guard.on_request)
        return context

    def open_application(self, url: str, policy: NavigationPolicy) -> PlaywrightApplicationPage:
        from playwright.sync_api import Error as PlaywrightError

        policy.check_initial(url)
        if self._browser is None:
            raise BrowserUnavailable("browser session is not started")
        guard = _Guard(policy=policy, strict=self.options.block_third_party_requests,
                       arm_seconds=self.options.timeout_seconds + 15)
        context = self._guarded_context(guard)
        page = context.new_page()
        guard.page = page
        context.on("page", guard.on_new_page)
        page.on("dialog", lambda d: (guard.dialogs.append(d.message[:200]), d.dismiss()))
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
        closers = [lambda c=c: c.close() for c in self._contexts]
        closers += [lambda: self._browser and self._browser.close(),
                    lambda: self._pw_cm and self._pw_cm.__exit__(None, None, None)]
        for closer in closers:
            try:
                closer()
            except Exception as exc:  # noqa: BLE001 - shutting down; log and continue
                log.debug("browser shutdown: %s", exc)
        self._contexts = []
        self._browser = None
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
