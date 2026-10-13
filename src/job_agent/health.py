"""System health checks (``job-agent doctor``, startup report, dashboard)."""

from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Callable

from job_agent.database.connection import from_json, to_json
from job_agent.errors import JobAgentError

if TYPE_CHECKING:  # pragma: no cover
    from job_agent.context import AppContext
    from job_agent.tasks import RunInfo

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    action: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _check_config(ctx: "AppContext") -> Check:
    cfg = ctx.config
    if cfg.using_example:
        return Check("config", WARN, f"using example config {cfg.source_path.name if cfg.source_path else ''}",
                     "Run `job-agent init` to create config/config.yaml, then edit it.")
    return Check("config", OK, f"loaded {cfg.source_path.name if cfg.source_path else 'config'}"
                 f" (mode={cfg.app.mode}, hash={cfg.config_hash()})")


def _check_dirs(ctx: "AppContext") -> Check:
    cfg = ctx.config
    problems = []
    for label, d in (("data", cfg.data_dir), ("reports", cfg.reports_dir), ("documents", cfg.documents_dir),
                     ("logs", cfg.logs_dir)):
        try:
            d.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=d, prefix=".write-test-", delete=True):
                pass
        except OSError as exc:
            problems.append(f"{label}: {exc.strerror or exc}")
    if problems:
        return Check("directories", FAIL, "; ".join(problems), "Fix directory permissions.")
    free = shutil.disk_usage(cfg.data_dir).free / 1e9
    if free < 2:
        return Check("directories", WARN, f"writable; only {free:.1f} GB free", "Free disk space.")
    return Check("directories", OK, f"writable; {free:.0f} GB free")


def _check_database(ctx: "AppContext") -> Check:
    try:
        db = ctx.db
        result = db.integrity_check()
        version = db.scalar("SELECT MAX(version) FROM schema_migrations")
        jobs = db.scalar("SELECT COUNT(*) FROM jobs")
    except JobAgentError as exc:
        return Check("database", FAIL, str(exc), "Check the database path / restore from backup.")
    if result != "ok":
        return Check("database", FAIL, f"integrity check: {result}", "Restore from backup (docs/troubleshooting.md).")
    return Check("database", OK, f"schema v{version}, {jobs} jobs, integrity ok")


def _check_profile(ctx: "AppContext") -> list[Check]:
    try:
        profile = ctx.profile
    except JobAgentError as exc:
        return [Check("profile", FAIL, str(exc).splitlines()[0], exc.human_action)]
    checks = []
    placeholders = profile.placeholders()
    if placeholders:
        checks.append(Check("profile", WARN, f"{len(placeholders)} placeholder value(s) remain, e.g. "
                            f"{', '.join(placeholders[:3])}", "Fill in your details in the profile YAML."))
    else:
        checks.append(Check("profile", OK, f"{len(profile.all_facts())} verified facts, {len(profile.skills)} skills"))
    unknown = profile.unknown_work_authorization_fields()
    if unknown:
        checks.append(Check("work_authorization", WARN,
                            f"{len(unknown)} field(s) unknown ({', '.join(unknown[:4])}...); every legal/eligibility "
                            "question will be routed to you and never auto-answered",
                            "Review work_authorization in your profile (optional, but needed to prepare forms)."))
    else:
        checks.append(Check("work_authorization", OK,
                            f"all fields set (last verified: {profile.work_authorization.last_verified or 'unknown'});"
                            " legal declarations still require your approval"))
    try:
        from job_agent.database import repo

        repo.record_profile_meta(ctx.db, profile_hash=profile.profile_hash(), schema_version=profile.schema_version,
                                 fact_count=len(profile.all_facts()), skill_count=len(profile.skills),
                                 placeholders=len(placeholders))
    except JobAgentError:
        pass  # database failure is reported by its own check
    return checks


def _check_secrets(ctx: "AppContext") -> Check:
    s = ctx.secrets
    missing = [n for n, v in (("JOB_AGENT_DASHBOARD_USERNAME", s.dashboard_username),
                              ("JOB_AGENT_DASHBOARD_PASSWORD_HASH", s.dashboard_password_hash),
                              ("JOB_AGENT_SESSION_SECRET", s.session_secret)) if not v]
    env_file = ctx.config.home / ".env"
    if env_file.exists() and os.name == "posix" and (env_file.stat().st_mode & 0o077):
        return Check("secrets", WARN, ".env is readable by other users", "Run: chmod 600 .env")
    if missing:
        return Check("secrets", WARN, f"dashboard login not configured ({', '.join(missing)})",
                     "Run `job-agent dashboard set-password` before starting the dashboard.")
    return Check("secrets", OK, "dashboard credentials configured (values not shown)")


