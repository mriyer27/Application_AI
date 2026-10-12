"""CLI commands: apply, approvals, applications (fresh Typer app + CliRunner, fake browser)."""

from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from job_agent.applications import commands, service
from job_agent.browser.forms import FormField
from job_agent.database import repo
from test_applications_support import add_sponsorship_answer, fake_for, setup_application, simple_fields

runner = CliRunner()


@pytest.fixture
def cli(home, monkeypatch):
    import job_agent.cli as cli_mod

    app = typer.Typer()
    commands.register(app)
    cli_mod.state._ctx = None
    cli_mod.state.config_path = None

    def invoke(*args, input=None):
        try:
            return runner.invoke(app, list(args), input=input, catch_exceptions=False)
        finally:
            if cli_mod.state._ctx is not None:
                cli_mod.state._ctx.close()
            cli_mod.state._ctx = None

    return invoke


@pytest.fixture
def fake(monkeypatch):
    browser = fake_for()
    monkeypatch.setattr(service, "default_factory", browser)
    return browser


def test_apply_prepares_and_prints_approval(cli, ctx, make_job, fake):
    info = setup_application(ctx, make_job)
    result = cli("apply", info["job_uid"])
    assert result.exit_code == 0, result.output
    uid = ctx.db.scalar("SELECT approval_uid FROM approval_requests WHERE status = 'pending'")
    assert uid in result.output and "awaiting_approval" in result.output
    assert "Nothing was submitted" in result.output and fake.submissions == 0
    assert fake.options.headless is True
    assert cli("apply", info["job_uid"], "--headed").exit_code == 0
    assert fake.options.headless is False


def test_apply_without_documents_explains(cli, ctx, make_job, fake):
    job_id = make_job()
    uid = ctx.db.scalar("SELECT job_uid FROM jobs WHERE id = ?", (job_id,))
    result = cli("apply", uid)
    assert result.exit_code == 1
    assert f"run job-agent prepare {uid} first" in result.output


def test_approvals_list_show_answer_approve(cli, ctx, make_job, fake):
    info = setup_application(ctx, make_job)
    uid = service.prepare_application(ctx, info["job_uid"], browser_factory=fake).approval_uid
    listed = cli("approvals")
    assert listed.exit_code == 0 and uid in listed.output and "Acme" in listed.output

    shown = cli("approvals", "show", uid)
    assert shown.exit_code == 0
    assert "LEGAL DECLARATIONS" in shown.output and "Agreements" in shown.output
    assert "display only" in shown.output

    answered = cli("approvals", "answer", info["app_uid"], "salary", "Negotiable")
    assert answered.exit_code == 0 and "New approval request" in answered.output
    new_uid = ctx.db.scalar("SELECT approval_uid FROM approval_requests WHERE status = 'pending'")
    assert new_uid != uid

    declined = cli("approvals", "approve", new_uid, input="n\n")
    assert declined.exit_code == 1 and repo.get_approval(ctx.db, new_uid)["status"] == "pending"
    approved = cli("approvals", "approve", new_uid, "--note", "looks right", input="y\n")
    assert approved.exit_code == 0, approved.output
    assert "LEGAL DECLARATIONS" in approved.output  # full view shown before the confirmation
    assert repo.get_approval(ctx.db, new_uid)["status"] == "approved"
    assert "submit manually" in approved.output


def test_approvals_reject_requires_note(cli, ctx, make_job, fake):
    info = setup_application(ctx, make_job)
    uid = service.prepare_application(ctx, info["job_uid"], browser_factory=fake).approval_uid
    assert cli("approvals", "reject", uid).exit_code != 0
    result = cli("approvals", "reject", uid, "--note", "fix the cover letter")
    assert result.exit_code == 0 and "documents_ready" in result.output
    assert repo.get_approval(ctx.db, uid)["status"] == "rejected"


def test_applications_list_show_mark_submitted(cli, ctx, make_job, fake):
    info = setup_application(ctx, make_job)
    service.prepare_application(ctx, info["job_uid"], browser_factory=fake)
    listing = cli("applications")
    assert listing.exit_code == 0 and info["app_uid"] in listing.output
    assert cli("applications", "list", "--status", "submitted").output.startswith("No applications")
    shown = cli("applications", "show", info["app_uid"])
    assert shown.exit_code == 0 and "Timeline" in shown.output and "form_prepared" in shown.output
    assert cli("applications", "note", info["app_uid"], "called recruiter").exit_code == 0
    marked = cli("applications", "mark-submitted", info["app_uid"], "--note", "via website")
    assert marked.exit_code == 0 and "marked submitted" in marked.output
    assert repo.get_application(ctx.db, info["app_id"])["status"] == "submitted"


