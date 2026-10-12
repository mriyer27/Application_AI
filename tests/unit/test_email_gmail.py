"""Gmail read-only client, OAuth token handling and authorization failure behaviour."""

from __future__ import annotations

import inspect
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from job_agent.database import repo
from job_agent.email import auth
from job_agent.email.gmail_client import GmailReadClient
from job_agent.email.service import sync_gmail
from job_agent.errors import (
    AuthorizationRequired,
    ConfigError,
    HumanActionRequired,
    PermanentError,
    PermissionDenied,
    RecoverableError,
)
from test_email_fakes import enable_gmail, fake_service, http_error

READONLY = "https://www.googleapis.com/auth/gmail.readonly"
FORBIDDEN_WORDS = ("send", "draft", "modify", "trash", "delete", "insert", "import", "batch", "label", "watch",
                   "untrash", "forward", "reply", "write", "update")


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # google-auth uses naive UTC expiry


# ------------------------------------------------------- no write paths ---- #
def test_client_exposes_only_read_operations():
    public = {name for name, _ in inspect.getmembers(GmailReadClient) if not name.startswith("_")}
    assert public == {"list_message_ids", "get_message", "from_credentials", "message_format"}
    for name in dir(GmailReadClient):
        assert not any(w in name.lower() for w in FORBIDDEN_WORDS), name


def test_scopes_are_read_only_everywhere(config):
    assert auth.READ_ONLY_SCOPES == {READONLY, "https://www.googleapis.com/auth/gmail.metadata"}
    assert config.gmail.scopes == [READONLY]
    assert config.permissions.send_emails is False
    for bad in ("https://mail.google.com/", "https://www.googleapis.com/auth/gmail.send",
                "https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/gmail.compose"):
        with pytest.raises(AuthorizationRequired):
            auth.check_read_only([READONLY, bad])


def test_config_rejects_send_scope(home):
    import yaml

    from job_agent.config import load_config

    data = yaml.safe_load((home / "config/config.yaml").read_text())
    data["gmail"]["scopes"] = ["https://www.googleapis.com/auth/gmail.send"]
    (home / "config/config.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError):
        load_config(home=home)


# ------------------------------------------------------------- client ---- #
def test_list_message_ids_paginates():
    svc = fake_service(page_size=2)
    client = GmailReadClient(svc, sleep=lambda s: None)
    refs = client.list_message_ids("newer_than:14d", 100)
    assert len(refs) == len(svc.store) == 9
    assert len(svc.list_calls) == 5  # 2+2+2+2+1
    assert [c.get("pageToken") for c in svc.list_calls] == [None, "2", "4", "6", "8"]
    assert all(c["userId"] == "me" and c["q"] == "newer_than:14d" for c in svc.list_calls)
    assert refs[0].id == "m-newsletter-1"  # newest first


def test_list_message_ids_respects_max_results():
    svc = fake_service(page_size=2)
    refs = GmailReadClient(svc, sleep=lambda s: None).list_message_ids(None, 3)
    assert len(refs) == 3 and len(svc.list_calls) == 2
    assert "q" not in svc.list_calls[0]


def test_get_message_uses_full_format():
    svc = fake_service("m-confirm-1")
    msg = GmailReadClient(svc).get_message("m-confirm-1")
    assert msg["id"] == "m-confirm-1"
    assert svc.get_calls == [{"userId": "me", "id": "m-confirm-1", "format": "full"}]


def test_429_then_success_is_retried_with_backoff():
    sleeps: list[float] = []
    svc = fake_service("m-confirm-1", list_failures=[http_error(429, "rateLimitExceeded")])
    refs = GmailReadClient(svc, sleep=sleeps.append).list_message_ids(None, 10)
    assert [r.id for r in refs] == ["m-confirm-1"]
    assert len(sleeps) == 1 and len(svc.list_calls) == 2


def test_server_errors_exhaust_retries_as_recoverable():
    sleeps: list[float] = []
    svc = fake_service("m-confirm-1", list_failures=[http_error(503)] * 10)
    with pytest.raises(RecoverableError):
        GmailReadClient(svc, max_retries=3, sleep=sleeps.append).list_message_ids(None, 10)
    assert len(svc.list_calls) == 4 and len(sleeps) == 3


def test_401_is_authorization_required_and_not_retried():
    svc = fake_service("m-confirm-1", list_failures=[http_error(401)])
    with pytest.raises(AuthorizationRequired):
        GmailReadClient(svc, sleep=lambda s: None).list_message_ids(None, 10)
    assert len(svc.list_calls) == 1


def test_403_rate_limit_retried_but_insufficient_scope_is_auth_error():
    svc = fake_service("m-confirm-1", list_failures=[http_error(403, "userRateLimitExceeded")])
    assert GmailReadClient(svc, sleep=lambda s: None).list_message_ids(None, 10)
    svc = fake_service("m-confirm-1", list_failures=[http_error(403, "insufficientPermissions")])
    with pytest.raises(AuthorizationRequired):
        GmailReadClient(svc, sleep=lambda s: None).list_message_ids(None, 10)


def test_404_on_get_is_permanent():
    svc = fake_service("m-confirm-1")
    with pytest.raises(PermanentError):
        GmailReadClient(svc).get_message("does-not-exist")


def test_network_error_is_retried():
    svc = fake_service("m-confirm-1", list_failures=[ConnectionResetError("reset")])
    assert len(GmailReadClient(svc, sleep=lambda s: None).list_message_ids(None, 10)) == 1


# --------------------------------------------------------------- OAuth ---- #
def _creds(*, scopes=None, granted=None, expired=False):
    from google.oauth2.credentials import Credentials

    expiry = _utcnow_naive() + (timedelta(hours=-2) if expired else timedelta(hours=1))
    return Credentials(token="ya29.test-access-token-0123456789", refresh_token="1//test-refresh-token-0123456789abcd",
                       token_uri="https://oauth2.googleapis.com/token", client_id="test-client.apps.example",
                       client_secret="GOCSPX-test-secret-0123", scopes=scopes or [READONLY],
                       granted_scopes=granted or scopes or [READONLY], expiry=expiry)


class FakeFlow:
    def __init__(self, creds=None, error: Exception | None = None):
        self.creds = creds
        self.error = error
        self.calls: list[dict] = []

    def run_local_server(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.creds


def _client_file(ctx):
    path = auth.credentials_file(ctx.config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"installed": {"client_id": "x", "client_secret": "y",
                                              "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                                              "token_uri": "https://oauth2.googleapis.com/token",
                                              "redirect_uris": ["http://localhost"]}}))
    return path


