"""Committed example files must contain no secrets and no real personal data."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

SECRET_PATTERNS = [
    re.compile(r"ya29\.[A-Za-z0-9\-_]{10,}"),
    re.compile(r"GOCSPX-[A-Za-z0-9\-_]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"scrypt\$\d+\$"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)(password|secret|token)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}"),
]


def _tracked_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=REPO,
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return [REPO / line for line in out.splitlines() if line]


def test_example_files_have_no_secrets():
    for name in (".env.example", "config/config.example.yaml", "profiles/candidate_profile.example.yaml"):
        text = (REPO / name).read_text()
        for pat in SECRET_PATTERNS:
            assert not pat.search(text), f"{name} matches secret pattern {pat.pattern}"


def test_env_example_values_are_placeholders():
    for line in (REPO / ".env.example").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            _, _, value = line.partition("=")
            assert value.strip("'\"").startswith("<"), f"non-placeholder value in .env.example: {line}"


def test_sensitive_paths_are_gitignored():
    sensitive = [".env", "config/config.yaml", "profiles/candidate_profile.yaml", "data/job_agent.db",
                 "data/secrets/gmail_token.json", "reports/Job_Search_Report_2026-01-01.xlsx",
                 "documents/Acme_Analyst_Resume_2026-01-01.pdf", "logs/job_agent.log"]
    result = subprocess.run(["git", "check-ignore", *sensitive], cwd=REPO, capture_output=True, text=True)
    ignored = set(result.stdout.split())
    assert ignored == set(sensitive)


def test_no_sensitive_files_would_be_committed():
    for path in _tracked_files():
        rel = path.relative_to(REPO).as_posix()
        assert not rel.endswith((".db", ".sqlite", ".xlsx")), rel
        assert rel != ".env" and not rel.startswith("data/secrets/"), rel
        assert rel != "profiles/candidate_profile.yaml", rel