def test_submit_explains_when_browser_submission_disabled(cli, ctx, make_job, fake):
    info = setup_application(ctx, make_job)
    uid = service.prepare_application(ctx, info["job_uid"], browser_factory=fake).approval_uid
    result = cli("applications", "submit", uid)
    assert result.exit_code == 1 and "Browser submission is disabled" in result.output
    assert fake.submissions == 0


def test_submit_with_browser_enabled(cli, ctx, make_job, monkeypatch, home):
    import yaml

    add_sponsorship_answer(ctx)
    info = setup_application(ctx, make_job)
    browser = fake_for(fields=simple_fields())
    monkeypatch.setattr(service, "default_factory", browser)
    service.prepare_application(ctx, info["job_uid"], browser_factory=browser)
    uid = service.set_answer(ctx, info["app_uid"], "certify", "yes")
    service.decide(ctx, uid, approve=True, actor="cli")
    cfg_path = home / "config" / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text())
    data["applications"]["browser_submission_enabled"] = True
    cfg_path.write_text(yaml.safe_dump(data))
    assert cli("applications", "submit", uid, input="n\n").exit_code == 1 and browser.submissions == 0
    result = cli("applications", "submit", uid, input="y\n")
    assert result.exit_code == 0, result.output
    assert "submitted" in result.output and browser.submissions == 1
    again = cli("applications", "submit", uid, input="y\n")
    assert again.exit_code == 1 and "already used" in again.output and browser.submissions == 1


def test_untrusted_labels_are_not_rendered_as_markup(cli, ctx, make_job, monkeypatch):
    info = setup_application(ctx, make_job)
    browser = fake_for(fields=[FormField("q", "[bold red]Injected[/bold red] question?", "text")])
    monkeypatch.setattr(service, "default_factory", browser)
    uid = service.prepare_application(ctx, info["job_uid"], browser_factory=browser).approval_uid
    shown = cli("approvals", "show", uid)
    assert "[bold red]Injected[/bold red]" in shown.output


def test_generic_approval_show_and_approve(cli, ctx):
    from job_agent.models import ApprovalType

    uid = repo.create_approval(ctx.db, ApprovalType.SEND_FOLLOWUP, {"draft": "[red]Hi[/red]"}, summary="Follow up",
                               actor="system", expires_in_hours=1)
    shown = cli("approvals", "show", uid)
    assert shown.exit_code == 0 and "send_followup" in shown.output and "[red]Hi[/red]" in shown.output
    assert cli("approvals", "approve", uid, input="y\n").exit_code == 0
    assert repo.get_approval(ctx.db, uid)["status"] == "approved"
    missing = cli("approvals", "show", "AP-nope")
    assert missing.exit_code == 1 and "not found" in missing.output


def test_set_status_corrects_a_wrong_email_rejection(home):
    """Integration finding: an email could reject the wrong application with no way to undo it."""
    import typer
    from typer.testing import CliRunner

    from job_agent.applications.commands import register
    from job_agent.cli import state
    from job_agent.context import AppContext
    from job_agent.database import repo
    from job_agent.models import PipelineState as S
    from job_agent.utils.time import utcnow_iso

    ctx = AppContext.load()
    now = utcnow_iso()
    job_id = ctx.db.insert("jobs", {
        "job_uid": "J-corr000001", "source_name": "fixture", "source_identifier": "t", "external_id": "c1",
        "employer": "Acme", "employer_normalized": "acme", "title": "Business Analyst",
        "title_normalized": "business analyst", "description_hash": "h", "content_hash": "c",
        "discovered_at": now, "last_checked_at": now, "last_changed_at": now, "created_at": now, "updated_at": now})
    app_id = repo.create_application(ctx.db, job_id, actor="system")
    repo.transition_application(ctx.db, app_id, S.DOCUMENTS_READY, actor="system")
    repo.transition_application(ctx.db, app_id, S.SUBMITTED, actor="user")
    repo.transition_application(ctx.db, app_id, S.REJECTED, actor="email",
                                evidence={"type": "email", "gmail_message_id": "x1"})
    uid = repo.get_application(ctx.db, app_id)["application_uid"]
    ctx.close()

    app = typer.Typer()
    register(app)
    state._ctx = None
    runner = CliRunner()
    bad = runner.invoke(app, ["applications", "set-status", uid, "in_progress"])
    assert bad.exit_code != 0
    ok = runner.invoke(app, ["applications", "set-status", uid, "submitted", "--note", "email was for another role"])
    assert ok.exit_code == 0, ok.output
    ctx = AppContext.load()
    assert repo.get_application(ctx.db, app_id)["status"] == "submitted"
    ctx.close()
