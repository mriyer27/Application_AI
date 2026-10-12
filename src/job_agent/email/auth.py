"""Gmail OAuth 2.0 (installed-app flow) with read-only scopes.

* ``authorize`` runs Google's browser consent flow on a loopback port and saves
  the resulting token JSON (0600) to ``gmail.token_path``. No password is ever
  requested or stored by this program.
* ``load_credentials`` loads that token, refuses any non-read-only scope,
  refreshes an expired access token and turns a revoked / expired refresh token
  (incl. the 7-day expiry of apps in "Testing" mode) into
  ``AuthorizationRequired`` while recording ``agent_state.gmail_auth_error`` so
  the scheduler fails fast instead of hammering Google.
* ``revoke_local`` deletes the local token. Revoking the grant itself is done
  by the user at https://myaccount.google.com/permissions.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from job_agent.database import repo
from job_agent.errors import AuthorizationRequired, HumanActionRequired, RecoverableError
from job_agent.logging_config import get_logger
from job_agent.security.redaction import redact_text
from job_agent.security.sanitize import ensure_within
from job_agent.utils.time import parse_iso, utcnow_iso

if TYPE_CHECKING:  # pragma: no cover
    from job_agent.config import AppConfig
    from job_agent.context import AppContext
    from job_agent.database import Database

log = get_logger(__name__)

SCOPE_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_METADATA = "https://www.googleapis.com/auth/gmail.metadata"
#: The only scopes this program will ever request or accept in a token.
READ_ONLY_SCOPES: frozenset[str] = frozenset({SCOPE_READONLY, SCOPE_METADATA})

REVOKE_URL = "https://myaccount.google.com/permissions"
AUTH_ACTION = "Run job-agent gmail auth"
SETUP_DOC = "docs/gmail-setup.md"

STATE_AUTH_ERROR = "gmail_auth_error"
STATE_AUTH_ERROR_AT = "gmail_auth_error_at"
STATE_AUTH_ERROR_TOKEN = "gmail_auth_error_token_fingerprint"
STATE_LAST_SUCCESS = "gmail_last_success_at"
STATE_AUTHORIZED_AT = "gmail_authorized_at"

_MISSING_LIBS_ACTION = 'Install the Gmail extras: .venv/bin/pip install -e ".[gmail]"'


# --------------------------------------------------------------------------- #
# Paths and scope checks
# --------------------------------------------------------------------------- #
def credentials_file(config: "AppConfig") -> Path:
    """The OAuth *client* file downloaded from Google Cloud (read only by us)."""
    return config.resolve(config.gmail.credentials_path)


def token_file(config: "AppConfig") -> Path:
    """The user token file we write. It must stay inside the project home."""
    return ensure_within(config.home, config.resolve(config.gmail.token_path))


def check_read_only(scopes: Iterable[str] | None, *, what: str = "scopes") -> list[str]:
    """Return the scopes if every one is read-only; raise otherwise."""
    values = sorted({s for s in (scopes or []) if s})
    bad = [s for s in values if s not in READ_ONLY_SCOPES]
    if bad:
        raise AuthorizationRequired(
            f"Gmail {what} include non-read-only scopes {bad}; refusing to use them",
            human_action=f"Run job-agent gmail revoke, remove the app at {REVOKE_URL}, then {AUTH_ACTION}",
        )
    return values


def requested_scopes(config: "AppConfig") -> list[str]:
    scopes = check_read_only(config.gmail.scopes, what="configured scopes")
    if not scopes:
        raise HumanActionRequired("gmail.scopes is empty", human_action=f"Set gmail.scopes to [{SCOPE_READONLY}]")
    return scopes


def token_fingerprint(path: Path) -> str | None:
    """Content hash of the token file (``None`` if missing) - detects re-authorization."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except FileNotFoundError:
        return None
    except OSError:
        return "unreadable"


