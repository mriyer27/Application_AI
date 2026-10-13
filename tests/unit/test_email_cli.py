"""CLI: gmail auth/status/sync/revoke and emails list/show/associate/handled."""

from __future__ import annotations

import json

import pytest
import typer
import yaml
from typer.testing import CliRunner

from job_agent.email import auth
from job_agent.email import commands as email_commands
from job_agent.email import service as email_service
from job_agent.email.gmail_client import GmailReadClient
from test_email_fakes import fake_service, make_application

runner = CliRunner()


@pytest.fixture
def cli(home, monkeypatch):
    from job_agent import cli as cli_module

    monkeypatch.setattr(cli_module.state, "_ctx", None)
    monkeypatch.setattr(cli_module.state, "config_path", None)
    monkeypatch.setattr(cli_module.console, "width", 200)  # avoid wrapping table cells in assertions
    monkeypatch.setattr(cli_module.err_console, "width", 200)
    app = typer.Typer()
    email_commands.register(app)

    def invoke(*args):
        return runner.invoke(app, list(args), catch_exceptions=False)

    yield invoke
    if cli_module.state._ctx is not None:
        cli_module.state._ctx.close()


def _enable_gmail(home):
    path = home / "config" / "config.yaml"
    data = yaml.safe_load(path.read_text())
    data["gmail"]["enabled"] = True
    path.write_text(yaml.safe_dump(data))


def _patch_service(monkeypatch, svc):
    monkeypatch.setattr(email_service, "_make_client", lambda ctx, service, fmt: GmailReadClient(svc))


def test_gmail_status_disabled_is_not_connected(cli):
    result = cli("gmail", "status")
    assert result.exit_code == 0, result.output
    assert "NOT CONNECTED" in result.output and "disabled" in result.output


def test_gmail_sync_disabled_fails_with_next_step(cli, caplog):
    from job_agent import cli as cli_module

    result = cli("gmail", "sync")
    assert result.exit_code == 1
    assert "disabled" in result.output and "gmail-setup.md" in result.output
    # a plain message: no traceback, and no failed task run / error row for a setting you chose
    assert "Traceback" not in result.output and "Traceback" not in caplog.text
    db = cli_module.state._ctx.db
    assert db.scalar("SELECT COUNT(*) FROM agent_runs") == 0
    assert db.scalar("SELECT COUNT(*) FROM errors") == 0


def test_gmail_sync_then_emails_commands(cli, home, monkeypatch, ctx, make_job):
    _enable_gmail(home)
    job_id = make_job(employer="Acme Analytics", employer_normalized="acme analytics",
                      job_url="https://boards.greenhouse.io/acmeanalytics/jobs/7")
    app_id = make_application(ctx.db, job_id, "submitted", submitted_at="2026-10-01T00:00:00+00:00")
    app_uid = ctx.db.scalar("SELECT application_uid FROM applications WHERE id = ?", (app_id,))
    _patch_service(monkeypatch, fake_service())

    result = cli("gmail", "sync")
    assert result.exit_code == 0, result.output
    assert "Gmail sync" in result.output and "By category" in result.output

    status = cli("gmail", "status")
    # the sync used an injected fake service; with no OAuth files present it must not claim a connection
    assert status.exit_code == 0 and "NOT CONNECTED" in status.output and "never" not in status.output

    listing = cli("emails")
    assert listing.exit_code == 0 and "rejection" in listing.output and "recruiter_mes" in listing.output
    attention = cli("emails", "--attention")
    assert attention.exit_code == 0 and "recruiter_mes" in attention.output
    assert "application_conf" not in attention.output  # applied automatically with evidence, nothing to do

    recruiter_id = ctx.db.scalar("SELECT id FROM email_messages WHERE gmail_message_id = 'm-recruiter-1'")
    shown = cli("emails", "show", str(recruiter_id))
    assert shown.exit_code == 0
    assert "Suggested reply" in shown.output and "never sent" in shown.output
    assert "[your availability]" in shown.output  # escaped placeholder printed literally

    confirm_id = ctx.db.scalar("SELECT id FROM email_messages WHERE gmail_message_id = 'm-confirm-1'")
    linked = cli("emails", "associate", str(confirm_id), app_uid, "--apply-status")
    assert linked.exit_code == 0, linked.output
    assert "linked" in linked.output
    assert "confirmation_received" in linked.output or "Status not changed" in linked.output

    handled = cli("emails", "handled", str(recruiter_id))
    assert handled.exit_code == 0 and "Marked handled" in handled.output
    missing = cli("emails", "show", "99999")
    assert missing.exit_code == 1