def _check_ollama(ctx: "AppContext") -> Check:
    cfg = ctx.config.llm
    if not cfg.enabled:
        return Check("ollama", INFO, "local LLM disabled in config; rules-based fallback only")
    try:
        from job_agent.llm.client import OllamaClient  # provided by the llm module

        with OllamaClient(cfg) as client:
            status = client.health()
        return Check("ollama", status.status, status.detail, status.action)
    except ImportError:
        pass
    import httpx

    try:
        resp = httpx.get(f"{cfg.base_url}/api/tags", timeout=3)
        resp.raise_for_status()
        names = {m.get("name", "") for m in resp.json().get("models", [])}
    except (httpx.HTTPError, ValueError) as exc:
        return Check("ollama", WARN, f"Ollama not reachable at {cfg.base_url} ({type(exc).__name__}); "
                     "AI features fall back to rules", "Install/start Ollama: see docs/setup.md")
    if cfg.model in names or f"{cfg.model}:latest" in names:
        return Check("ollama", OK, f"Ollama running; model {cfg.model} available")
    fallback = [m for m in cfg.fallback_models if m in names]
    if fallback:
        return Check("ollama", WARN, f"model {cfg.model} missing; will use fallback {fallback[0]}",
                     f"ollama pull {cfg.model}")
    return Check("ollama", WARN, f"Ollama running but model {cfg.model} is not pulled",
                 f"ollama pull {cfg.model}")


def _check_gmail(ctx: "AppContext") -> Check:
    g = ctx.config.gmail
    from job_agent.database import repo

    if not g.enabled:
        return Check("gmail", INFO, "Gmail integration disabled (gmail.enabled: false)")
    creds = ctx.config.resolve(g.credentials_path)
    token = ctx.config.resolve(g.token_path)
    if not creds.exists():
        return Check("gmail", WARN, "OAuth client file missing; Gmail NOT connected",
                     "Follow docs/gmail-setup.md, then run `job-agent gmail auth`.")
    if not token.exists():
        return Check("gmail", WARN, "not authorized yet; Gmail NOT connected", "Run `job-agent gmail auth`.")
    last_ok = repo.get_state(ctx.db, "gmail_last_success_at")
    auth_error = repo.get_state(ctx.db, "gmail_auth_error")
    if auth_error:
        return Check("gmail", FAIL, f"authorization problem: {auth_error}", "Run `job-agent gmail auth` again.")
    if last_ok:
        return Check("gmail", OK, f"token present; last successful sync {last_ok}")
    return Check("gmail", WARN, "token present but no successful sync yet", "Run `job-agent gmail sync`.")


def _check_browser(ctx: "AppContext", *, deep: bool = False) -> Check:
    if importlib.util.find_spec("playwright") is None:
        return Check("browser", INFO, "Playwright not installed; application form preparation unavailable",
                     'pip install -e ".[browser]" && python -m playwright install chromium')
    mode = ("; browser submission disabled (approved applications are submitted by you)"
            if not ctx.config.applications.browser_submission_enabled else
            "; browser submission enabled for approved applications only")
    if deep:
        try:
            from job_agent.browser.session import chromium_launchable

            ok, detail = chromium_launchable(headless=True)
        except ImportError as exc:
            return Check("browser", WARN, f"browser module unavailable: {exc}")
        if not ok:
            return Check("browser", WARN, f"Chromium does not start: {detail}",
                         "python -m playwright install chromium")
        return Check("browser", OK, "Chromium starts" + mode)
    return Check("browser", OK, "Playwright installed" + mode)


def _check_permissions(ctx: "AppContext") -> Check:
    if os.name != "posix":
        return Check("permissions", INFO, "Windows: keep the project inside your user folder and outside "
                     "OneDrive-synced folders (data, documents and reports are private to your account)")
    cfg = ctx.config
    loose = []
    for path in (cfg.data_dir, cfg.logs_dir, cfg.reports_dir, cfg.documents_dir, cfg.db_path, cfg.home / ".env"):
        try:
            if path.exists() and path.stat().st_mode & 0o077:
                loose.append(str(path.relative_to(cfg.home)) if path.is_relative_to(cfg.home) else str(path))
        except OSError:
            continue
    if loose:
        return Check("permissions", WARN, f"readable by other users: {', '.join(loose)}",
                     "chmod -R go-rwx " + " ".join(loose))
    return Check("permissions", OK, "data, logs, reports, documents and .env are private to you")


