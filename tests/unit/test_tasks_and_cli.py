from __future__ import annotations

import pytest
from typer.testing import CliRunner

from job_agent import tasks
from job_agent.database import repo
from job_agent.errors import RecoverableError
from job_agent.models import RunStatus


@pytest.fixture
def fake_tasks(monkeypatch):
    calls = []

    def ok(ctx, run):
        calls.append(run.task_name)
        return {"count": 3}

    def boom(ctx, run):
        raise RecoverableError("network down")

    def partial(ctx, run):
        return {"partial": True, "failed_sources": ["x"]}

    import sys
    import types

    mod = types.ModuleType("fake_task_mod")
    mod.ok, mod.boom, mod.partial = ok, boom, partial
    monkeypatch.setitem(sys.modules, "fake_task_mod", mod)
    for name in ("ok", "boom", "partial"):
        monkeypatch.setitem(tasks.TASKS, f"t_{name}", tasks.TaskSpec(f"t_{name}", f"fake_task_mod:{name}", name))
    return calls


def test_run_task_success_records_run(ctx, fake_tasks):
    result = tasks.run_task(ctx, "t_ok", trigger="manual")
    assert result.status == RunStatus.SUCCEEDED and result.summary == {"count": 3}
    row = ctx.db.query_one("SELECT * FROM agent_runs WHERE run_uid = ?", (result.run_uid,))
    assert row["status"] == "succeeded" and row["finished_at"]
    assert repo.get_state(ctx.db, "last_successful_run_at")


def test_run_task_failure_is_recorded_not_raised(ctx, fake_tasks):
    result = tasks.run_task(ctx, "t_boom", trigger="scheduled")
    assert result.status == RunStatus.FAILED and "network down" in result.error
    err = ctx.db.query_one("SELECT * FROM errors ORDER BY id DESC LIMIT 1")
    assert err["error_kind"] == "recoverable" and err["retryable"] == 1
    # lock released after failure
    assert ctx.db.scalar("SELECT lock_owner FROM scheduled_tasks WHERE task_name = 't_boom'") is None
    assert ctx.db.scalar("SELECT consecutive_failures FROM scheduled_tasks WHERE task_name = 't_boom'") == 1


def test_partial_run(ctx, fake_tasks):
    assert tasks.run_task(ctx, "t_partial").status == RunStatus.PARTIAL


def test_pause_skips_scheduled_but_not_manual(ctx, fake_tasks):
    repo.set_paused(ctx.db, True, actor="cli")
    assert tasks.run_task(ctx, "t_ok", trigger="scheduled").status == RunStatus.SKIPPED
    assert fake_tasks == []
    assert tasks.run_task(ctx, "t_ok", trigger="manual").status == RunStatus.SUCCEEDED


def test_overlapping_run_skipped(ctx, fake_tasks):
    assert repo.acquire_task_lock(ctx.db, "t_ok", "someone-else", 30)
    result = tasks.run_task(ctx, "t_ok", trigger="scheduled")
    assert result.status == RunStatus.SKIPPED and fake_tasks == []


def test_unknown_task_rejected(ctx):
    with pytest.raises(Exception, match="unknown task"):
        tasks.run_task(ctx, "rm -rf /")


def test_health_task(ctx):
    result = tasks.run_task(ctx, "health")
    assert result.status == RunStatus.SUCCEEDED
    assert result.summary["overall"] in {"ok", "warn", "fail"}


# ---------------------------------------------------------------- CLI ---- #
runner = CliRunner()


def _cli(*args):
    from job_agent.cli import app

    return runner.invoke(app, list(args), catch_exceptions=False)


def test_cli_help_lists_commands():
    out = _cli("--help").output
    for cmd in ("init", "doctor", "status", "pause", "resume", "config-check", "db", "tasks"):
        assert cmd in out


