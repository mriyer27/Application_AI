"""CLI: ``job-agent llm check | recommend | benchmark | analyze``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import typer

if TYPE_CHECKING:  # pragma: no cover
    from job_agent.config import LLMSection
    from job_agent.context import AppContext


def register(app: typer.Typer) -> None:
    llm_app = typer.Typer(help="Local LLM (Ollama on localhost): check, recommend, benchmark, analyze.",
                          no_args_is_help=True)
    llm_app.command("check")(check)
    llm_app.command("recommend")(recommend)
    llm_app.command("benchmark")(benchmark_cmd)
    llm_app.command("analyze")(analyze)
    app.add_typer(llm_app, name="llm")


def _probe_config(ctx: "AppContext") -> "LLMSection":
    """The LLM config with ``enabled`` forced on: explicit CLI commands probe even if disabled."""
    return ctx.config.llm.model_copy(update={"enabled": True})


def check() -> None:
    """Check the Ollama server: reachable, version, pulled models, model that will be used."""
    from rich.table import Table

    from job_agent.cli import STATUS_STYLE, console, get_ctx
    from job_agent.llm.client import OllamaClient, match_model

    ctx = get_ctx()
    cfg = ctx.config.llm
    if not cfg.enabled:
        console.print("[yellow]llm.enabled is false:[/yellow] the agent will not use the local LLM "
                      "(probing the server anyway).")
    with OllamaClient(_probe_config(ctx)) as client:
        health = client.health()
        details = client.model_details() if health.reachable else []

    summary = Table(show_header=False)
    summary.add_column("Setting", style="bold")
    summary.add_column("Value", overflow="fold")
    summary.add_row("Server", cfg.base_url)
    summary.add_row("Reachable", "yes" if health.reachable else "[red]no[/red]")
    summary.add_row("Version", health.version or "unknown")
    summary.add_row("Configured model", cfg.model)
    summary.add_row("Fallback models", ", ".join(cfg.fallback_models) or "none")
    summary.add_row("Selected model", health.selected_model or "[red]none[/red]")
    summary.add_row("Status", f"[{STATUS_STYLE.get(health.status, 'white')}]{health.status.upper()}[/] {health.detail}")
    if health.action:
        summary.add_row("Next step", health.action)
    console.print(summary)

    if details:
        table = Table(title="Pulled models", header_style="bold")
        for col in ("Model", "Size (GB)", "Parameters", "Quantization", "Role"):
            table.add_column(col)
        configured = match_model(cfg.model, [d["name"] for d in details])
        fallbacks = {match_model(f, [d["name"] for d in details]) for f in cfg.fallback_models}
        for d in details:
            info = d.get("details") if isinstance(d.get("details"), dict) else {}
            size = d.get("size")
            role = "configured" if d["name"] == configured else ("fallback" if d["name"] in fallbacks else "")
            table.add_row(d["name"], f"{size / 1e9:.1f}" if isinstance(size, (int, float)) else "?",
                          str(info.get("parameter_size", "")), str(info.get("quantization_level", "")), role)
        console.print(table)
    if not health.reachable or health.selected_model is None:
        raise typer.Exit(1)


def recommend() -> None:
    """Show detected hardware, model recommendations, and why 'runs' is not 'reliable'."""
    from rich.table import Table

    from job_agent.cli import console, get_ctx
    from job_agent.llm.hardware import RUNS_VS_RELIABLE, detect_hardware, recommend_models

    ctx = get_ctx()
    hw = detect_hardware()
    recs = recommend_models(hw)
    console.print(f"[bold]Hardware:[/bold] {hw.summary()}")
    console.print(f"[bold]Configured model:[/bold] {ctx.config.llm.model}\n")
    table = Table(title="Recommended local models", header_style="bold")
    for col in ("Role", "Model", "Size (GB)", "Why"):
        table.add_column(col, overflow="fold")
    style = {"default": "green", "fast": "cyan", "quality": "magenta", "avoid": "red"}
    for r in recs:
        table.add_row(f"[{style.get(r.role, 'white')}]{r.role}[/]", r.name, f"{r.approx_size_gb:.1f}", r.why)
    console.print(table)
    if recs:
        console.print(f"\nPull the default: [bold]ollama pull {recs[0].name}[/bold]  "
                      f"(then set llm.model in config/config.yaml if different)\n")
    console.print(RUNS_VS_RELIABLE, markup=False, highlight=False)


def benchmark_cmd(
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Model to measure (default: selected model)."),
    runs: int = typer.Option(1, "--runs", min=1, max=10, help="Number of runs."),
    cold: bool = typer.Option(False, "--cold", help="Unload the model first to measure a cold start."),
) -> None:
    """Measure load time, tokens/s, total time and JSON validity on this machine."""
    from rich.table import Table

    from job_agent.cli import console, fail, get_ctx
    from job_agent.errors import JobAgentError
    from job_agent.llm.benchmark import TYPICAL_OUTPUT_TOKENS, TYPICAL_PROMPT_TOKENS, benchmark
    from job_agent.llm.client import OllamaClient

    ctx = get_ctx()
    with OllamaClient(_probe_config(ctx)) as client:
        try:
            name = model or client.resolve_model()
            console.print(f"Benchmarking [bold]{name}[/bold] ({runs} run(s)); on CPU this can take minutes...")
            result = benchmark(client, name, runs=runs, cold=cold)
        except JobAgentError as exc:
            fail(exc)
            return

    def fmt(value: float | None, unit: str = "") -> str:
        return "n/a" if value is None else f"{value:,.1f}{unit}"

    table = Table(show_header=False, title=f"Benchmark: {result.model}")
    table.add_column("Metric", style="bold")
    table.add_column("Value")
    table.add_row("Runs (JSON valid)", f"{result.json_valid_runs}/{result.runs}")
    table.add_row("Model load time", fmt(result.load_ms, " ms") + (" (cold)" if cold else " (≈0 if already loaded)"))
    table.add_row("Prompt processing", fmt(result.prompt_tokens_per_s, " tokens/s"))
    table.add_row("Generation", fmt(result.tokens_per_s, " tokens/s"))
    table.add_row("Average run", fmt(result.avg_run_ms, " ms"))
    table.add_row("Total", fmt(result.total_ms, " ms"))
    table.add_row("Grounded skills", "n/a" if result.grounded_skill_ratio is None
                  else f"{result.grounded_skill_ratio:.0%} of extracted skills appear in the posting")
    table.add_row(f"Est. job analysis (~{TYPICAL_PROMPT_TOKENS} in / {TYPICAL_OUTPUT_TOKENS} out tokens)",
                  fmt(result.est_job_analysis_seconds, " s"))
    console.print(table)
    for err in result.errors:
        console.print(f"[yellow]{err}[/yellow]")
    if result.json_valid_runs == 0:
        raise typer.Exit(1)


def analyze(
    job_uid: str = typer.Argument(..., help="Job id as shown by the jobs list, e.g. J-1a2b3c4d5e."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Ignore the cached analysis and ask the model again."),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Model to use (default: selected model)."),
) -> None:
    """Analyze one stored job with the local LLM and print the validated JSON."""
    from job_agent.cli import console, err_console, fail, get_ctx
    from job_agent.errors import JobAgentError
    from job_agent.llm.analysis import analyze_job_cached
    from job_agent.llm.client import OllamaClient
    from job_agent.llm.prompts import PROMPT_VERSION

    ctx = get_ctx()
    if not ctx.config.llm.enabled:
        err_console.print("[red]llm.enabled is false in the config;[/red] enable it to analyze jobs.")
        raise typer.Exit(2)
    row = ctx.db.query_one("SELECT * FROM jobs WHERE job_uid = ?", (job_uid,))
    if row is None:
        err_console.print(f"[red]No job with id {job_uid!r}.[/red]")
        raise typer.Exit(1)
    with OllamaClient(ctx.config.llm) as client:
        try:
            outcome = analyze_job_cached(ctx.db, client, {k: row[k] for k in row.keys()}, ctx.profile,
                                         use_cache=not no_cache, model=model)
        except JobAgentError as exc:
            fail(exc)
            return
    console.print_json(outcome.analysis.model_dump_json())
    how = "from cache" if outcome.cached else f"analyzed in {(outcome.duration_ms or 0) / 1000:.1f}s"
    console.print(f"[dim]model {outcome.analysis.model}; prompt version {PROMPT_VERSION}; {how}. "
                  "Skills and sponsorship statements were checked against the posting text.[/dim]")
