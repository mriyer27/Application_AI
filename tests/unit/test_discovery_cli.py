"""CLI: ``discover``, ``sources``, ``sources check`` (registered on a fresh Typer app)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
import typer
from typer.testing import CliRunner

from job_agent.discovery.commands import register

runner = CliRunner()


@pytest.fixture
def cli_app(home, monkeypatch):
    from job_agent import cli

    monkeypatch.setattr(cli.state, "_ctx", None)
    monkeypatch.setattr(cli.state, "config_path", None)
    # Wide consoles so table cells are not folded mid-word in assertions.
    monkeypatch.setattr(cli.console, "_width", 220)
    monkeypatch.setattr(cli.err_console, "_width", 220)
    app = typer.Typer()
    register(app)
    yield app
    if cli.state._ctx is not None:
        cli.state._ctx.close()


@pytest.fixture
def mock_file(home):
    path = home / "data" / "mock_jobs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"employer": "Mock Co", "jobs": [
        {"id": "m-1", "title": "Data Analyst", "location": "New York, NY", "description": "Mock."},
        {"id": "m-2", "title": "Senior Data Analyst", "location": "New York, NY", "description": "Mock."},
        {"id": "m-3", "title": "Warehouse Associate", "location": "New York, NY", "description": "Mock."},
    ]}))
    return "data/mock_jobs.json"


def test_discover_with_mock_fixture_prints_summary(cli_app, mock_file):
    result = runner.invoke(cli_app, ["discover", "--source", f"fixture:{mock_file}", "--limit", "5"])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "fixture:data/mock_jobs.json" in out and "MOCK" in out and "Total" in out
    rerun = runner.invoke(cli_app, ["discover", "--source", f"fixture:{mock_file}"])
    assert rerun.exit_code == 0, rerun.output


def test_discover_all_sources_failed_exits_nonzero(cli_app):
    result = runner.invoke(cli_app, ["discover", "--source", "fixture:data/missing.json"])
    assert result.exit_code == 1
    assert "FAILED" in result.output


def test_discover_bad_source_is_a_readable_error(cli_app):
    result = runner.invoke(cli_app, ["discover", "--source", "linkedin"])
    assert result.exit_code == 1
    assert "unknown job source" in result.output


def test_sources_lists_configured_sources(cli_app, mock_file):
    runner.invoke(cli_app, ["discover", "--source", f"fixture:{mock_file}"])
    result = runner.invoke(cli_app, ["sources"])
    assert result.exit_code == 0, result.output
    assert "capco" in result.output and "palantir" in result.output and "ramp" in result.output
    assert "MOCK" in result.output  # the fixture source that ran is listed as mock


@respx.mock
def test_sources_check_reports_reachability(cli_app):
    respx.get("https://boards-api.greenhouse.io/v1/boards/capco").mock(
        return_value=httpx.Response(200, json={"name": "Capco", "content": ""}))
    respx.get("https://api.lever.co/v0/postings/nosuchco").mock(return_value=httpx.Response(404))
    result = runner.invoke(cli_app, ["sources", "check", "--source", "greenhouse:capco",
                                     "--source", "lever:nosuchco"])
    assert result.exit_code == 1  # one source unreachable
    assert "OK" in result.output and "FAIL" in result.output and "board not found" in result.output