def test_cli_init_creates_files_without_overwriting(tmp_path, monkeypatch):
    import shutil
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    home = tmp_path / "proj"
    (home / "config").mkdir(parents=True)
    (home / "profiles").mkdir()
    shutil.copy(repo_root / "config/config.example.yaml", home / "config")
    shutil.copy(repo_root / "profiles/candidate_profile.example.yaml", home / "profiles")
    monkeypatch.setenv("JOB_AGENT_HOME", str(home))
    result = _cli("init")
    assert result.exit_code == 0, result.output
    assert (home / "config/config.yaml").exists() and (home / "profiles/candidate_profile.yaml").exists()
    assert (home / "data/job_agent.db").exists()
    assert ((home / ".env").stat().st_mode & 0o777) == 0o600
    (home / "profiles/candidate_profile.yaml").write_text("# mine\n")
    result = _cli("init")
    assert "kept existing profiles/candidate_profile.yaml" in result.output
    assert (home / "profiles/candidate_profile.yaml").read_text() == "# mine\n"


def test_cli_doctor_and_status(home):
    result = _cli("doctor")
    assert result.exit_code == 0, result.output
    assert "database" in result.output and "ollama" in result.output
    result = _cli("status")
    assert result.exit_code == 0 and "Automation" in result.output


def test_cli_pause_resume(home):
    assert "paused" in _cli("pause", "--reason", "testing").output.lower()
    assert "PAUSED" in _cli("status").output
    assert "resumed" in _cli("resume").output.lower()


def test_cli_config_check_and_db(home, tmp_path):
    assert _cli("config-check").exit_code == 0
    out = _cli("db", "backup", "--dest", str(tmp_path / "b.db"))
    assert out.exit_code == 0 and (tmp_path / "b.db").exists()
    assert "schema_migrations" in _cli("db", "info").output


def test_cli_bad_config_exit_code(home):
    (home / "config/config.yaml").write_text("search: {}\n")
    result = runner.invoke(__import__("job_agent.cli", fromlist=["app"]).app, ["doctor"])
    assert result.exit_code == 2


def test_init_home_option_wins_over_env(tmp_path, monkeypatch):
    """Correctness review: `init --home B` with JOB_AGENT_HOME=A must initialize B only."""
    import shutil
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    a, b = tmp_path / "A", tmp_path / "B"
    for home in (a, b):
        (home / "config").mkdir(parents=True)
        (home / "profiles").mkdir()
        shutil.copy(repo_root / "config/config.example.yaml", home / "config")
        shutil.copy(repo_root / "profiles/candidate_profile.example.yaml", home / "profiles")
    monkeypatch.setenv("JOB_AGENT_HOME", str(a))
    result = _cli("init", "--home", str(b))
    assert result.exit_code == 0, result.output
    assert (b / "data/job_agent.db").exists() and not (a / "data/job_agent.db").exists()


def test_status_does_not_count_expired_approvals(home):
    from job_agent.context import AppContext
    from job_agent.database import repo as _repo
    from job_agent.models import ApprovalType
    from job_agent.utils.time import utcnow_iso

    c = AppContext.load()
    now = utcnow_iso()
    job_id = c.db.insert("jobs", {"job_uid": "J-exp0000001", "source_name": "fixture", "source_identifier": "t",
                                  "external_id": "e1", "employer": "Acme", "employer_normalized": "acme",
                                  "title": "Analyst", "title_normalized": "analyst", "description_hash": "h",
                                  "content_hash": "c", "discovered_at": now, "last_checked_at": now,
                                  "last_changed_at": now, "created_at": now, "updated_at": now})
    app_id = _repo.create_application(c.db, job_id, actor="system")
    uid = _repo.create_approval(c.db, ApprovalType.SUBMIT_APPLICATION, {"x": 1}, summary="s", actor="system",
                                application_id=app_id)
    c.db.execute("UPDATE approval_requests SET expires_at = '2000-01-01T00:00:00+00:00' WHERE approval_uid = ?",
                 (uid,))
    c.close()
    out = _cli("status").output
    assert "Pending approvals: 0" in out
