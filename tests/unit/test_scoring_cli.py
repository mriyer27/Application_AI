from __future__ import annotations

import json

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

DESCRIPTION = (
    "Acme Analytics is hiring a Business Analyst to join our insights team in New York. "
    "You will partner with finance and operations leaders to analyze trends, build clear reports, "
    "and recommend improvements. You will receive mentorship from experienced colleagues. "
    "We value curiosity, clear writing, and ownership of outcomes. This is a full-time position "
    "based in our New York office with a collaborative culture. We will not sponsor visas for this role."
)


@pytest.fixture
def cli(home, monkeypatch):
    import job_agent.cli as cli_mod
    from job_agent.scoring.commands import register

    monkeypatch.setattr(cli_mod.state, "_ctx", None)
    monkeypatch.setattr(cli_mod.state, "config_path", None)
    monkeypatch.setattr(cli_mod, "console", Console(width=250, color_system=None))
    monkeypatch.setattr(cli_mod, "err_console", Console(width=250, stderr=True, color_system=None))
    app = typer.Typer()
    register(app)
    runner = CliRunner()

    def invoke(*args: str):
        return runner.invoke(app, list(args), catch_exceptions=False)

    yield invoke
    if cli_mod.state._ctx is not None:
        cli_mod.state._ctx.close()


@pytest.fixture
def seeded(make_job):
    common = dict(salary_min=95000.0, salary_max=120000.0, salary_currency="USD", salary_period="year",
                  salary_is_base=1, work_arrangement="onsite")
    good = make_job(description_text=DESCRIPTION, application_url="https://example.com/apply/1",
                    required_qualifications=json.dumps(["SQL and Python", "Tableau", "0-2 years of experience"]),
                    **common)
    weak = make_job(title="Staff Software Engineer", employer="Bolt Systems", description_text=DESCRIPTION,
                    required_qualifications=json.dumps(["10+ years of experience", "Java, Kubernetes"]), **common)
    return {"good": good, "weak": weak}


def test_score_then_jobs_table(cli, db, seeded):
    result = cli("score", "--no-llm")
    assert result.exit_code == 0, result.output
    assert "Scored" in result.output and "rules-only" in result.output
    assert db.scalar("SELECT COUNT(*) FROM job_scores") == 2

    again = cli("score", "--no-llm")
    assert "Unchanged (kept current score)" in again.output

    listing = cli("jobs")
    assert listing.exit_code == 0, listing.output
    out = listing.output
    assert "J-test00001" in out and "J-test00002" in out
    assert out.index("J-test00001") < out.index("J-test00002")  # best first
    assert "Acme Analytics" in out and "Business Analyst" in out and "New York, NY" in out
    assert "$95,000-$120,000/year" in out and "no_sponsorship" in out and "needs_review" in out

    weak_only = cli("jobs", "--category", "weak_match")
    assert "J-test00002" in weak_only.output and "J-test00001" not in weak_only.output
    assert "J-test00001" in cli("jobs", "--top", "1").output


def test_score_explicit_job_and_unknown_job(cli, db, seeded):
    result = cli("score", "--no-llm", "--job", "J-test00002")
    assert result.exit_code == 0, result.output
    assert db.scalar("SELECT COUNT(*) FROM job_scores") == 1
    missing = cli("score", "--no-llm", "--job", "J-nope")
    assert missing.exit_code == 1 and "not found" in missing.output


def test_job_show_full_explanation(cli, seeded):
    cli("score", "--no-llm")
    result = cli("job", "show", "J-test00001")
    assert result.exit_code == 0, result.output
    out = result.output
    for expected in ("Business Analyst", "Acme Analytics", "Score ", "Factors", "career_interest", "skills",
                     "employer_growth", "Matching qualifications", "Eligibility flags", "no_sponsorship",
                     '"We will not sponsor visas for this role."', "Review: the listing states",
                     "Evidence from your verified profile", "agency_python_migration",
                     "Recommended next action", "Review eligibility statement before investing time",
                     "https://example.com/jobs/1", "https://example.com/apply/1", "Reasons to apply"):
        assert expected in out, expected
    assert cli("job", "show", "1").exit_code == 0  # numeric id also works


def test_job_show_unscored_and_missing(cli, seeded):
    unscored = cli("job", "show", "J-test00002")
    assert unscored.exit_code == 0 and "Not scored yet" in unscored.output
    missing = cli("job", "show", "J-nope")
    assert missing.exit_code == 1 and "not found" in missing.output


def test_job_mark_and_note(cli, db, seeded):
    cli("score", "--no-llm")
    result = cli("job", "mark", "J-test00001", "irrelevant")
    assert result.exit_code == 0, result.output
    assert db.scalar("SELECT user_relevance FROM jobs WHERE job_uid = 'J-test00001'") == "irrelevant"
    assert "J-test00001" not in cli("jobs").output
    assert "J-test00001" in cli("jobs", "--include-filtered").output
    assert cli("job", "mark", "J-test00001", "clear").exit_code == 0
    assert db.scalar("SELECT user_relevance FROM jobs WHERE job_uid = 'J-test00001'") is None
    assert cli("job", "mark", "J-test00001", "maybe").exit_code != 0  # rejected by the CLI

    note = cli("job", "note", "J-test00001", "Recruiter call on Friday")
    assert note.exit_code == 0, note.output
    assert db.scalar("SELECT user_notes FROM jobs WHERE job_uid = 'J-test00001'") == "Recruiter call on Friday"
    assert "Recruiter call on Friday" in cli("job", "show", "J-test00001").output
    assert db.scalar("SELECT COUNT(*) FROM audit_events WHERE action IN "
                     "('job_relevance_marked', 'job_note_updated') AND actor = 'cli'") == 3
    missing = cli("job", "note", "J-nope", "x")
    assert missing.exit_code == 1 and "not found" in missing.output


def test_untrusted_markup_is_escaped(cli, make_job):
    make_job(title="[bold red]Business Analyst[/bold red]", description_text=DESCRIPTION)
    cli("score", "--no-llm")
    out = cli("jobs").output
    assert "[bold red]Business Analyst[/bold red]" in out
