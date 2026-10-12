"""Application context: config + secrets + lazily loaded profile + database.

Create one ``AppContext`` per thread (the dashboard creates one per request).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from job_agent.config import AppConfig, Secrets, ensure_directories, load_config
from job_agent.database import Database
from job_agent.logging_config import setup_logging
from job_agent.profile import CandidateProfile, load_profile


@dataclass
class AppContext:
    config: AppConfig
    secrets: Secrets
    _db: Database | None = field(default=None, repr=False)
    _profile: CandidateProfile | None = field(default=None, repr=False)

    @classmethod
    def load(cls, config_path: str | Path | None = None, *, setup_logs: bool = True,
             console_level: str = "WARNING") -> "AppContext":
        cfg = load_config(config_path)
        ensure_directories(cfg)
        if setup_logs:
            setup_logging(cfg.logs_dir, cfg.logging.level, max_bytes=cfg.logging.max_bytes,
                          backup_count=cfg.logging.backup_count, console_level=console_level)
        return cls(config=cfg, secrets=Secrets.from_env())

    @classmethod
    def from_config(cls, cfg: AppConfig, secrets: Secrets | None = None) -> "AppContext":
        ensure_directories(cfg)
        return cls(config=cfg, secrets=secrets or Secrets.from_env())

    @property
    def db(self) -> Database:
        if self._db is None:
            self._db = Database(self.config.db_path, busy_timeout_ms=self.config.database.busy_timeout_ms)
        return self._db

    @property
    def profile(self) -> CandidateProfile:
        if self._profile is None:
            self._profile = load_profile(self.config.profile_path)
        return self._profile

    def reload_profile(self) -> CandidateProfile:
        self._profile = None
        return self.profile

    def fork(self) -> "AppContext":
        """A context sharing config/profile but with its own DB connection (for threads)."""
        return AppContext(config=self.config, secrets=self.secrets, _profile=self._profile)

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None
