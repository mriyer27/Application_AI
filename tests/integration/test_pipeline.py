"""End-to-end cycle on mock data: discover (fixture source) -> score -> draft documents.

No network, no LLM. Exercises the real modules together, including the
``run-once`` CLI command and the scheduled task wiring.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from job_agent import pipeline
from job_agent.models import RunStatus
from job_agent.tasks import run_task

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "discovery"


@pytest.fixture
def fixture_source(ctx, monkeypatch):
    """Point discovery at the mock fixture adapter only."""
    target = ctx.config.home / "fixture_jobs.json"
    shutil.copyfile(FIXTURES / "fixture_jobs.json", target)
    from job_agent.discovery import service as discovery_service

    real = discovery_service.discover_jobs

    def only_fixture(c, **kwargs):
        kwargs["sources"] = ["fixture:fixture_jobs.json"]
        return real(c, **kwargs)

    monkeypatch.setattr(discovery_service, "discover_jobs", only_fixture)
    return target


def test_cycle_discovers_scores_and_drafts_documents(ctx, fixture_source):
    ctx.config.documents.auto_prepare_top_n = 2
    summary = pipeline.run_cycle(ctx)
    assert summary["errors"] == []
    assert summary["steps"]["discover"]["totals"]["new"] >= 1
    assert summary["steps"]["score"]["scored"] >= 1
    assert "rules-only" in summary["steps"]["llm"]  # llm disabled in test config
    db = ctx.db
    assert db.scalar("SELECT COUNT(*) FROM job_scores WHERE is_current = 1") >= 1
    docs = summary["steps"]["documents"]
    shortlisted = [r["job_uid"] for r in db.query_all("SELECT job_uid FROM jobs WHERE pipeline_state = 'shortlisted'")]
    assert shortlisted, "fixture data should yield shortlisted jobs"
    assert docs["failed"] == []
    assert sorted(docs["prepared"]) == sorted(shortlisted[:2])
    for job_uid in docs["prepared"]:
        app = db.query_one("SELECT a.status FROM applications a JOIN jobs j ON j.id = a.job_id "
                           "WHERE j.job_uid = ?", (job_uid,))
        assert app["status"] == "documents_ready"
    # Nothing consequential happened.
    assert db.scalar("SELECT COUNT(*) FROM approval_requests") == 0
    assert db.scalar("SELECT COUNT(*) FROM applications WHERE submitted_at IS NOT NULL") == 0


def test_cycle_is_idempotent(ctx, fixture_source):
    pipeline.run_cycle(ctx)
    jobs_before = ctx.db.scalar("SELECT COUNT(*) FROM jobs")
    docs_before = ctx.db.scalar("SELECT COUNT(*) FROM document_versions")
    second = pipeline.run_cycle(ctx)
    assert second["steps"]["discover"]["totals"]["new"] == 0
    assert second["steps"]["score"]["scored"] == 0
    assert ctx.db.scalar("SELECT COUNT(*) FROM jobs") == jobs_before
    assert ctx.db.scalar("SELECT COUNT(*) FROM document_versions") == docs_before


def test_discovery_failure_still_scores_existing_jobs(ctx, fixture_source, monkeypatch):
    pipeline.run_cycle(ctx, auto_prepare=False)
    from job_agent.discovery import service as discovery_service
    from job_agent.errors import RecoverableError

    def down(c, **kwargs):
        raise RecoverableError("all sources unreachable")

    monkeypatch.setattr(discovery_service, "discover_jobs", down)
    ctx.db.execute("UPDATE job_scores SET is_current = 0")
    summary = pipeline.run_cycle(ctx, auto_prepare=False)
    assert summary["partial"] is True
    assert any("discover" in e for e in summary["errors"])
    assert summary["steps"]["score"]["scored"] >= 1
    assert ctx.db.scalar("SELECT COUNT(*) FROM errors WHERE component = 'pipeline:discover'") == 1


def test_task_and_run_once_cli(ctx, fixture_source, home):
    result = run_task(ctx, "discover_and_score", trigger="manual")
    assert result.status in {RunStatus.SUCCEEDED, RunStatus.PARTIAL}
    from job_agent.cli import app, state

    state._ctx = None
    out = CliRunner().invoke(app, ["run-once", "--no-llm", "--no-documents"], catch_exceptions=False)
    assert out.exit_code == 0, out.output
    assert "discover_and_score" in out.output


def test_offline_cycle_is_a_retryable_failure(ctx, fixture_source, monkeypatch):
    """Correctness review: every source failing (laptop offline at wake) must not count as success."""
    from job_agent.discovery import service as discovery_service
    from job_agent.errors import RecoverableError

    def down(c, **kwargs):
        raise RecoverableError("network unreachable")

    monkeypatch.setattr(discovery_service, "discover_jobs", down)
    result = run_task(ctx, "discover_and_score", trigger="scheduled")
    assert result.status == RunStatus.FAILED and result.error_kind.value == "recoverable"
    assert ctx.db.scalar("SELECT last_success_at FROM scheduled_tasks WHERE task_name = 'discover_and_score'") is None
