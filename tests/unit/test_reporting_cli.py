"""CLI: ``job-agent report`` and ``job-agent reports`` (registered on a fresh Typer app)."""

from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from job_agent.reporting.commands import register

runner = CliRunner()


@pytest.fixture
def cli_app(home, monkeypatch):
    from job_agent import cli

    monkeypatch.setattr(cli.state, "_ctx", None)
    monkeypatch.setattr(cli.state, "config_path", None)
    app = typer.Typer()
    register(app)
    yield app
    if cli.state._ctx is not None:
        cli.state._ctx.close()


def test_report_command_writes_files_and_prints_paths(cli_app, home):
    result = runner.invoke(cli_app, ["report", "--date", "2026-10-10"])
    assert result.exit_code == 0, result.output
    assert "Job_Search_Report_2026-10-10.xlsx" in result.output
    assert "Daily_Summary_2026-10-10.md" in result.output
    assert "Submitted today (evidence-based)" in result.output
    assert (home / "reports" / "Job_Search_Report_2026-10-10.xlsx").is_file()
    assert (home / "reports" / "Daily_Summary_2026-10-10.md").is_file()

    again = runner.invoke(cli_app, ["report", "--date", "2026-10-10"])
    assert again.exit_code == 0 and "generation 2" in again.output

    listing = runner.invoke(cli_app, ["reports"])
    assert listing.exit_code == 0, listing.output
    assert "2026-10-10" in listing.output and "missing" not in listing.output

    (home / "reports" / "Daily_Summary_2026-10-10.md").unlink()
    assert "(missing)" in runner.invoke(cli_app, ["reports"]).output


def test_report_command_default_date_and_errors(cli_app, home):
    assert runner.invoke(cli_app, ["reports"]).output.startswith("No reports generated yet")
    result = runner.invoke(cli_app, ["report"])
    assert result.exit_code == 0, result.output
    assert len(list((home / "reports").glob("Job_Search_Report_*.xlsx"))) == 1

    bad = runner.invoke(cli_app, ["report", "--date", "10/10/2026"])
    assert bad.exit_code == 2 and "YYYY-MM-DD" in bad.output
    future = runner.invoke(cli_app, ["report", "--date", "2999-01-01"])
    assert future.exit_code == 1 and "future" in future.output
