"""Shared fixtures: an isolated project home with config, profile and database."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Never let tests read the developer's real .env, config, profile or database."""
    for var in ("JOB_AGENT_HOME", "JOB_AGENT_CONFIG", "JOB_AGENT_DASHBOARD_USERNAME",
                "JOB_AGENT_DASHBOARD_PASSWORD_HASH", "JOB_AGENT_SESSION_SECRET", "JOB_AGENT_CONTACT_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A temporary project home containing config/config.yaml and a test profile."""
    root = tmp_path / "home"
    (root / "config").mkdir(parents=True)
    (root / "profiles").mkdir()
    data = yaml.safe_load((REPO_ROOT / "config" / "config.example.yaml").read_text())
    # Tests never touch real services by default.
    data["llm"]["enabled"] = False
    (root / "config" / "config.yaml").write_text(yaml.safe_dump(data))
    shutil.copyfile(FIXTURES / "profile_test.yaml", root / "profiles" / "candidate_profile.yaml")
    shutil.copyfile(REPO_ROOT / "config" / "config.example.yaml", root / "config" / "config.example.yaml")
    shutil.copyfile(REPO_ROOT / "profiles" / "candidate_profile.example.yaml",
                    root / "profiles" / "candidate_profile.example.yaml")
    monkeypatch.setenv("JOB_AGENT_HOME", str(root))
    return root


@pytest.fixture
def config(home):
    from job_agent.config import load_config

    return load_config(home=home)


@pytest.fixture
def ctx(config):
    from job_agent.context import AppContext
    from job_agent.config import Secrets

    c = AppContext.from_config(config, Secrets())
    yield c
    c.close()


@pytest.fixture
def db(ctx):
    return ctx.db


@pytest.fixture
def profile(ctx):
    return ctx.profile


@pytest.fixture
def make_job(db):
    """Insert a minimal job row and return its id."""
    import itertools

    from job_agent.utils.time import utcnow_iso

    counter = itertools.count(1)

    def _make(**overrides) -> int:
        n = next(counter)
        now = utcnow_iso()
        values = {
            "job_uid": f"J-test{n:05d}",
            "source_name": "fixture",
            "source_identifier": "test",
            "external_id": f"ext-{n}",
            "employer": "Acme Analytics",
            "employer_normalized": "acme analytics",
            "title": "Business Analyst",
            "title_normalized": "business analyst",
            "location": "New York, NY",
            "description_text": "Analyze data with SQL and Python.",
            "description_hash": f"h{n}",
            "content_hash": f"c{n}",
            "job_url": f"https://example.com/jobs/{n}",
            "job_url_canonical": f"https://example.com/jobs/{n}",
            "discovered_at": now,
            "last_checked_at": now,
            "last_changed_at": now,
            "created_at": now,
            "updated_at": now,
        }
        values.update(overrides)
        return db.insert("jobs", values)

    return _make
