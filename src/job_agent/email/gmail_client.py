"""Thin, read-only wrapper over the Gmail API.

The wrapper exposes exactly two operations -- list message ids and get one
message -- and has no send / draft / modify / trash / label method. Even if a
bug or a malicious email tried to make the agent act on a mailbox, there is no
code path for it here, and the OAuth scope is read-only on top of that.

The underlying ``service`` object is injectable: tests pass a fake that
implements ``users().messages().list(...).execute()`` and ``.get(...)``.
"""

from __future__ import annotations

import random
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable

from job_agent.errors import AuthorizationRequired, PermanentError, RecoverableError
from job_agent.logging_config import get_logger

log = get_logger(__name__)

#: HTTP statuses retried with backoff (quota, transient server errors).
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_RATE_LIMIT_REASONS = ("ratelimitexceeded", "userratelimitexceeded", "quotaexceeded", "backenderror")
DEFAULT_TIMEOUT_SECONDS = 30.0
#: Hard cap on list pages per sync (500 ids per page at most).
MAX_LIST_PAGES = 50
#: Headers requested in metadata mode (gmail.metadata scope).
METADATA_HEADERS = ["From", "Subject", "Date", "List-Unsubscribe", "List-Id", "Precedence", "Auto-Submitted",
                    "Authentication-Results"]  # sender verification (DKIM/DMARC) for automatic updates


@dataclass(frozen=True)
class MessageRef:
    id: str
    thread_id: str


class GmailReadClient:
    """Read-only Gmail access: ``list_message_ids`` and ``get_message`` only."""

    def __init__(self, service: Any, *, message_format: str = "full", max_retries: int = 4,
                 backoff_seconds: float = 1.0, max_backoff_seconds: float = 30.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if message_format not in {"full", "metadata"}:
            raise ValueError("message_format must be 'full' or 'metadata'")
        self._service = service
        self._format = message_format
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._sleep = sleep

    @classmethod
    def from_credentials(cls, credentials: Any, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
                         **kwargs: Any) -> "GmailReadClient":
        """Build the official client (bundled discovery document; no extra network call)."""
        try:
            import google_auth_httplib2
            import httplib2
            from googleapiclient.discovery import build
        except ImportError as exc:
            from job_agent.errors import HumanActionRequired

            raise HumanActionRequired(f"Gmail libraries are not installed ({exc})",
                                      human_action='.venv/bin/pip install -e ".[gmail]"') from None
        # An explicit timeout: httplib2's default would let a stalled connection hang the scheduler.
        http = google_auth_httplib2.AuthorizedHttp(credentials, http=httplib2.Http(timeout=timeout_seconds))
        service = build("gmail", "v1", http=http, cache_discovery=False)
        return cls(service, **kwargs)

    @property
    def message_format(self) -> str:
        return self._format

    # ------------------------------------------------------------------ #
    def list_message_ids(self, query: str | None, max_results: int, *,
                         label_ids: list[str] | None = None) -> list[MessageRef]:
        """Ids (newest first) of messages matching ``query``, following pagination."""
        refs: list[MessageRef] = []
        seen: set[str] = set()
        page_token: str | None = None
        for _ in range(MAX_LIST_PAGES):
            remaining = max_results - len(refs)
            if remaining <= 0:
                break
            params: dict[str, Any] = {"userId": "me", "maxResults": min(500, remaining), "includeSpamTrash": False}
            if query:
                params["q"] = query
            if label_ids:
                params["labelIds"] = label_ids
            if page_token:
                params["pageToken"] = page_token
            response = self._execute(lambda p=params: self._service.users().messages().list(**p), "list messages")
            for item in response.get("messages") or []:
                mid = str(item.get("id") or "")
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                refs.append(MessageRef(id=mid, thread_id=str(item.get("threadId") or mid)))
                if len(refs) >= max_results:
                    break
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return refs

    def get_message(self, message_id: str) -> dict[str, Any]:
        """One message resource (``format=full``, or ``metadata`` with the metadata scope)."""
        params: dict[str, Any] = {"userId": "me", "id": message_id, "format": self._format}
        if self._format == "metadata":
            params["metadataHeaders"] = METADATA_HEADERS
        return self._execute(lambda: self._service.users().messages().get(**params), "get message")

    # ------------------------------------------------------------------ #
    def _execute(self, make_request: Callable[[], Any], what: str) -> dict[str, Any]:
        try:
            from googleapiclient.errors import HttpError
        except ImportError:  # pragma: no cover - fake services in a minimal install
            HttpError = ()  # type: ignore[assignment,misc]
        try:
            from google.auth.exceptions import RefreshError, TransportError
        except ImportError:  # pragma: no cover
            RefreshError = TransportError = ()  # type: ignore[assignment,misc]

        attempt = 0
        while True:
            try:
                result = make_request().execute()
                return result if isinstance(result, dict) else {}
            except RefreshError as exc:  # access token expired mid-sync and refresh was rejected
                raise AuthorizationRequired(f"Gmail token refresh rejected during {what} ({type(exc).__name__})",
                                            human_action="Run job-agent gmail auth") from None
            except HttpError as exc:  # type: ignore[misc]
                status = _status_of(exc)
                if status == 401:
                    raise AuthorizationRequired(f"Gmail rejected the credentials during {what} (HTTP 401)",
                                                human_action="Run job-agent gmail auth") from None
                retryable = status in RETRY_STATUSES or (status == 403 and _is_rate_limit(exc))
                if status == 403 and not retryable:
                    raise AuthorizationRequired(
                        f"Gmail denied {what} (HTTP 403: insufficient permission for the granted scope)",
                        human_action="Check gmail.scopes, then run job-agent gmail auth") from None
                if status == 404:
                    raise PermanentError(f"Gmail {what}: not found (HTTP 404)") from None
                if not retryable:
                    raise PermanentError(f"Gmail {what} failed (HTTP {status})") from None
                error: Exception = RecoverableError(f"Gmail {what} failed after retries (HTTP {status})")
                retry_after = _retry_after(exc)
            except (TransportError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
                error = RecoverableError(f"Gmail {what} failed after retries (network: {type(exc).__name__})")
                retry_after = None
            except Exception as exc:  # httplib2 raises its own hierarchy (e.g. ServerNotFoundError)
                if type(exc).__module__.startswith("httplib2"):
                    error = RecoverableError(f"Gmail {what} failed after retries (network: {type(exc).__name__})")
                    retry_after = None
                else:
                    raise
            if attempt >= self._max_retries:
                raise error
            delay = retry_after if retry_after is not None else min(
                self._max_backoff, self._backoff * (2 ** attempt) * (1 + random.random() * 0.25))
            log.info("Gmail %s: transient failure, retry %d/%d in %.1fs", what, attempt + 1, self._max_retries,
                     delay)
            self._sleep(delay)
            attempt += 1


def _status_of(exc: Any) -> int:
    resp = getattr(exc, "resp", None)
    try:
        return int(getattr(resp, "status", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _is_rate_limit(exc: Any) -> bool:
    content = getattr(exc, "content", b"") or b""
    text = content.decode("utf-8", "replace").lower() if isinstance(content, bytes) else str(content).lower()
    return any(reason in text for reason in _RATE_LIMIT_REASONS)


def _retry_after(exc: Any) -> float | None:
    resp = getattr(exc, "resp", None)
    value = None
    if resp is not None and hasattr(resp, "get"):
        value = resp.get("retry-after")
    try:
        return min(60.0, max(0.0, float(value))) if value is not None else None
    except (TypeError, ValueError):
        return None