def test_gmail_sync_auth_error_shows_next_step(cli, home, monkeypatch):
    from job_agent.errors import AuthorizationRequired

    _enable_gmail(home)

    def broken(ctx, service, fmt):
        raise AuthorizationRequired("Gmail authorization required: token refresh rejected (invalid_grant)",
                                    human_action="Run job-agent gmail auth")

    monkeypatch.setattr(email_service, "_make_client", broken)
    result = cli("gmail", "sync")
    assert result.exit_code == 1
    assert "authorization_required" in result.output and "gmail auth" in result.output


def test_gmail_auth_and_revoke(cli, home, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from google.oauth2.credentials import Credentials

    secrets = home / "data" / "secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    (secrets / "gmail_credentials.json").write_text(json.dumps({"installed": {"client_id": "x"}}))
    creds = Credentials(token="ya29.cli-test-token-0123456789", refresh_token="1//cli-refresh-0123456789abcdefgh",
                        token_uri="https://oauth2.googleapis.com/token", client_id="c", client_secret="s",
                        scopes=[auth.SCOPE_READONLY], granted_scopes=[auth.SCOPE_READONLY],
                        expiry=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1))

    class Flow:
        def run_local_server(self, **kwargs):
            assert kwargs["host"] == "localhost" and kwargs["port"] == 0
            return creds

    monkeypatch.setattr(auth, "_default_flow_factory", lambda secrets_file, scopes: Flow())
    result = cli("gmail", "auth", "--no-browser")
    assert result.exit_code == 0, result.output
    assert "Authorized" in result.output and "7 days" in result.output
    assert (secrets / "gmail_token.json").is_file()

    revoked = cli("gmail", "revoke")
    assert revoked.exit_code == 0 and "Local token deleted" in revoked.output
    assert "myaccount.google.com/permissions" in revoked.output
    assert not (secrets / "gmail_token.json").exists()


def test_emails_show_explains_why_status_was_not_changed(cli, home, monkeypatch, ctx, make_job):
    from test_email_fakes import build_message, spec

    _enable_gmail(home)
    job_id = make_job(employer="Acme Analytics", employer_normalized="acme analytics",
                      job_url="https://careers.acmeanalytics.com/jobs/7")
    make_application(ctx.db, job_id, "submitted", submitted_at="2026-10-01T00:00:00+00:00")
    forged = spec("s-injection-1")
    forged.update(id="s-forged-1", threadId="t-forged-1", subject="Offer letter - Business Analyst",
                  text="We are pleased to extend you an offer for the Business Analyst position.",
                  headers={"Authentication-Results": "mx.google.com; spf=fail smtp.mailfrom=evil.example; "
                                                     "dkim=none; dmarc=fail"})
    _patch_service(monkeypatch, fake_service().__class__([build_message(forged)]))
    assert cli("gmail", "sync").exit_code == 0
    email_id = ctx.db.scalar("SELECT id FROM email_messages WHERE gmail_message_id = 's-forged-1'")
    shown = cli("emails", "show", str(email_id))
    assert shown.exit_code == 0
    assert "Status not changed automatically" in shown.output
    assert "sender not verified by Gmail (dmarc=fail)" in shown.output
    assert "--apply-status" in shown.output
