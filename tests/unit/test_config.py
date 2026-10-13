from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from job_agent.config import load_config, parse_config
from job_agent.errors import ConfigError

REPO = Path(__file__).resolve().parents[2]


def _example() -> dict:
    return yaml.safe_load((REPO / "config" / "config.example.yaml").read_text())


def test_example_config_is_valid(tmp_path):
    cfg = parse_config(_example(), home=tmp_path)
    assert cfg.applications.require_approval is True
    assert cfg.permissions.send_emails is False
    assert cfg.dashboard.host == "127.0.0.1"
    assert abs(sum(cfg.scoring.weights.normalized().values()) - 100) < 1e-9
    assert cfg.db_path == tmp_path / "data" / "job_agent.db"


def test_load_prefers_real_config_over_example(home):
    cfg = load_config(home=home)
    assert cfg.using_example is False
    assert cfg.source_path.name == "config.yaml"


def test_falls_back_to_example_and_flags_it(home):
    (home / "config" / "config.yaml").unlink()
    cfg = load_config(home=home)
    assert cfg.using_example is True


def test_missing_config_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(home=tmp_path)


def test_missing_required_section(tmp_path):
    data = _example()
    del data["search"]
    with pytest.raises(ConfigError, match="search"):
        parse_config(data, home=tmp_path)


def test_unknown_keys_rejected(tmp_path):
    data = _example()
    data["scoring"]["wieghts"] = {}
    with pytest.raises(ConfigError, match="wieghts"):
        parse_config(data, home=tmp_path)


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda d: d["applications"].update(require_approval=False), "require_approval"),
        (lambda d: d["permissions"].update(send_emails=True), "send_emails"),
        (lambda d: d["llm"].update(base_url="https://api.example.com"), "localhost"),
        (lambda d: d["dashboard"].update(host="0.0.0.0"), "allow_non_localhost"),
        (lambda d: d["gmail"].update(scopes=["https://mail.google.com/"]), "read-only"),
        (lambda d: d["scoring"].update(possible_threshold=90, strong_threshold=80), "possible_threshold"),
        (lambda d: d["scoring"]["weights"].update(skills=-1), "weights"),
        (lambda d: d["app"].update(timezone="Mars/Olympus"), "timezone"),
        (lambda d: d["scheduler"]["tasks"].update(report={"cron": "* *"}), "5 fields"),
        (lambda d: d["scheduler"]["tasks"].update(report={"cron": "0 1 * * *", "interval_minutes": 30}),
         "exactly one"),
    ],
)
def test_invalid_values_rejected(tmp_path, mutate, match):
    data = _example()
    mutate(data)
    with pytest.raises(ConfigError, match=match):
        parse_config(data, home=tmp_path)


def test_secret_like_keys_rejected(tmp_path):
    data = _example()
    data["gmail"]["client_secret"] = "abc123"
    with pytest.raises(ConfigError, match="secret"):
        parse_config(data, home=tmp_path)


def test_non_localhost_dashboard_requires_opt_in(tmp_path):
    data = _example()
    data["dashboard"].update(host="100.64.0.5", allow_non_localhost=True)
    cfg = parse_config(data, home=tmp_path)
    assert cfg.dashboard.host == "100.64.0.5"


def test_weights_are_configurable(tmp_path):
    data = _example()
    data["scoring"]["weights"] = {"career_interest": 1, "skills": 1, "experience_level": 0, "domain_relevance": 0,
                                  "location": 0, "compensation": 0, "employer_growth": 0}
    cfg = parse_config(data, home=tmp_path)
    assert cfg.scoring.weights.normalized()["skills"] == 50


def test_invalid_yaml(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text("search: [unclosed")
    with pytest.raises(ConfigError, match="YAML"):
        load_config(home=tmp_path)


def test_explicit_config_path_and_env(home, monkeypatch):
    alt = home / "alt.yaml"
    alt.write_text((home / "config" / "config.yaml").read_text())
    assert load_config(alt, home=home).source_path == alt.resolve()
    monkeypatch.setenv("JOB_AGENT_CONFIG", str(alt))
    assert load_config(home=home).source_path == alt.resolve()
    monkeypatch.setenv("JOB_AGENT_CONFIG", str(home / "nope.yaml"))
    with pytest.raises(ConfigError):
        load_config(home=home)


@pytest.mark.parametrize("entry, host", [("https://hp-aero.tail1234.ts.net", "hp-aero.tail1234.ts.net"),
                                         ("hp-aero.tail1234.ts.net:443", "hp-aero.tail1234.ts.net"),
                                         ("HP-Aero.Tail1234.ts.net.", "hp-aero.tail1234.ts.net")])
def test_allowed_hosts_are_reduced_to_host_names(tmp_path, entry, host):
    data = _example()
    data["dashboard"]["allowed_hosts"] = [entry]
    assert parse_config(data, home=tmp_path).dashboard.allowed_hosts == [host]


@pytest.mark.parametrize("bad", ["https://user:pw@host.ts.net", "https://host.ts.net/some/path", ""])
def test_allowed_hosts_rejects_non_hosts(tmp_path, bad):
    data = _example()
    data["dashboard"]["allowed_hosts"] = [bad]
    with pytest.raises(ConfigError, match="allowed_hosts"):
        parse_config(data, home=tmp_path)


def test_runtime_files_are_private(config):
    import os
    import stat

    from job_agent.config import ensure_directories
    from job_agent.database import Database

    if os.name != "posix":
        pytest.skip("POSIX permissions")
    ensure_directories(config)
    Database(config.db_path).close()
    for d in (config.data_dir, config.logs_dir, config.reports_dir, config.documents_dir):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d
    assert stat.S_IMODE(config.db_path.stat().st_mode) == 0o600
