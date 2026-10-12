"""Configuration loading and validation.

Resolution order for the config file:
  1. explicit ``--config`` path
  2. ``JOB_AGENT_CONFIG`` environment variable
  3. ``<home>/config/config.yaml``
  4. ``<home>/config/config.example.yaml`` (flagged as "using example config")

``<home>`` is ``JOB_AGENT_HOME`` or the current working directory. All relative
paths inside the config are resolved against ``<home>``.

Secrets never live in the YAML file. They come from environment variables or
``<home>/.env`` (see ``.env.example``) and are exposed only via ``Secrets``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from job_agent.errors import ConfigError
from job_agent.models import EmploymentType, Permission


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #
class AppSection(_Section):
    mode: Literal["supervised"] = "supervised"
    timezone: str = "America/New_York"
    data_dir: str = "data"
    reports_dir: str = "reports"
    documents_dir: str = "documents"
    logs_dir: str = "logs"
    profile_path: str = "profiles/candidate_profile.yaml"

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {v!r}") from exc
        return v


class DatabaseSection(_Section):
    path: str = "data/job_agent.db"
    busy_timeout_ms: int = Field(10_000, ge=100, le=120_000)


class LoggingSection(_Section):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    max_bytes: int = Field(5_000_000, ge=10_000)
    backup_count: int = Field(5, ge=1, le=50)


class SearchSection(_Section):
    target_titles: list[str] = Field(min_length=1)
    #: Extra phrases that make a title relevant even if not an exact target title.
    title_keywords: list[str] = Field(default_factory=list)
    #: Titles containing any of these (case-insensitive, word-boundary) are skipped.
    exclude_title_keywords: list[str] = Field(default_factory=list)
    #: Locations accepted. A job matches if its location contains any entry
    #: (case-insensitive). "Remote" matches remote listings.
    locations: list[str] = Field(default_factory=lambda: ["United States", "Remote"])
    remote_preference: Literal["remote_only", "remote_or_hybrid", "any"] = "any"
    employment_types: list[EmploymentType] = Field(
        default_factory=lambda: [EmploymentType.FULL_TIME, EmploymentType.UNKNOWN]
    )
    experience_levels: list[str] = Field(
        default_factory=lambda: ["entry", "new_grad", "associate", "early_career", "unknown"]
    )
    #: Jobs explicitly requiring more years than this are filtered out.
    max_years_experience: int = Field(3, ge=0, le=30)
    industries: list[str] = Field(default_factory=list)
    target_employers: list[str] = Field(default_factory=list)
    max_jobs_per_run: int = Field(300, ge=1, le=10_000)
    #: Skip postings older than this many days (when the source gives a date).
    max_posting_age_days: int = Field(45, ge=1, le=365)


class GreenhouseSource(_Section):
    enabled: bool = True
    boards: list[str] = Field(default_factory=list)


class LeverSource(_Section):
    enabled: bool = True
    companies: list[str] = Field(default_factory=list)


class AshbySource(_Section):
    enabled: bool = True
    boards: list[str] = Field(default_factory=list)


class SourcesSection(_Section):
    user_agent: str = "job-agent/0.1 (personal job search assistant)"
    request_timeout_seconds: float = Field(20, gt=0, le=120)
    min_request_interval_seconds: float = Field(1.0, ge=0.2, le=60)
    max_retries: int = Field(3, ge=0, le=6)
    greenhouse: GreenhouseSource = Field(default_factory=GreenhouseSource)
    lever: LeverSource = Field(default_factory=LeverSource)
    ashby: AshbySource = Field(default_factory=AshbySource)


class ScoringWeights(_Section):
    career_interest: float = 25
    skills: float = 25
    experience_level: float = 15
    domain_relevance: float = 10
    location: float = 10
    compensation: float = 10
    employer_growth: float = 5

    @model_validator(mode="after")
    def _positive(self) -> "ScoringWeights":
        values = self.model_dump()
        if any(v < 0 for v in values.values()):
            raise ValueError("scoring weights must be >= 0")
        if sum(values.values()) <= 0:
            raise ValueError("scoring weights must not all be zero")
        return self

    def normalized(self) -> dict[str, float]:
        """Weights rescaled so they sum to 100."""
        values = self.model_dump()
        total = sum(values.values())
        return {k: v * 100.0 / total for k, v in values.items()}


class ScoringSection(_Section):
    weights: ScoringWeights = Field(default_factory=ScoringWeights)
    shortlist_threshold: float = Field(70, ge=0, le=100)
    strong_threshold: float = Field(80, ge=0, le=100)
    possible_threshold: float = Field(60, ge=0, le=100)
    salary_target_base: float = Field(90_000, ge=0)
    use_llm: bool = True
    max_llm_jobs_per_run: int = Field(10, ge=0, le=500)

    @model_validator(mode="after")
    def _ordering(self) -> "ScoringSection":
        if not self.possible_threshold <= self.strong_threshold:
            raise ValueError("possible_threshold must be <= strong_threshold")
        return self


class LLMSection(_Section):
    enabled: bool = True
    provider: Literal["ollama"] = "ollama"
    base_url: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:7b-instruct"
    fallback_models: list[str] = Field(default_factory=lambda: ["qwen2.5:3b-instruct", "llama3.2:3b"])
    timeout_seconds: float = Field(180, gt=0, le=1800)
    max_retries: int = Field(2, ge=0, le=5)
    temperature: float = Field(0.1, ge=0, le=1.5)
    num_ctx: int = Field(8192, ge=1024, le=131_072)
    max_input_chars: int = Field(12_000, ge=1000, le=200_000)

    @field_validator("base_url")
    @classmethod
    def _local_only(cls, v: str) -> str:
        # Local-first: refuse non-local LLM endpoints so personal data is never
        # sent to a remote inference server by accident.
        from urllib.parse import urlparse

        host = (urlparse(v).hostname or "").lower()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("llm.base_url must point to localhost (local-first policy)")
        return v.rstrip("/")


class DocumentsSection(_Section):
    formats: list[Literal["docx", "pdf"]] = Field(default_factory=lambda: ["docx", "pdf"])
    generate_cover_letter: bool = True
    max_bullets_per_role: int = Field(5, ge=1, le=10)
    use_llm_rewording: bool = True


class ApplicationsSection(_Section):
    #: Human approval before final submission is mandatory; it cannot be disabled.
    require_approval: Literal[True] = True
    #: Whether an *approved* application may be submitted by the browser
    #: automation. Off by default: approved applications are then submitted by
    #: you manually using the prepared summary.
    browser_submission_enabled: bool = False
    headless: bool = True
    browser_timeout_seconds: float = Field(45, gt=0, le=600)
    max_concurrent_browsers: int = Field(1, ge=1, le=2)
    approval_expiry_hours: int = Field(72, ge=1, le=24 * 30)
    screenshots: bool = True


class GmailSection(_Section):
    enabled: bool = False
    credentials_path: str = "data/secrets/gmail_credentials.json"
    token_path: str = "data/secrets/gmail_token.json"
    scopes: list[str] = Field(default_factory=lambda: ["https://www.googleapis.com/auth/gmail.readonly"])
    query: str = "newer_than:14d -category:promotions -category:social"
    max_messages_per_sync: int = Field(100, ge=1, le=500)
    body_char_limit: int = Field(4000, ge=200, le=50_000)
    store_snippets: bool = True

    @field_validator("scopes")
    @classmethod
    def _scopes(cls, v: list[str]) -> list[str]:
        allowed = {
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.metadata",
        }
        bad = [s for s in v if s not in allowed]
        if bad:
            raise ValueError(
                f"unsupported Gmail scopes {bad}; this version only uses read-only scopes"
            )
        return v


class FollowupsSection(_Section):
    application_no_response_days: int = Field(8, ge=1, le=90)
    recruiter_reply_days: int = Field(2, ge=1, le=30)
    stale_application_days: int = Field(30, ge=7, le=365)
    interview_reminder_hours: int = Field(24, ge=1, le=168)
    max_followups_per_application: int = Field(2, ge=0, le=5)


class ReportingSection(_Section):
    top_matches: int = Field(15, ge=1, le=200)
    keep_days: int = Field(365, ge=1)


class DashboardSection(_Section):
    host: str = "127.0.0.1"
    port: int = Field(8765, ge=1024, le=65535)
    session_max_age_minutes: int = Field(720, ge=5, le=60 * 24 * 14)
    login_attempts_per_minute: int = Field(5, ge=1, le=60)
    #: Explicit opt-in required to bind to a non-loopback address (e.g. a
    #: Tailscale IP). Never bind to 0.0.0.0 on an untrusted network.
    allow_non_localhost: bool = False
    secure_cookies: bool = False  # set true when served over HTTPS (e.g. tailscale serve)

    @model_validator(mode="after")
    def _bind(self) -> "DashboardSection":
        if self.host not in {"127.0.0.1", "localhost", "::1"} and not self.allow_non_localhost:
            raise ValueError(
                "dashboard.host is not loopback; set dashboard.allow_non_localhost: true "
                "only if you bind to a private VPN address (see docs/remote-access.md)"
            )
        return self


class ScheduleEntry(_Section):
    enabled: bool = True
    #: Standard 5-field cron expression, interpreted in app.timezone.
    cron: str | None = None
    interval_minutes: int | None = Field(None, ge=5, le=24 * 60)

    @model_validator(mode="after")
    def _one(self) -> "ScheduleEntry":
        if (self.cron is None) == (self.interval_minutes is None):
            raise ValueError("each schedule needs exactly one of 'cron' or 'interval_minutes'")
        if self.cron is not None and len(self.cron.split()) != 5:
            raise ValueError(f"cron expression must have 5 fields: {self.cron!r}")
        return self


class SchedulerSection(_Section):
    enabled: bool = True
    tasks: dict[str, ScheduleEntry] = Field(
        default_factory=lambda: {
            "discover_and_score": ScheduleEntry(cron="15 7,16 * * *"),
            "email_sync": ScheduleEntry(interval_minutes=30, enabled=False),
            "followups": ScheduleEntry(cron="0 8 * * *"),
            "report": ScheduleEntry(cron="30 21 * * *"),
            "health": ScheduleEntry(interval_minutes=60),
        }
    )
    lock_timeout_minutes: int = Field(90, ge=5, le=24 * 60)
    max_retries: int = Field(2, ge=0, le=5)
    retry_backoff_seconds: int = Field(120, ge=5, le=3600)
    misfire_grace_minutes: int = Field(60, ge=1, le=24 * 60)


class PermissionsSection(_Section):
    read_jobs: bool = True
    generate_documents: bool = True
    prepare_applications: bool = True
    submit_applications: bool = True  # still always approval-gated
    read_emails: bool = True
    draft_emails: bool = True
    #: Sending email is not implemented in this version and must stay false.
    send_emails: Literal[False] = False
    change_config: bool = False

    def allows(self, permission: Permission) -> bool:
        return bool(getattr(self, permission.value))


# --------------------------------------------------------------------------- #
# Root
# --------------------------------------------------------------------------- #
class AppConfig(_Section):
    config_version: Literal[1] = 1
    app: AppSection = Field(default_factory=AppSection)
    database: DatabaseSection = Field(default_factory=DatabaseSection)
    logging: LoggingSection = Field(default_factory=LoggingSection)
    search: SearchSection
    sources: SourcesSection = Field(default_factory=SourcesSection)
    scoring: ScoringSection = Field(default_factory=ScoringSection)
    llm: LLMSection = Field(default_factory=LLMSection)
    documents: DocumentsSection = Field(default_factory=DocumentsSection)
    applications: ApplicationsSection = Field(default_factory=ApplicationsSection)
    gmail: GmailSection = Field(default_factory=GmailSection)
    followups: FollowupsSection = Field(default_factory=FollowupsSection)
    reporting: ReportingSection = Field(default_factory=ReportingSection)
    dashboard: DashboardSection = Field(default_factory=DashboardSection)
    scheduler: SchedulerSection = Field(default_factory=SchedulerSection)
    permissions: PermissionsSection = Field(default_factory=PermissionsSection)

    # Populated by load_config(); excluded from dumps / hashes.
    home: Path = Field(default=Path("."), exclude=True)
    source_path: Path | None = Field(default=None, exclude=True)
    using_example: bool = Field(default=False, exclude=True)

    # -- path helpers ------------------------------------------------------ #
    def resolve(self, p: str | Path) -> Path:
        path = Path(p).expanduser()
        return path if path.is_absolute() else (self.home / path)

    @property
    def data_dir(self) -> Path:
        return self.resolve(self.app.data_dir)

    @property
    def reports_dir(self) -> Path:
        return self.resolve(self.app.reports_dir)

    @property
    def documents_dir(self) -> Path:
        return self.resolve(self.app.documents_dir)

    @property
    def logs_dir(self) -> Path:
        return self.resolve(self.app.logs_dir)

    @property
    def db_path(self) -> Path:
        return self.resolve(self.database.path)

    @property
    def profile_path(self) -> Path:
        return self.resolve(self.app.profile_path)

    @property
    def secrets_dir(self) -> Path:
        return self.data_dir / "secrets"

    def safe_dict(self) -> dict[str, Any]:
        """Non-secret settings, safe to show in reports and the dashboard.

        The YAML never contains secrets, but paths are reduced to relative form
        to avoid leaking home-directory layout.
        """
        return self.model_dump(mode="json")

    def config_hash(self) -> str:
        blob = json.dumps(self.safe_dict(), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Secrets (environment only)
# --------------------------------------------------------------------------- #
class Secrets(BaseModel):
    """Secret values from the environment. Never log or serialize this object."""

    model_config = ConfigDict(frozen=True)

    dashboard_username: str | None = None
    dashboard_password_hash: str | None = None
    session_secret: str | None = None
    contact_email: str | None = None  # optional, appended to HTTP User-Agent

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return "Secrets(<redacted>)"

    __str__ = __repr__

    @classmethod
    def from_env(cls) -> "Secrets":
        return cls(
            dashboard_username=os.environ.get("JOB_AGENT_DASHBOARD_USERNAME") or None,
            dashboard_password_hash=os.environ.get("JOB_AGENT_DASHBOARD_PASSWORD_HASH") or None,
            session_secret=os.environ.get("JOB_AGENT_SESSION_SECRET") or None,
            contact_email=os.environ.get("JOB_AGENT_CONTACT_EMAIL") or None,
        )


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def resolve_home() -> Path:
    env = os.environ.get("JOB_AGENT_HOME")
    return Path(env).expanduser().resolve() if env else Path.cwd().resolve()


def load_env_file(home: Path) -> None:
    """Load ``<home>/.env`` without overriding variables already set."""
    env_path = home / ".env"
    if env_path.is_file():
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)


def find_config_path(explicit: str | Path | None, home: Path) -> tuple[Path, bool]:
    if explicit:
        p = Path(explicit).expanduser()
        p = p if p.is_absolute() else (Path.cwd() / p)
        if not p.is_file():
            raise ConfigError(f"config file not found: {p}")
        return p.resolve(), p.name.endswith(".example.yaml")
    env = os.environ.get("JOB_AGENT_CONFIG")
    if env:
        p = Path(env).expanduser()
        p = p if p.is_absolute() else (home / p)
        if not p.is_file():
            raise ConfigError(f"JOB_AGENT_CONFIG points to a missing file: {p}")
        return p.resolve(), p.name.endswith(".example.yaml")
    real = home / "config" / "config.yaml"
    if real.is_file():
        return real, False
    example = home / "config" / "config.example.yaml"
    if example.is_file():
        return example, True
    raise ConfigError(
        f"no configuration found under {home / 'config'}; run `job-agent init` "
        "or copy config/config.example.yaml to config/config.yaml"
    )


_SECRET_KEY_RE = re.compile(r"(password|secret|token|api[_-]?key|private[_-]?key)", re.I)


def _reject_secrets_in_yaml(data: Any, path: str = "") -> None:
    """Secrets belong in the environment, never in the YAML config."""
    if isinstance(data, dict):
        for k, v in data.items():
            key_path = f"{path}.{k}" if path else str(k)
            if _SECRET_KEY_RE.search(str(k)) and isinstance(v, str) and v.strip():
                if not str(k).endswith("_path"):
                    raise ConfigError(
                        f"config key {key_path!r} looks like a secret; put secrets in .env instead"
                    )
            _reject_secrets_in_yaml(v, key_path)
    elif isinstance(data, list):
        for i, v in enumerate(data):
            _reject_secrets_in_yaml(v, f"{path}[{i}]")


def parse_config(data: dict[str, Any], *, home: Path, source_path: Path | None = None,
                 using_example: bool = False) -> AppConfig:
    if not isinstance(data, dict):
        raise ConfigError("configuration root must be a mapping")
    _reject_secrets_in_yaml(data)
    try:
        cfg = AppConfig.model_validate(data)
    except ValidationError as exc:
        lines = []
        for err in exc.errors():
            loc = ".".join(str(x) for x in err["loc"])
            lines.append(f"  - {loc}: {err['msg']}")
        raise ConfigError("invalid configuration:\n" + "\n".join(lines)) from None
    cfg.home = home
    cfg.source_path = source_path
    cfg.using_example = using_example
    return cfg


def load_config(path: str | Path | None = None, *, home: Path | None = None) -> AppConfig:
    home = home or resolve_home()
    load_env_file(home)
    cfg_path, using_example = find_config_path(path, home)
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"config file {cfg_path} is not valid YAML: {exc}") from None
    return parse_config(data, home=home, source_path=cfg_path, using_example=using_example)


def ensure_directories(cfg: AppConfig) -> list[Path]:
    """Create runtime directories (data, reports, documents, logs, secrets)."""
    created = []
    for d in (cfg.data_dir, cfg.reports_dir, cfg.documents_dir, cfg.logs_dir, cfg.secrets_dir,
              cfg.db_path.parent):
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created.append(d)
    try:
        os.chmod(cfg.secrets_dir, 0o700)
    except OSError:  # pragma: no cover - e.g. Windows
        pass
    return created
