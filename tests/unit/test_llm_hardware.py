"""Hardware detection, model recommendations and the benchmark (mocked server)."""

from __future__ import annotations

import pytest

from job_agent.config import LLMSection
from job_agent.llm import hardware
from job_agent.llm.benchmark import BENCHMARK_POSTING, BenchmarkExtraction, benchmark
from job_agent.llm.errors import LLMUnavailable
from job_agent.llm.hardware import Hardware, detect_hardware, recommend_models
from job_agent.llm.testing import FakeOllama

#: HP Pavilion Aero 13 (Ryzen 7 5800U, 8C/16T, integrated Radeon, 16 GB).
AERO_13 = Hardware(total_ram_gb=15.4, cpu_count=16, cpu_model="AMD Ryzen 7 5800U with Radeon Graphics", gpu=None,
                   os="Windows", physical_cores=8, available_ram_gb=9.0, arch="AMD64")


def _by_role(recs, role):
    return [r.name for r in recs if r.role == role]


def test_recommendations_for_16gb_cpu_only_laptop():
    recs = recommend_models(AERO_13)
    assert recs[0].name == "qwen2.5:7b-instruct" and recs[0].role == "default"
    assert recs[0].approx_size_gb == pytest.approx(4.7)
    assert {"qwen2.5:3b-instruct", "llama3.2:3b"} <= set(_by_role(recs, "fast"))
    avoid = [r for r in recs if r.role == "avoid"]
    assert avoid and all(r.approx_size_gb >= 9 for r in avoid)
    assert any("14b" in r.name for r in avoid) and "13B" in avoid[0].why
    assert all(r.why for r in recs)


def test_recommendations_scale_with_memory_and_gpu():
    small = recommend_models(Hardware(total_ram_gb=7.6, cpu_count=8, cpu_model="x", gpu=None, os="Linux"))
    assert small[0].name == "qwen2.5:3b-instruct" and "qwen2.5:7b-instruct" in _by_role(small, "avoid")
    tiny = recommend_models(Hardware(total_ram_gb=4.0, cpu_count=4, cpu_model="x", gpu=None, os="Linux"))
    assert tiny[0].name == "qwen2.5:1.5b-instruct"
    big = recommend_models(Hardware(total_ram_gb=32, cpu_count=16, cpu_model="x", gpu=None, os="Linux"))
    assert big[0].name == "qwen2.5:7b-instruct" and "qwen2.5:14b-instruct" in _by_role(big, "quality")
    gpu = recommend_models(Hardware(total_ram_gb=32, cpu_count=16, cpu_model="x", gpu="NVIDIA RTX 4090", os="Linux",
                                    gpu_vram_gb=24))
    assert "qwen2.5:32b-instruct" in _by_role(gpu, "quality")
    mac = recommend_models(Hardware(total_ram_gb=16, cpu_count=8, cpu_model="Apple M2", os="Darwin",
                                    gpu="Apple Silicon GPU (unified memory)", apple_silicon=True, arch="arm64"))
    assert mac[0].name == "qwen2.5:7b-instruct" and "GPU" in mac[0].why


def test_summary_mentions_key_facts():
    text = AERO_13.summary()
    assert "15.4 GB RAM" in text and "16 logical CPUs/8 cores" in text and "Ryzen 7 5800U" in text
    assert "no dedicated GPU" in text


def test_detect_hardware_on_this_machine_is_sane():
    hw = detect_hardware()
    assert hw.cpu_count >= 1 and hw.total_ram_gb > 0 and hw.os and hw.cpu_model
    assert recommend_models(hw)[0].name


def test_detect_hardware_nvidia(monkeypatch):
    def fake_run(cmd, timeout=5.0):
        return "NVIDIA GeForce RTX 3060, 12288" if cmd[0] == "nvidia-smi" else None

    monkeypatch.setattr(hardware, "_run", fake_run)
    monkeypatch.setattr(hardware.platform, "system", lambda: "Linux")
    hw = detect_hardware()
    assert hw.gpu == "NVIDIA GeForce RTX 3060" and hw.gpu_vram_gb == 12.0


def test_detect_hardware_never_raises_without_tools(monkeypatch):
    monkeypatch.setattr(hardware, "_run", lambda cmd, timeout=5.0: None)
    monkeypatch.setattr(hardware, "_read", lambda path: None)
    monkeypatch.setattr(hardware.platform, "system", lambda: "Linux")
    hw = detect_hardware()
    assert hw.gpu is None and hw.cpu_model


# --------------------------------------------------------------------------- #
# benchmark
# --------------------------------------------------------------------------- #
GOOD = {"title": "Business Analyst (Entry Level)", "seniority": "entry", "years_experience_required": 1,
        "required_skills": ["SQL", "Excel", "Tableau", "Kubernetes"], "preferred_skills": ["Python"],
        "sponsorship_statements": ["This position is not eligible for visa sponsorship."]}


def test_benchmark_measures_speed_and_validity():
    fake = FakeOllama(models=["qwen2.5:3b-instruct"]).queue(GOOD, GOOD)
    cfg = LLMSection(model="qwen2.5:3b-instruct")
    result = benchmark(fake.client(cfg), "qwen2.5:3b-instruct", runs=2, cold=True)
    assert ("POST", "/api/generate") in fake.calls  # cold start unloads first
    assert result.runs == 2 and result.json_valid_runs == 2 and result.json_valid
    assert result.load_ms == 2000.0
    assert result.tokens_per_s == 75.0 and result.prompt_tokens_per_s == 150.0
    assert result.grounded_skill_ratio == pytest.approx(0.8)  # Kubernetes is not in the posting
    assert result.est_job_analysis_seconds == pytest.approx(3000 / 150 + 500 / 75, rel=0.01)
    assert result.total_ms >= 0 and result.as_dict()["json_valid"] is True
    systems = [r["messages"][0]["content"] for r in fake.chat_requests]
    assert systems[0] != systems[1]  # prompt-cache busting between runs
    assert fake.chat_requests[0]["format"] == BenchmarkExtraction.model_json_schema()
    assert "Example Analytics Group" in fake.chat_requests[0]["messages"][1]["content"]
    assert "fictional" in BENCHMARK_POSTING


def test_benchmark_reports_invalid_json():
    fake = FakeOllama(models=["m"]).queue("not json")
    result = benchmark(fake.client(LLMSection(model="m")), "m")
    assert result.json_valid_runs == 0 and not result.json_valid and result.errors


def test_benchmark_unavailable():
    with pytest.raises(LLMUnavailable):
        benchmark(FakeOllama(down=True).client(LLMSection(max_retries=0)), "qwen2.5:7b-instruct")