def test_authorize_saves_token_with_0600_and_loopback_flow(ctx):
    _client_file(ctx)
    flow = FakeFlow(_creds())
    seen = {}

    def factory(secrets, scopes):
        seen.update(secrets=secrets, scopes=scopes)
        return flow

    result = auth.authorize(ctx, flow_factory=factory, open_browser=False)
    assert seen["scopes"] == [READONLY]
    assert flow.calls == [{"host": "localhost", "port": 0, "open_browser": False}]
    token = auth.token_file(ctx.config)
    assert result.token_path == token and token.is_file()
    if os.name == "posix":
        assert (token.stat().st_mode & 0o777) == 0o600
    data = json.loads(token.read_text())
    assert data["granted_scopes"] == [READONLY] and data["refresh_token"]
    assert repo.get_state(ctx.db, auth.STATE_AUTHORIZED_AT)
    assert ctx.db.scalar("SELECT COUNT(*) FROM audit_events WHERE action = 'gmail_authorized'") == 1
    # the audit trail never contains the token itself
    assert "ya29" not in ctx.db.scalar("SELECT details FROM audit_events WHERE action = 'gmail_authorized'")


def test_authorize_refuses_broader_granted_scopes(ctx):
    _client_file(ctx)
    flow = FakeFlow(_creds(granted=[READONLY, "https://www.googleapis.com/auth/gmail.send"]))
    with pytest.raises(AuthorizationRequired):
        auth.authorize(ctx, flow_factory=lambda s, sc: flow)
    assert not auth.token_file(ctx.config).exists()


def test_authorize_without_client_file_points_to_setup(ctx):
    with pytest.raises(HumanActionRequired, match="OAuth client file"):
        auth.authorize(ctx, flow_factory=lambda s, sc: FakeFlow(_creds()))


def test_authorize_denied_consent_is_authorization_required(ctx):
    _client_file(ctx)
    flow = FakeFlow(error=RuntimeError("access_denied"))
    with pytest.raises(AuthorizationRequired):
        auth.authorize(ctx, flow_factory=lambda s, sc: flow)


def _write_token(ctx, creds, granted=None):
    path = auth.token_file(ctx.config)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(creds.to_json())
    data["granted_scopes"] = granted or [READONLY]
    path.write_text(json.dumps(data))
    return path


