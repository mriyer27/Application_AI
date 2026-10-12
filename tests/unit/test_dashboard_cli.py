"""CLI tests for `job-agent dashboard` and `job-agent dashboard set-password` (uvicorn is never started)."""

from __future__ import annotations

import os

import pytest
import typer
import yaml
from typer.testing import CliRunner

from job_agent.dashboard.commands import register
from job_agent.security.passwords import hash_password, verify_password

PASSWORD = "a long enough password"
ENV_VARS = ("JOB_AGENT_DASHBOARD_USERNAME", "JOB_AGENT_DASHBOARD_PASSWORD_HASH", "JOB_AGENT_SESSION_SECRET")


@pytest.fixture
def cli_app(home, monkeypatch):
    from job_agent import cli

    monkeypatch.setattr(cli.state, "_ctx", None)
    monkeypatch.setattr(cli.state, "config_path", None)
    # Register restoration for variables set_env_var() writes into os.environ.
    for var in ENV_VARS:
        monkeypatch.setenv(var, "")
    app = typer.Typer()
    register(app)
    return app


@pytest.fixture
def uvicorn_calls(monkeypatch):
    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    return calls


def _with_credentials(monkeypatch):
    monkeypatch.setenv("JOB_AGENT_DASHBOARD_USERNAME", "owner")
    monkeypatch.setenv("JOB_AGENT_DASHBOARD_PASSWORD_HASH", hash_password(PASSWORD))
    monkeypatch.setenv("JOB_AGENT_SESSION_SECRET", "s" * 40)


def _set_dashboard_config(home, **values):
    path = home / "config" / "config.yaml"
    data = yaml.safe_load(path.read_text())
    data["dashboard"].update(values)
    path.write_text(yaml.safe_dump(data))


def test_dashboard_refuses_without_credentials(cli_app, uvicorn_calls):
    result = CliRunner().invoke(cli_app, ["dashboard"])
    assert result.exit_code == 2
    assert "dashboard login is not configured" in result.output
    assert "set-password" in result.output
    assert uvicorn_calls == []


def test_dashboard_serves_on_configured_loopback(cli_app, uvicorn_calls, monkeypatch):
    _with_credentials(monkeypatch)
    result = CliRunner().invoke(cli_app, ["dashboard"])
    assert result.exit_code == 0, result.output
    assert "http://127.0.0.1:8765/" in result.output
    assert "WARNING" not in result.output
    (_, kwargs), = uvicorn_calls
    assert kwargs["host"] == "127.0.0.1" and kwargs["port"] == 8765 and kwargs["proxy_headers"] is False


def test_dashboard_warns_loudly_for_non_loopback(cli_app, uvicorn_calls, monkeypatch, home):
    _with_credentials(monkeypatch)
    _set_dashboard_config(home, host="100.101.102.103", allow_non_localhost=True)
    result = CliRunner().invoke(cli_app, ["dashboard"])
    assert result.exit_code == 0, result.output
    assert "WARNING" in result.output and "100.101.102.103" in result.output
    assert uvicorn_calls and uvicorn_calls[0][1]["host"] == "100.101.102.103"


def test_dashboard_refuses_wildcard_bind(cli_app, uvicorn_calls, monkeypatch, home):
    _with_credentials(monkeypatch)
    _set_dashboard_config(home, host="0.0.0.0", allow_non_localhost=True)
    result = CliRunner().invoke(cli_app, ["dashboard"])
    assert result.exit_code == 2
    assert "Refusing" in result.output
    assert uvicorn_calls == []


def test_set_password_writes_env_without_echo(cli_app, home):
    result = CliRunner().invoke(cli_app, ["dashboard", "set-password"], input=f"owner\n{PASSWORD}\n{PASSWORD}\n")
    assert result.exit_code == 0, result.output
    assert PASSWORD not in result.output and "scrypt$" not in result.output
    env_text = (home / ".env").read_text()
    assert "JOB_AGENT_DASHBOARD_USERNAME='owner'" in env_text
    assert PASSWORD not in env_text
    stored_hash = os.environ["JOB_AGENT_DASHBOARD_PASSWORD_HASH"]
    assert f"JOB_AGENT_DASHBOARD_PASSWORD_HASH='{stored_hash}'" in env_text
    assert verify_password(PASSWORD, stored_hash)
    assert "JOB_AGENT_SESSION_SECRET='" in env_text
    if os.name == "posix":
        assert (home / ".env").stat().st_mode & 0o077 == 0

    # A second run replaces the login but keeps the existing session secret.
    secret_line = next(line for line in env_text.splitlines() if line.startswith("JOB_AGENT_SESSION_SECRET="))
    os.environ["JOB_AGENT_SESSION_SECRET"] = ""
    result = CliRunner().invoke(cli_app, ["dashboard", "set-password"],
                                input=f"owner2\n{PASSWORD}x\n{PASSWORD}x\n")
    assert result.exit_code == 0, result.output
    env_text = (home / ".env").read_text()
    assert secret_line in env_text and env_text.count("JOB_AGENT_SESSION_SECRET=") == 1
    assert "JOB_AGENT_DASHBOARD_USERNAME='owner2'" in env_text


def test_set_password_then_dashboard_starts(cli_app, home, uvicorn_calls):
    result = CliRunner().invoke(cli_app, ["dashboard", "set-password"], input=f"owner\n{PASSWORD}\n{PASSWORD}\n")
    assert result.exit_code == 0, result.output
    result = CliRunner().invoke(cli_app, ["dashboard"])
    assert result.exit_code == 0, result.output
    assert len(uvicorn_calls) == 1


@pytest.mark.parametrize("user_input", [
    "owner\nshort\nshort\n",                       # too short
    f"bad user\n{PASSWORD}\n{PASSWORD}\n",          # invalid username
    f"owner\n{PASSWORD}\ndifferent password!!\n",   # confirmation mismatch (then input runs out)
])
def test_set_password_rejects_bad_input(cli_app, home, user_input):
    result = CliRunner().invoke(cli_app, ["dashboard", "set-password"], input=user_input)
    assert result.exit_code != 0
    assert not (home / ".env").exists()
    assert PASSWORD not in result.output
