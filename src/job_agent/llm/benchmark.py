"""Measure a local model on THIS machine: load time, speed, JSON validity, grounding.

The benchmark uses a fixed, synthetic (fictional) job posting and a small
extraction schema, sent the same way real analyses are (structured outputs,
``llm.num_ctx``). It does not use the corrective re-ask, so ``json_valid_runs``
reflects the model's raw reliability. ``grounded_skill_ratio`` is the share of
extracted skills that literally appear in the posting (1.0 = no invented
skills) -- a cheap proxy for hallucination rate.

Runs that needed a retry are reported and excluded from the timing averages (a
retried request can reuse the timed-out request's prompt cache).
``est_job_analysis_seconds`` includes the measured server overhead
(``overhead_factor``, e.g. schema-constrained sampling on CPU).
"""

from __future__ import annotations

import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from job_agent.llm import prompts
from job_agent.llm.analysis import find_term
from job_agent.errors import JobAgentError
from job_agent.llm.client import OllamaClient, OutputValidationError, parse_model
from job_agent.llm.errors import LLMUnavailable
from job_agent.security.untrusted import wrap_untrusted

#: Assumed size of a typical job-analysis request, used for the time estimate.
TYPICAL_PROMPT_TOKENS = 3000
TYPICAL_OUTPUT_TOKENS = 500

BENCHMARK_POSTING = """\
Title: Business Analyst (Entry Level)
Employer: Example Analytics Group (fictional)
Location: Remote, United States

Example Analytics Group helps regional retailers understand their customers. We are hiring an entry-level
Business Analyst to join our client insights team.

What you will do:
- Gather requirements from business stakeholders and translate them into clear user stories.
- Build and maintain dashboards in Tableau and Power BI for weekly sales reviews.
- Write SQL queries against our Snowflake warehouse to answer ad-hoc questions.
- Document processes in Confluence and track work in Jira.

What we are looking for:
- Bachelor's degree in Information Systems, Business, Statistics or a related field.
- 1+ years of experience with SQL and Excel (internships count).
- Strong written communication and stakeholder management skills.
- Nice to have: Python, experience with A/B testing, exposure to machine learning concepts.

This position is not eligible for visa sponsorship. Applicants must be authorized to work in the United States.
"""

_BENCH_SYSTEM = prompts.JOB_ANALYSIS_SYSTEM


class BenchmarkExtraction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    seniority: Literal["intern", "entry", "mid", "senior", "unknown"]
    years_experience_required: int | None
    required_skills: list[str]
    preferred_skills: list[str]
    sponsorship_statements: list[str]


@dataclass
class BenchmarkResult:
    model: str
    runs: int
    json_valid_runs: int = 0
    load_ms: float | None = None  # from the first run (≈0 if the model was already in memory)
    prompt_tokens_per_s: float | None = None
    tokens_per_s: float | None = None
    total_ms: float = 0.0  # wall-clock for all runs
    avg_run_ms: float | None = None
    grounded_skill_ratio: float | None = None
    #: server time / (prompt + generation time); > 1 = overhead such as schema-constrained sampling
    overhead_factor: float | None = None
    est_job_analysis_seconds: float | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def json_valid(self) -> bool:
        return self.runs > 0 and self.json_valid_runs == self.runs

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "json_valid": self.json_valid}


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def benchmark(client: OllamaClient, model: str, *, runs: int = 1, cold: bool = False) -> BenchmarkResult:
    """Run the benchmark ``runs`` times. Raises ``LLMUnavailable`` if the server/model is unavailable.

    ``cold=True`` first asks Ollama to unload the model so ``load_ms`` measures a cold start.
    """
    runs = max(1, int(runs))
    result = BenchmarkResult(model=model, runs=runs)
    if cold:
        client.unload(model)
    wrapped = wrap_untrusted(BENCHMARK_POSTING, label="JOB_POSTING", max_chars=client.config.max_input_chars)
    user = (f"Extract the requested fields from the job posting below.\n\n{wrapped}\n\n"
            'Return {"title", "seniority", "years_experience_required", "required_skills", "preferred_skills", '
            '"sponsorship_statements"} as one JSON object.')
    fmt = BenchmarkExtraction.model_json_schema()
    prompt_rates: list[float] = []
    gen_rates: list[float] = []
    overheads: list[float] = []
    grounded = total_skills = 0
    started_all = time.monotonic()
    for i in range(runs):
        # A unique first line defeats Ollama's prompt-prefix cache so every run measures prompt processing.
        system = f"Benchmark run {i + 1} ({secrets.token_hex(4)}).\n{_BENCH_SYSTEM}"
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        try:
            chat = client.chat(messages, model=model, format=fmt, max_tokens=400)
        except LLMUnavailable:
            raise
        except JobAgentError as exc:  # e.g. rejected request: reported in the result, benchmark continues
            result.errors.append(f"run {i + 1}: {type(exc).__name__}: {exc}")
            continue
        if chat.attempts > 1:
            # A retried request can reuse the prompt cache of the timed-out one: its timings are not comparable.
            result.errors.append(f"run {i + 1}: needed {chat.attempts} attempts (timeout or transient error); "
                                 "timings excluded -- raise llm.timeout_seconds and re-run")
        else:
            if i == 0:
                result.load_ms = round(chat.load_ms, 1) if chat.load_ms is not None else None
            if chat.prompt_tokens_per_second:
                prompt_rates.append(chat.prompt_tokens_per_second)
            if chat.tokens_per_second:
                gen_rates.append(chat.tokens_per_second)
            busy = (chat.prompt_eval_ms or 0) + (chat.eval_ms or 0)
            if chat.total_ms and busy:
                overheads.append(max(1.0, (chat.total_ms - (chat.load_ms or 0)) / busy))
        try:
            parsed = parse_model(chat.content, BenchmarkExtraction)
        except OutputValidationError as exc:
            result.errors.append(f"run {i + 1}: invalid JSON ({'; '.join(exc.problems[:3])})")
            continue
        result.json_valid_runs += 1
        skills = [*parsed.required_skills, *parsed.preferred_skills]
        total_skills += len(skills)
        grounded += sum(1 for s in skills if find_term(s, BENCHMARK_POSTING))
    result.total_ms = round((time.monotonic() - started_all) * 1000, 1)
    result.avg_run_ms = round(result.total_ms / runs, 1)
    result.prompt_tokens_per_s = _mean(prompt_rates)
    result.tokens_per_s = _mean(gen_rates)
    result.grounded_skill_ratio = round(grounded / total_skills, 3) if total_skills else None
    result.overhead_factor = _mean(overheads)
    if result.prompt_tokens_per_s and result.tokens_per_s:
        result.est_job_analysis_seconds = round(
            (TYPICAL_PROMPT_TOKENS / result.prompt_tokens_per_s + TYPICAL_OUTPUT_TOKENS / result.tokens_per_s)
            * (result.overhead_factor or 1.0), 1
        )
    return result