# --------------------------------------------------------------------------- #
# Auth-error state
# --------------------------------------------------------------------------- #
def record_auth_error(db: "Database", config: "AppConfig", reason: str) -> None:
    """Remember an authorization failure (short, redacted) and the token it happened with."""
    short = redact_text(" ".join(str(reason).split()), pii=True)[:200] or "authorization failed"
    with db.transaction():
        repo.set_state(db, STATE_AUTH_ERROR, short)
        repo.set_state(db, STATE_AUTH_ERROR_AT, utcnow_iso())
        repo.set_state(db, STATE_AUTH_ERROR_TOKEN, token_fingerprint(token_file(config)) or "missing")
        repo.audit(db, actor="system", action="gmail_auth_error", outcome="authorization_required",
                   details={"reason": short})
    log.warning("Gmail authorization problem: %s", short)


def clear_auth_error(db: "Database") -> None:
    if repo.get_state(db, STATE_AUTH_ERROR) is None and repo.get_state(db, STATE_AUTH_ERROR_AT) is None:
        return
    with db.transaction():
        repo.set_state(db, STATE_AUTH_ERROR, None)
        repo.set_state(db, STATE_AUTH_ERROR_AT, None)
        repo.set_state(db, STATE_AUTH_ERROR_TOKEN, None)


def fail_fast_if_auth_broken(db: "Database", config: "AppConfig") -> None:
    """Raise without any network call if the last auth error still applies.

    The error applies until the token file changes (i.e. ``gmail auth`` was run).
    """
    reason = repo.get_state(db, STATE_AUTH_ERROR)
    if not reason:
        return
    recorded = repo.get_state(db, STATE_AUTH_ERROR_TOKEN) or "missing"
    current = token_fingerprint(token_file(config)) or "missing"
    if current == recorded:
        raise AuthorizationRequired(
            f"Gmail authorization problem ({reason}); not contacting Google until you re-authorize",
            human_action=AUTH_ACTION,
        )


