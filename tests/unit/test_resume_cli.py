"""CLI: prepare / documents list / approve / reject (fresh Typer app + CliRunner)."""

from __future__ import annotations

import json

import pytest
import typer
from typer.testing import CliRunner

from job_agent.resume.commands import register

runner = CliRunner()


@pytest.fixture
def cli(home, monkeypatch):
    from job_agent import cli as cli_module

    monkeypatch.setattr(cli_module.state, "config_path", None)
    monkeypatch.setattr(cli_module.state, "_ctx", None)
    monkeypatch.setattr(cli_module.console, "width", 200)
    monkeypatch.setattr(cli_module.err_console, "width", 200)
    app = typer.Typer()
    register(app)

    def invoke(*args):
        return runner.invoke(app, list(args), catch_exceptions=False)

    yield invoke
    if cli_module.state._ctx is not None:
        cli_module.state._ctx.close()


@pytest.fixture
def job_uid(make_job):
    make_job(job_uid="J-cli0000001", description_text="SQL, Python and Tableau dashboards for reporting.",
             required_qualifications=json.dumps(["SQL", "Python"]))
    return "J-cli0000001"


def test_prepare_prints_files_bullets_and_next_step(cli, job_uid, db):
    result = cli("prepare", job_uid, "--no-llm")
    assert result.exit_code == 0, result.output
    out = result.output
    for fact_id in ("agency_regression", "agency_dashboards", "agency_python_migration", "smallco_flask",
                    "smallco_dashboard"):
        assert fact_id in out
    assert "resume" in out and "cover_letter" in out and "pdf" in out and "docx" in out
    assert "Next step" in out and "documents approve" in out
    assert db.scalar("SELECT COUNT(*) FROM document_versions") == 6
    assert db.scalar("SELECT status FROM applications") == "documents_ready"


def test_prepare_no_cover_letter_and_unknown_job(cli, job_uid, db):
    assert cli("prepare", job_uid, "--no-llm", "--no-cover-letter").exit_code == 0
    assert db.scalar("SELECT COUNT(*) FROM document_versions WHERE doc_type = 'cover_letter'") == 0
    bad = cli("prepare", "J-missing0000")
    assert bad.exit_code == 1


def test_documents_list_approve_reject(cli, job_uid, db):
    assert "No documents yet" in cli("documents", "list", job_uid).output
    cli("prepare", job_uid, "--no-llm")
    listing = cli("documents", "list", job_uid)
    assert listing.exit_code == 0 and "needs_review" in listing.output and "draft" in listing.output
    resume_pdf = db.scalar("SELECT id FROM document_versions WHERE doc_type = 'resume' AND format = 'pdf'")
    approved = cli("documents", "approve", str(resume_pdf), "--note", "checked")
    assert approved.exit_code == 0 and "approved" in approved.output
    assert {r["status"] for r in db.query_all(
        "SELECT status FROM document_versions WHERE doc_type = 'resume'")} == {"approved"}
    letter = db.scalar("SELECT id FROM document_versions WHERE doc_type = 'cover_letter' AND format = 'docx'")
    rejected = cli("documents", "reject", str(letter))
    assert rejected.exit_code == 0 and "rejected" in rejected.output
    assert cli("documents", "approve", "4242").exit_code == 1


MARKUP = "[bold red]Hire me[/bold red] [/] [link=https://evil.example]x[/link]"


def test_documents_list_prints_listing_text_literally(cli, make_job):
    make_job(job_uid="J-cli0000002", title=f"Data Analyst {MARKUP}", employer="Acme [/] Analytics")
    cli("prepare", "J-cli0000002", "--no-llm")
    listing = cli("documents", "list", "J-cli0000002")
    assert listing.exit_code == 0, listing.output
    assert "Acme [/] Analytics" in listing.output
    assert "[bold red]Hire me[/bold red]" in listing.output  # shown as text, not applied as markup


def test_prepare_prints_llm_and_listing_text_literally(cli, job_uid, monkeypatch):
    from job_agent.resume import service
    from job_agent.resume.models import Claim, Issue

    real = service.prepare_documents

    def patched(*args, **kwargs):
        result = real(*args, **kwargs)
        result.claims.append(Claim(text=f"Reworded {MARKUP}", fact_ids=["agency_regression"],
                                   generator="llm:[/]", section="experience:[x]"))
        result.issues.append(Issue(code="rewording_rejected", severity="info", document="resume",
                                   message=f"LLM rewording rejected ('{MARKUP}')"))
        return result

    monkeypatch.setattr(service, "prepare_documents", patched)
    result = cli("prepare", job_uid, "--no-llm")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "[bold red]Hire me[/bold red]" in out and "llm:[/]" in out and "experience:[x]" in out