def test_load_credentials_refreshes_expired_token(ctx, monkeypatch):
    from google.oauth2.credentials import Credentials

    _write_token(ctx, _creds(expired=True))
    calls = []

    def fake_refresh(self, request):
        calls.append(request)
        self.token = "ya29.refreshed-access-token-0123456789"
        self.expiry = _utcnow_naive() + timedelta(hours=1)

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)
    creds = auth.load_credentials(ctx, request=object())
    assert creds.valid and len(calls) == 1
    saved = json.loads(auth.token_file(ctx.config).read_text())
    assert saved["token"] == "ya29.refreshed-access-token-0123456789" and saved["granted_scopes"] == [READONLY]


def test_load_credentials_refuses_non_read_only_token(ctx):
    _write_token(ctx, _creds(scopes=["https://mail.google.com/"]), granted=["https://mail.google.com/"])
    with pytest.raises(AuthorizationRequired):
        auth.load_credentials(ctx, request=object())
    assert "non-read-only" in repo.get_state(ctx.db, auth.STATE_AUTH_ERROR)


def test_missing_token_requires_authorization(ctx):
    with pytest.raises(AuthorizationRequired) as info:
        auth.load_credentials(ctx)
    assert info.value.human_action == "Run job-agent gmail auth"


def test_revoked_token_sets_auth_error_and_fails_fast(ctx, monkeypatch):
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials

    enable_gmail(ctx)
    _write_token(ctx, _creds(expired=True))
    refresh_calls = []

    def revoked(self, request):
        refresh_calls.append(1)
        raise RefreshError("invalid_grant: Token has been expired or revoked.", {"error": "invalid_grant"})

    monkeypatch.setattr(Credentials, "refresh", revoked)
    monkeypatch.setattr(auth, "_transport_request", lambda: object())

    with pytest.raises(AuthorizationRequired) as info:
        sync_gmail(ctx)
    assert info.value.human_action == "Run job-agent gmail auth"
    assert "invalid_grant" in repo.get_state(ctx.db, auth.STATE_AUTH_ERROR)
    assert len(refresh_calls) == 1

    # Next run: fails fast without contacting Google (no refresh attempt).
    with pytest.raises(AuthorizationRequired, match="not contacting Google"):
        sync_gmail(ctx)
    assert len(refresh_calls) == 1
    status = auth.gmail_status(ctx)
    assert status.connected is False and status.auth_error

    # Re-authorizing (token file changes) lets the next sync run; success clears the error.
    _write_token(ctx, _creds())
    assert auth.gmail_status(ctx).reauthorized_since_error is True
    svc = fake_service("m-confirm-1")
    monkeypatch.setattr(GmailReadClient, "from_credentials", classmethod(lambda cls, creds, **kw: cls(svc, **kw)))
    result = sync_gmail(ctx)
    assert result.new == 1
    assert repo.get_state(ctx.db, auth.STATE_AUTH_ERROR) is None
    assert repo.get_state(ctx.db, auth.STATE_LAST_SUCCESS)
    assert auth.gmail_status(ctx).connected is True


def test_401_during_sync_records_auth_error(ctx):
    enable_gmail(ctx)
    svc = fake_service("m-confirm-1", list_failures=[http_error(401)])
    with pytest.raises(AuthorizationRequired):
        sync_gmail(ctx, service=svc)
    assert repo.get_state(ctx.db, auth.STATE_AUTH_ERROR)
    with pytest.raises(AuthorizationRequired):
        sync_gmail(ctx, service=svc)
    assert len(svc.list_calls) == 1  # fail-fast: no second request


def test_revoke_local_deletes_token(ctx):
    path = _write_token(ctx, _creds())
    assert auth.revoke_local(ctx) is True and not path.exists()
    assert auth.revoke_local(ctx) is False
    assert auth.gmail_status(ctx).token_present is False


def test_status_not_connected_without_successful_sync(ctx):
    enable_gmail(ctx)
    _client_file(ctx)
    _write_token(ctx, _creds())
    st = auth.gmail_status(ctx)
    assert st.token_present and not st.connected and "no successful sync" in st.detail


def test_sync_requires_enabled_and_permission(ctx):
    with pytest.raises(HumanActionRequired, match="disabled"):
        sync_gmail(ctx, service=fake_service("m-confirm-1"))
    enable_gmail(ctx)
    ctx.config.permissions.read_emails = False
    with pytest.raises(PermissionDenied):
        sync_gmail(ctx, service=fake_service("m-confirm-1"))


def test_token_path_outside_home_is_refused(ctx):
    ctx.config.gmail.token_path = "/tmp/../etc/gmail_token.json"
    with pytest.raises(PermanentError):
        auth.token_file(ctx.config)