# --------------------------------------------------------------------------- #
# Token file I/O
# --------------------------------------------------------------------------- #
def _write_token(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:  # pragma: no cover - e.g. Windows
        pass
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover
        pass


def _token_data(creds: Any, granted: list[str]) -> dict[str, Any]:
    data = json.loads(creds.to_json())
    # Keep the scopes Google actually granted next to the token so later loads can
    # refuse a token that carries more than read-only access.
    data["granted_scopes"] = granted
    return data


def _granted_scopes(creds: Any, fallback: Iterable[str]) -> list[str]:
    granted = getattr(creds, "granted_scopes", None)
    if not granted:
        granted = getattr(creds, "scopes", None) or list(fallback)
    if isinstance(granted, str):
        granted = granted.split()
    return sorted(set(granted))


# --------------------------------------------------------------------------- #
# Authorization (interactive)
# --------------------------------------------------------------------------- #
@dataclass
class AuthResult:
    token_path: Path
    scopes: list[str] = field(default_factory=list)


FlowFactory = Callable[[str, list[str]], Any]


def _default_flow_factory(client_secrets: str, scopes: list[str]) -> Any:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise HumanActionRequired(f"Gmail libraries are not installed ({exc})", human_action=_MISSING_LIBS_ACTION)
    return InstalledAppFlow.from_client_secrets_file(client_secrets, scopes=scopes)


def authorize(ctx: "AppContext", *, open_browser: bool = True, flow_factory: FlowFactory | None = None) -> AuthResult:
    """Run the OAuth consent flow in the user's browser and store the token (0600).

    Uses a loopback redirect on a random free port. Google shows the consent
    screen; this program never sees your Google password.
    """
    config = ctx.config
    scopes = requested_scopes(config)
    secrets_path = credentials_file(config)
    if not secrets_path.is_file():
        raise HumanActionRequired(
            f"Gmail OAuth client file not found at {config.gmail.credentials_path}",
            human_action=f"Create a Desktop OAuth client and save its JSON there (see {SETUP_DOC})",
        )
    target = token_file(config)
    factory = flow_factory or _default_flow_factory
    try:
        flow = factory(str(secrets_path), scopes)
    except (ValueError, KeyError) as exc:
        raise HumanActionRequired(
            f"the OAuth client file is not a valid Desktop-app client ({type(exc).__name__})",
            human_action=f"Download the JSON of an OAuth client of type 'Desktop app' ({SETUP_DOC})",
        ) from None
    try:
        creds = flow.run_local_server(host="localhost", port=0, open_browser=open_browser)
    except Exception as exc:  # oauthlib raises a variety of errors (denied consent, state mismatch, ...)
        if isinstance(exc, (HumanActionRequired, AuthorizationRequired)):
            raise
        raise AuthorizationRequired(
            f"Gmail authorization did not complete: {redact_text(str(exc), pii=True)[:200]}",
            human_action=f"{AUTH_ACTION} again and approve the read-only access",
        ) from None
    granted = _granted_scopes(creds, scopes)
    check_read_only(granted, what="granted scopes")
    _write_token(target, _token_data(creds, granted))
    with ctx.db.transaction():
        repo.set_state(ctx.db, STATE_AUTHORIZED_AT, utcnow_iso())
        repo.audit(ctx.db, actor="user", action="gmail_authorized", outcome="success",
                   details={"scopes": granted})
    log.info("Gmail authorized (scopes: %s)", ", ".join(granted))
    return AuthResult(token_path=target, scopes=granted)


# --------------------------------------------------------------------------- #
# Loading / refreshing credentials (non-interactive)
# --------------------------------------------------------------------------- #
def _auth_failure(ctx: "AppContext", reason: str) -> AuthorizationRequired:
    record_auth_error(ctx.db, ctx.config, reason)
    return AuthorizationRequired(f"Gmail authorization required: {reason}", human_action=AUTH_ACTION)


def load_credentials(ctx: "AppContext", *, request: Any = None) -> Any:
    """Load the stored token, enforce read-only scopes and refresh if expired.

    ``request`` is a ``google.auth.transport.Request`` (injectable for tests).
    """
    config = ctx.config
    path = token_file(config)
    if not path.is_file():
        raise AuthorizationRequired("Gmail is not authorized yet (no token file)", human_action=AUTH_ACTION)
    try:
        from google.auth.exceptions import RefreshError, TransportError
        from google.oauth2.credentials import Credentials
    except ImportError as exc:
        raise HumanActionRequired(f"Gmail libraries are not installed ({exc})", human_action=_MISSING_LIBS_ACTION)
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(info, dict):
            raise ValueError("token file is not a JSON object")
    except (OSError, ValueError) as exc:
        raise _auth_failure(ctx, f"token file unreadable ({type(exc).__name__})") from None

    stored = info.get("scopes") or []
    if isinstance(stored, str):
        stored = stored.split()
    granted = info.get("granted_scopes") or stored
    try:
        check_read_only(stored, what="token scopes")
        check_read_only(granted, what="granted scopes")
    except AuthorizationRequired:
        record_auth_error(ctx.db, config, "token has non-read-only scopes")
        raise
    try:
        creds = Credentials.from_authorized_user_info(info)
    except ValueError as exc:
        raise _auth_failure(ctx, f"token file invalid ({exc})") from None

    if creds.valid:
        return creds
    if not creds.refresh_token:
        raise _auth_failure(ctx, "token expired and has no refresh token")
    if request is None:
        from google.auth.transport.requests import Request

        request = Request()
    try:
        creds.refresh(request)
    except RefreshError as exc:
        # invalid_grant: revoked, password change, or Testing-mode 7-day expiry.
        raise _auth_failure(ctx, f"token refresh rejected ({_short_reason(exc)})") from None
    except TransportError as exc:
        raise RecoverableError(f"could not reach Google to refresh the Gmail token ({type(exc).__name__})") from None
    new_granted = _granted_scopes(creds, granted)
    try:
        check_read_only(new_granted, what="granted scopes")
    except AuthorizationRequired:
        record_auth_error(ctx.db, config, "refreshed token has non-read-only scopes")
        raise
    _write_token(path, _token_data(creds, new_granted))
    return creds


def _short_reason(exc: BaseException) -> str:
    text = str(exc)
    for marker in ("invalid_grant", "invalid_client", "unauthorized_client", "access_denied"):
        if marker in text:
            return marker
    return redact_text(text, pii=True)[:120] or type(exc).__name__


# --------------------------------------------------------------------------- #
# Revoke / status
# --------------------------------------------------------------------------- #
def revoke_local(ctx: "AppContext") -> bool:
    """Delete the local token file. Returns whether a token existed.

    This does not revoke the grant at Google; do that at
    https://myaccount.google.com/permissions (remove the app).
    """
    path = token_file(ctx.config)
    existed = path.exists()
    if existed:
        path.unlink()
    with ctx.db.transaction():
        repo.set_state(ctx.db, STATE_AUTHORIZED_AT, None)
        repo.audit(ctx.db, actor="user", action="gmail_token_deleted", outcome="success" if existed else "skipped",
                   details={"token_existed": existed})
    return existed


@dataclass
class GmailStatus:
    enabled: bool
    libraries_installed: bool
    client_file_present: bool
    token_present: bool
    scopes: list[str]
    authorized_at: str | None
    last_success_at: str | None
    auth_error: str | None
    auth_error_at: str | None
    reauthorized_since_error: bool
    connected: bool
    detail: str
    next_step: str | None


def gmail_status(ctx: "AppContext") -> GmailStatus:
    """Honest connection state: "connected" only after a sync succeeded since the last auth error."""
    import importlib.util

    config = ctx.config
    db = ctx.db
    libs = all(importlib.util.find_spec(m) is not None for m in ("googleapiclient", "google_auth_oauthlib"))
    client_present = credentials_file(config).is_file()
    tpath = token_file(config)
    token_present = tpath.is_file()
    scopes: list[str] = []
    if token_present:
        try:
            info = json.loads(tpath.read_text(encoding="utf-8"))
            scopes = sorted(set(info.get("granted_scopes") or info.get("scopes") or []))
        except (OSError, ValueError):
            scopes = []
    last_ok = repo.get_state(db, STATE_LAST_SUCCESS)
    err = repo.get_state(db, STATE_AUTH_ERROR)
    err_at = repo.get_state(db, STATE_AUTH_ERROR_AT)
    authorized_at = repo.get_state(db, STATE_AUTHORIZED_AT)
    reauthorized = bool(err) and (token_fingerprint(tpath) or "missing") != (
        repo.get_state(db, STATE_AUTH_ERROR_TOKEN) or "missing")
    ok_dt, err_dt = parse_iso(last_ok), parse_iso(err_at)
    connected = bool(token_present and last_ok and not err and (err_dt is None or (ok_dt and ok_dt > err_dt)))
    if not config.gmail.enabled:
        detail, step = "Gmail integration disabled (gmail.enabled: false)", f"Follow {SETUP_DOC}"
    elif not libs:
        detail, step = "Gmail libraries not installed", _MISSING_LIBS_ACTION
    elif not client_present:
        detail, step = "OAuth client file missing; NOT connected", f"Follow {SETUP_DOC}, then {AUTH_ACTION}"
    elif not token_present:
        detail, step = "not authorized; NOT connected", AUTH_ACTION
    elif err and reauthorized:
        detail, step = f"re-authorized after an error ({err}); not verified yet", "Run job-agent gmail sync"
    elif err:
        detail, step = f"authorization problem: {err}; NOT connected", AUTH_ACTION
    elif connected:
        detail, step = f"connected (read-only); last successful sync {last_ok}", None
    else:
        detail, step = "token present but no successful sync yet; NOT verified", "Run job-agent gmail sync"
    return GmailStatus(enabled=config.gmail.enabled, libraries_installed=libs, client_file_present=client_present,
                       token_present=token_present, scopes=scopes, authorized_at=authorized_at,
                       last_success_at=last_ok, auth_error=err, auth_error_at=err_at,
                       reauthorized_since_error=reauthorized, connected=connected, detail=detail, next_step=step)


__all__ = [
    "READ_ONLY_SCOPES", "REVOKE_URL", "AuthResult", "GmailStatus", "authorize", "load_credentials", "revoke_local",
    "gmail_status", "record_auth_error", "clear_auth_error", "fail_fast_if_auth_broken", "check_read_only",
    "credentials_file", "token_file", "token_fingerprint",
]
