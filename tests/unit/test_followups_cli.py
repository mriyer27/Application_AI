"""CLI: followups list/review/show/approve/done/dismiss."""

from __future__ import annotations

from datetime import timedelta

import pytest
import typer
from typer.testing import CliRunner

from job_agent.followups import commands as followup_commands
from job_agent.utils.time import to_iso, utcnow
from test_email_fakes import make_application

runner = CliRunner()


@pytest.fixture
def cli(home, monkeypatch):
    from job_agent import cli as cli_module

    monkeypatch.setattr(cli_module.state, "_ctx", None)
    monkeypatch.setattr(cli_module.state, "config_path", None)
    monkeypatch.setattr(cli_module.console, "width", 200)
    monkeypatch.setattr(cli_module.err_console, "width", 200)
    app = typer.Typer()
    followup_commands.register(app)

    def invoke(*args):
        return runner.invoke(app, list(args), catch_exceptions=False)

    yield invoke
    if cli_module.state._ctx is not None:
        cli_module.state._ctx.close()


def test_followups_cli_flow(cli, ctx, make_job):
    db = ctx.db
    ten_days = to_iso(utcnow() - timedelta(days=10))
    make_application(db, make_job(), "submitted", submitted_at=ten_days, status_updated_at=ten_days,
                     last_activity_at=ten_days)

    empty = cli("followups")
    assert empty.exit_code == 0 and "No follow-ups" in empty.output

    reviewed = cli("followups", "review")
    assert reviewed.exit_code == 0, reviewed.output
    assert "Created 1" in reviewed.output and "application_no_response=1" in reviewed.output
    again = cli("followups", "review")
    assert "Created 0" in again.output and "already existing 1" in again.output

    fid = db.scalar("SELECT id FROM followups")
    listing = cli("followups")
    assert listing.exit_code == 0 and "application_no_response" in listing.output and "drafted" in listing.output
    assert "application_no_response" in cli("followups", "list", "--all").output

    shown = cli("followups", "show", str(fid))
    assert shown.exit_code == 0 and "Draft" in shown.output and "Acme Analytics" in shown.output

    approved = cli("followups", "approve", str(fid))
    assert approved.exit_code == 0, approved.output
    assert "approved for manual sending" in approved.output and "draft (copy)" in approved.output
    assert "Following up on my application" in approved.output
    assert db.scalar("SELECT status FROM followups WHERE id = ?", (fid,)) == "approved"

    done = cli("followups", "done", str(fid))
    assert done.exit_code == 0 and "completed" in done.output
    bad = cli("followups", "dismiss", str(fid))  # completed -> dismissed is not allowed
    assert bad.exit_code == 1 and "not allowed" in bad.output
    assert cli("followups", "show", "999").exit_code == 1
    assert cli("followups", "--status", "bogus").exit_code == 1


def test_followups_dismiss(cli, ctx, make_job):
    db = ctx.db
    make_application(db, make_job(), "assessment_received", status_updated_at=to_iso(utcnow()),
                     last_activity_at=to_iso(utcnow()))
    assert cli("followups", "review").exit_code == 0
    fid = db.scalar("SELECT id FROM followups WHERE followup_type = 'assessment_due'")
    result = cli("followups", "dismiss", str(fid))
    assert result.exit_code == 0 and "dismissed" in result.output
    assert db.scalar("SELECT status FROM followups WHERE id = ?", (fid,)) == "dismissed"