def _check_scheduler(ctx: "AppContext") -> list[Check]:
    from job_agent.database import repo
    from job_agent.utils.time import parse_iso, utcnow

    db = ctx.db
    checks = []
    paused = repo.is_paused(db)
    heartbeat = parse_iso(repo.get_state(db, "daemon_heartbeat_at"))
    running = heartbeat is not None and (utcnow() - heartbeat).total_seconds() < 180
    state = "paused" if paused else ("running" if running else "not running")
    nxt = db.query_one("SELECT task_name, next_run_at FROM scheduled_tasks WHERE next_run_at IS NOT NULL "
                       "AND enabled = 1 ORDER BY next_run_at LIMIT 1")
    last = repo.last_successful_run(db)
    detail = f"daemon {state}"
    if last:
        detail += f"; last successful run: {last['task_name']} at {last['finished_at']}"
    else:
        detail += "; no successful runs yet"
    if nxt and running:
        detail += f"; next: {nxt['task_name']} at {nxt['next_run_at']}"
    status = WARN if paused else (OK if running else INFO)
    action = "Run `job-agent resume`." if paused else (None if running else "Start it with `job-agent daemon`.")
    checks.append(Check("scheduler", status, detail, action))

    pending = len(repo.pending_approvals(db))  # expires stale approvals first
    errors = db.scalar("SELECT COUNT(*) FROM errors WHERE resolved = 0 AND requires_human_action = 1")
    checks.append(Check("approvals", WARN if pending else OK, f"{pending} pending approval(s)",
                        "Run `job-agent approvals`." if pending else None))
    checks.append(Check("errors", WARN if errors else OK, f"{errors} unresolved error(s) needing attention",
                        "See `job-agent status` or the dashboard." if errors else None))
    return checks


def _check_hardware(ctx: "AppContext") -> Check:
    try:
        from job_agent.llm.hardware import detect_hardware, recommend_models  # provided by the llm module

        hw = detect_hardware()
        recs = recommend_models(hw)
        return Check("hardware", INFO, f"{hw.summary()}; recommended model: {recs[0].name if recs else 'n/a'}")
    except ImportError:
        cpus = os.cpu_count() or 0
        return Check("hardware", INFO, f"{cpus} logical CPUs")


def run_checks(ctx: "AppContext", *, deep: bool = False) -> list[Check]:
    """``deep`` (used by ``job-agent doctor``) also launches Chromium (~1 s)."""
    checks: list[Check] = []
    runners: list[Callable[["AppContext"], Check | list[Check]]] = [
        _check_config, _check_dirs, _check_database, _check_permissions, _check_profile, _check_secrets,
        _check_ollama, _check_gmail, lambda c: _check_browser(c, deep=deep), _check_scheduler, _check_hardware,
    ]
    for fn in runners:
        try:
            result = fn(ctx)
        except Exception as exc:  # a broken check must not hide the others
            name = getattr(fn, "__name__", "check").removeprefix("_check_")
            result = Check(name, FAIL, f"check crashed: {type(exc).__name__}: {exc}")
        checks.extend(result if isinstance(result, list) else [result])
    return checks


def overall(checks: list[Check]) -> str:
    if any(c.status == FAIL for c in checks):
        return FAIL
    if any(c.status == WARN for c in checks):
        return WARN
    return OK


def health_task(ctx: "AppContext", run: "RunInfo") -> dict[str, Any]:
    from job_agent.database import repo

    checks = run_checks(ctx)
    summary = {"overall": overall(checks), "checks": [c.as_dict() for c in checks]}
    repo.set_state(ctx.db, "last_health", to_json(summary))
    return {"overall": summary["overall"], "failed": [c.name for c in checks if c.status == FAIL],
            "warnings": [c.name for c in checks if c.status == WARN]}


def last_health(ctx: "AppContext") -> dict[str, Any] | None:
    from job_agent.database import repo

    return from_json(repo.get_state(ctx.db, "last_health"))
