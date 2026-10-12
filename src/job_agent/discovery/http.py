"""Polite HTTP client for public job-board APIs.

Rules enforced here:

* a minimum interval between requests to the same host
  (``sources.min_request_interval_seconds``),
* a request timeout and a bounded number of retries with exponential backoff,
  only for timeouts / connection errors / 5xx / 429; ``Retry-After`` is honored
  up to a cap -- if a server asks us to wait longer, we stop and report a
  recoverable error instead of retrying early,
* an honest ``User-Agent`` (optionally with the user's contact email),
* no cookies, no credentials, no attempts to get around access controls:
  401/403 and anti-bot challenge pages stop the source immediately.
"""

from __future__ import annotations

import email.utils
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from job_agent.config import SourcesSection
from job_agent.errors import HumanActionRequired, PermanentError, RecoverableError
from job_agent.logging_config import get_logger

log = get_logger(__name__)

#: Longest ``Retry-After`` we are willing to wait inside one run.
MAX_RETRY_AFTER_SECONDS = 60.0
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0
#: Refuse absurdly large responses (the largest real boards are a few MB).
MAX_RESPONSE_BYTES = 60_000_000

_CHALLENGE_RE = re.compile(
    r"(captcha|cf-chl|challenge-platform|cf-browser-verification|attention required|px-captcha|"
    r"are you a robot|verify you are human|bot detection)",
    re.I,
)


def build_user_agent(base: str, contact_email: str | None) -> str:
    base = (base or "job-agent").strip()
    if contact_email and contact_email.strip():
        return f"{base} (contact: {contact_email.strip()})"
    return base


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Seconds to wait from a ``Retry-After`` header (delta-seconds or HTTP date)."""
    if not value:
        return None
    text = value.strip()
    if text.isdigit():
        return float(text)
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = (when - (now or datetime.now(timezone.utc))).total_seconds()
    return max(0.0, delta)


class PoliteHttpClient:
    """Rate-limited, retrying JSON GET client. Inject ``client``/``transport``,
    ``sleep`` and ``clock`` in tests."""

    def __init__(
        self,
        settings: SourcesSection,
        *,
        contact_email: str | None = None,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_retry_after: float = MAX_RETRY_AFTER_SECONDS,
        backoff_base: float = BACKOFF_BASE_SECONDS,
    ) -> None:
        self.settings = settings
        self.user_agent = build_user_agent(settings.user_agent, contact_email)
        self.min_interval = float(settings.min_request_interval_seconds)
        self.max_retries = int(settings.max_retries)
        self.max_retry_after = max_retry_after
        self.backoff_base = backoff_base
        self._sleep = sleep
        self._clock = clock
        self._owns_client = client is None
        self._client = client or httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(settings.request_timeout_seconds),
            follow_redirects=True,
            max_redirects=5,
        )
        self._last_request_at: dict[str, float] = {}
        self.request_count = 0

    # ------------------------------------------------------------------ #
    def __enter__(self) -> "PoliteHttpClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # ------------------------------------------------------------------ #
    def _wait_for_host(self, host: str) -> None:
        last = self._last_request_at.get(host)
        if last is None:
            return
        remaining = self.min_interval - (self._clock() - last)
        if remaining > 0:
            self._sleep(remaining)

    def _backoff(self, attempt: int) -> float:
        return min(BACKOFF_MAX_SECONDS, self.backoff_base * (2 ** (attempt - 1)))

    def get(self, url: str, *, params: Mapping[str, Any] | None = None) -> httpx.Response:
        """GET with politeness rules. Raises classified ``job_agent.errors``."""
        host = _host(url)
        if urlsplit(url).scheme != "https":
            raise PermanentError(f"refusing non-HTTPS source URL for {host or url}")
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        attempts = self.max_retries + 1
        last_problem = "no attempt made"
        for attempt in range(1, attempts + 1):
            self._wait_for_host(host)
            # Never send cookies a previous response may have set.
            self._client.cookies.clear()
            try:
                response = self._client.get(url, params=dict(params or {}), headers=headers)
            except httpx.TimeoutException as exc:
                last_problem = f"timeout ({type(exc).__name__})"
                response = None
            except httpx.TransportError as exc:
                last_problem = f"connection error ({type(exc).__name__})"
                response = None
            finally:
                self._last_request_at[host] = self._clock()
                self.request_count += 1
            self._client.cookies.clear()

            if response is None:
                if attempt < attempts:
                    delay = self._backoff(attempt)
                    log.info("GET %s failed (%s); retry %d/%d in %.1fs", host, last_problem, attempt,
                             self.max_retries, delay)
                    self._sleep(delay)
                    continue
                raise RecoverableError(f"{host}: {last_problem} after {attempts} attempt(s)")

            status = response.status_code
            if 200 <= status < 300:
                return response
            self._raise_if_challenge(host, response)
            if status in (401, 403):
                raise PermanentError(
                    f"{host}: access denied (HTTP {status}) - not bypassing",
                    human_action="Check the board identifier in config. The source refused access; "
                                 "job-agent never tries to get around that.",
                )
            if status in (404, 410):
                raise PermanentError(
                    f"{host}{urlsplit(url).path}: board not found (HTTP {status})",
                    human_action="Check the board token / company slug in config (see docs/sources.md).",
                )
            if status == 429 or status >= 500:
                last_problem = f"HTTP {status}"
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                if retry_after is not None and retry_after > self.max_retry_after:
                    raise RecoverableError(
                        f"{host}: rate limited (HTTP {status}); server asked to retry after "
                        f"{retry_after:.0f}s, longer than the {self.max_retry_after:.0f}s we wait in one run"
                    )
                if attempt < attempts:
                    delay = retry_after if retry_after is not None else self._backoff(attempt)
                    log.info("GET %s returned HTTP %d; retry %d/%d in %.1fs", host, status, attempt,
                             self.max_retries, delay)
                    self._sleep(delay)
                    continue
                raise RecoverableError(f"{host}: {last_problem} after {attempts} attempt(s)")
            raise PermanentError(f"{host}: unexpected HTTP {status}")
        raise RecoverableError(f"{host}: {last_problem}")  # pragma: no cover - loop always returns/raises

    def get_json(self, url: str, *, params: Mapping[str, Any] | None = None) -> Any:
        response = self.get(url, params=params)
        host = _host(url)
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise PermanentError(f"{host}: response larger than {MAX_RESPONSE_BYTES} bytes; refusing")
        try:
            return response.json()
        except ValueError:
            self._raise_if_challenge(host, response, force=True)
            raise RecoverableError(f"{host}: response was not valid JSON") from None

    @staticmethod
    def _raise_if_challenge(host: str, response: httpx.Response, *, force: bool = False) -> None:
        ctype = response.headers.get("content-type", "")
        if "html" not in ctype and not force:
            return
        sample = response.text[:5000] if response.content else ""
        if _CHALLENGE_RE.search(sample):
            raise HumanActionRequired(
                f"{host}: anti-bot / CAPTCHA challenge detected (HTTP {response.status_code}); not bypassing",
                human_action="Remove this source or check it manually in a browser; job-agent will not "
                             "solve or bypass challenges.",
            )
