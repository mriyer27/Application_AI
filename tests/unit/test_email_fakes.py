"""Shared fakes for the email / follow-up tests (no tests in this module).

``FakeGmailService`` mimics the subset of the Gmail API client used by
``GmailReadClient``: ``users().messages().list(...).execute()`` and
``users().messages().get(...).execute()``. It has no send/modify methods and
records every call so tests can assert exactly what was requested.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "emails"


def load_specs(name: str = "messages.json") -> list[dict[str, Any]]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["messages"]


def spec(message_id: str) -> dict[str, Any]:
    for name in ("messages.json", "scenarios.json"):
        for s in load_specs(name):
            if s["id"] == message_id:
                return dict(s)
    raise KeyError(message_id)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode().rstrip("=")


def build_message(s: dict[str, Any]) -> dict[str, Any]:
    """Turn a fixture spec into a Gmail API ``users.messages`` resource (format=full)."""
    received = datetime.fromisoformat(s["received"].replace("Z", "+00:00"))
    headers = [
        {"name": "From", "value": s["from"]},
        {"name": "To", "value": "Test Candidate <test.candidate@example.com>"},
        {"name": "Subject", "value": s["subject"]},
        {"name": "Date", "value": format_datetime(received)},
    ]
    for k, v in (s.get("headers") or {}).items():
        headers.append({"name": k, "value": v})
    parts = []
    if s.get("text") is not None:
        parts.append({"partId": "0", "mimeType": "text/plain", "filename": "",
                      "headers": [{"name": "Content-Type", "value": "text/plain; charset=\"UTF-8\""}],
                      "body": {"size": len(s["text"]), "data": _b64(s["text"])}})
    if s.get("html") is not None:
        parts.append({"partId": "1", "mimeType": "text/html", "filename": "",
                      "headers": [{"name": "Content-Type", "value": "text/html; charset=UTF-8"}],
                      "body": {"size": len(s["html"]), "data": _b64(s["html"])}})
    payload: dict[str, Any] = {"partId": "", "mimeType": "multipart/alternative", "headers": headers,
                               "body": {"size": 0}, "parts": parts}
    if s.get("attachment"):
        payload = {"partId": "", "mimeType": "multipart/mixed", "headers": headers, "body": {"size": 0},
                   "parts": [dict(payload, headers=[]),
                             {"partId": "2", "mimeType": "application/pdf", "filename": "offer.pdf",
                              "headers": [], "body": {"attachmentId": "att-1", "size": 1234}}]}
    return {
        "id": s["id"],
        "threadId": s.get("threadId", s["id"]),
        "labelIds": s.get("labels", ["INBOX"]),
        "snippet": s.get("snippet", ""),
        "internalDate": str(int(received.timestamp() * 1000)),
        "sizeEstimate": 2048,
        "payload": payload,
    }


class _Request:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class _Messages:
    def __init__(self, service: "FakeGmailService") -> None:
        self._svc = service

    def list(self, **kwargs):
        self._svc.list_calls.append(kwargs)
        return _Request(lambda: self._svc._list(kwargs))

    def get(self, **kwargs):
        self._svc.get_calls.append(kwargs)
        return _Request(lambda: self._svc._get(kwargs))


class _Users:
    def __init__(self, service: "FakeGmailService") -> None:
        self._messages = _Messages(service)

    def messages(self):
        return self._messages


class FakeGmailService:
    """Holds Gmail message resources (newest first) and a queue of injected failures."""

    def __init__(self, messages: list[dict[str, Any]], *, page_size: int = 2,
                 list_failures: list[BaseException] | None = None,
                 get_failures: dict[str, list[BaseException]] | None = None) -> None:
        self.store = sorted(messages, key=lambda m: int(m["internalDate"]), reverse=True)
        self.page_size = page_size
        self.list_failures = list(list_failures or [])
        self.get_failures = {k: list(v) for k, v in (get_failures or {}).items()}
        self.list_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self._users = _Users(self)

    def users(self):
        return self._users

    def add(self, message: dict[str, Any]) -> None:
        self.store = sorted([*self.store, message], key=lambda m: int(m["internalDate"]), reverse=True)

    def _list(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        if self.list_failures:
            raise self.list_failures.pop(0)
        start = int(kwargs.get("pageToken") or 0)
        size = min(self.page_size, int(kwargs.get("maxResults") or self.page_size))
        chunk = self.store[start:start + size]
        out: dict[str, Any] = {"messages": [{"id": m["id"], "threadId": m["threadId"]} for m in chunk],
                               "resultSizeEstimate": len(self.store)}
        if start + size < len(self.store):
            out["nextPageToken"] = str(start + size)
        return out

    def _get(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        mid = kwargs["id"]
        failures = self.get_failures.get(mid)
        if failures:
            raise failures.pop(0)
        for m in self.store:
            if m["id"] == mid:
                return json.loads(json.dumps(m))
        raise http_error(404)


def http_error(status: int, reason: str = "") -> Exception:
    import httplib2
    from googleapiclient.errors import HttpError

    body = {"error": {"code": status, "message": reason or "error", "errors": [{"reason": reason or "error"}]}}
    return HttpError(httplib2.Response({"status": status}), json.dumps(body).encode())


def fake_service(*ids: str, page_size: int = 2, **kwargs: Any) -> FakeGmailService:
    specs = [spec(i) for i in ids] if ids else load_specs()
    return FakeGmailService([build_message(s) for s in specs], page_size=page_size, **kwargs)


def enable_gmail(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.config.gmail.enabled = True


def make_application(db, job_id: int, status: str, *, submitted_at: str | None = None,  # type: ignore[no-untyped-def]
                     status_updated_at: str | None = None, last_activity_at: str | None = None) -> int:
    """Create an application via the repo, then place it in ``status`` (test setup only)."""
    from job_agent.database import repo

    app_id = repo.create_application(db, job_id, actor="system")
    values: dict[str, Any] = {"status": status}
    if submitted_at is not None:
        values["submitted_at"] = submitted_at
    if status_updated_at is not None:
        values["status_updated_at"] = status_updated_at
    if last_activity_at is not None:
        values["last_activity_at"] = last_activity_at
    db.update("applications", app_id, values)
    return app_id
