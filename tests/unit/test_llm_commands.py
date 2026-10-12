"""`job-agent llm ...` commands, run through a fresh Typer app with HTTP mocked by respx."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
import typer
import yaml
from typer.testing import CliRunner

from job_agent.llm import client as client_mod
from job_agent.llm import commands, hardware
from job_agent.llm.testing import chat_envelope

BASE = "http://127.0.0.1:11434"
MODEL = "qwen2.5:7b-instruct"

ANALYSIS = {
    "summary": "Entry-level business analyst role.", "role_family": "business analysis", "seniority": "entry",
    "years_experience_required": None, "required_skills": ["SQL", "Python", "Kubernetes"], "preferred_skills": [],
    "responsibilities": ["Analyze data"], "coding_intensity": "low", "ai_relevance": 0.2,
    "business_relevance": 0.8, "analytics_relevance": 0.9, "product_relevance": 0.1,
    "sponsorship_statements": [], "red_flags": [], "fit_explanation": "Uses SQL and Python.",
}
BENCH = {"title": "Business Analyst", "seniority": "entry", "years_experience_required": 1,
         "required_skills": ["SQL"], "preferred_skills": [], "sponsorship_statements": []}


@pytest.fixture
def run(home, monkeypatch):
    from job_agent import cli

    monkeypatch.setattr(cli.state, "_ctx", None)
    monkeypatch.setattr(cli.state, "config_path", None)
    for console in (cli.console, cli.err_console):  # wide, deterministic rich output
        monkeypatch.setattr(console, "_width", 200)
    client_mod.reset_probe_cache()
    app = typer.Typer()

    @app.callback()
    def _root() -> None:  # a group, like the real CLI
        pass

    commands.register(app)
    runner = CliRunner()

    def _run(*args: str):
        return runner.invoke(app, list(args))

    yield _run
    if cli.state._ctx is not None:
        cli.state._ctx.close()


def _enable_llm(home) -> None:
    path = home / "config" / "config.yaml"
    data = yaml.safe_load(path.read_text())
    data["llm"]["enabled"] = True
    path.write_text(yaml.safe_dump(data))


def _tags(*names: str) -> httpx.Response:
    return httpx.Response(200, json={"models": [
        {"name": n, "size": 4_700_000_000, "details": {"parameter_size": "7.6B", "quantization_level": "Q4_K_M"}}
        for n in names]})


def test_check_ok(run):
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/version").respond(json={"version": "0.12.6"})
        mock.get("/api/tags").mock(return_value=_tags(MODEL, "llama3.2:3b"))
        result = run("llm", "check")
    assert result.exit_code == 0, result.output
    assert "0.12.6" in result.output and MODEL in result.output and "configured" in result.output
    assert "OK" in result.output


def test_check_server_down(run):
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/version").mock(side_effect=httpx.ConnectError("refused"))
        result = run("llm", "check")
    assert result.exit_code == 1
    assert "not reachable" in result.output and "ollama serve" in result.output


def test_recommend(run, monkeypatch):
    laptop = hardware.Hardware(total_ram_gb=15.4, cpu_count=16, cpu_model="AMD Ryzen 7 5800U with Radeon Graphics",
                               gpu=None, os="Windows", physical_cores=8)
    monkeypatch.setattr(hardware, "detect_hardware", lambda: laptop)
    result = run("llm", "recommend")
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "Ryzen 7 5800U" in out and "ollama pull qwen2.5:7b-instruct" in out
    assert "qwen2.5:3b-instruct" in out and "avoid" in out
    assert "runs* is not the same as a model that is *reliable*" in out
    for word in ("JSON validity", "Instruction following", "Hallucination rate", "Context length", "Speed"):
        assert word in out


def test_benchmark(run):
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/tags").mock(return_value=_tags(MODEL))
        chat = mock.post("/api/chat").mock(return_value=httpx.Response(200, json=chat_envelope(MODEL,
                                                                                            json.dumps(BENCH))))
        result = run("llm", "benchmark", "--runs", "2")
    assert result.exit_code == 0, result.output
    assert chat.call_count == 2
    assert "2/2" in result.output and "75.0 tokens/s" in result.output and "150.0 tokens/s" in result.output


def test_analyze_uses_cache_and_validates(run, home, make_job):
    _enable_llm(home)
    make_job(job_uid="J-cli0000001", description_text="We need SQL and Python skills. Remote OK.")
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/tags").mock(return_value=_tags(MODEL))
        chat = mock.post("/api/chat").mock(return_value=httpx.Response(200, json=chat_envelope(
            MODEL, json.dumps(ANALYSIS))))
        first = run("llm", "analyze", "J-cli0000001")
        second = run("llm", "analyze", "J-cli0000001")
        third = run("llm", "analyze", "J-cli0000001", "--no-cache")
    assert first.exit_code == 0, first.output
    payload = json.loads(first.output[: first.output.rindex("}") + 1])
    assert payload["required_skills"] == ["SQL", "Python"]  # "Kubernetes" dropped: not in the posting
    assert payload["model"] == MODEL
    assert "from cache" in " ".join(second.output.split()) and "analyzed in" in " ".join(third.output.split())
    assert chat.call_count == 2


def test_analyze_unknown_job(run, home):
    _enable_llm(home)
    with respx.mock(base_url=BASE):
        result = run("llm", "analyze", "J-missing")
    assert result.exit_code == 1 and "No job" in result.output


def test_analyze_requires_llm_enabled(run):
    with respx.mock(base_url=BASE):
        result = run("llm", "analyze", "J-anything")
    assert result.exit_code == 2 and "llm.enabled is false" in result.output


def test_analyze_ollama_down_is_readable_error(run, home, make_job):
    _enable_llm(home)
    make_job(job_uid="J-cli0000002")
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/tags").mock(side_effect=httpx.ConnectError("refused"))
        result = run("llm", "analyze", "J-cli0000002")
    assert result.exit_code == 1 and "recoverable" in result.output and "not reachable" in result.output


def test_only_working_commands_registered():
    app = typer.Typer()
    commands.register(app)
    group = typer.main.get_group(app)
    llm = group.commands["llm"]
    assert sorted(llm.commands) == ["analyze", "benchmark", "check", "recommend"]
