"""Discovery service: fetch -> prefilter -> filter -> dedupe -> persist, per source identifier.

One failing source never stops the others: its error is recorded with
``repo.record_error`` (component ``discovery:<source>:<identifier>``) and the
run is reported as ``partial``.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from job_agent.database import repo
from job_agent.discovery import repository as jobs_repo
from job_agent.discovery.filters import evaluate_filters, title_is_relevant
from job_agent.discovery.http import PoliteHttpClient
from job_agent.discovery.sources import (
    ALL_SOURCES,
    MOCK_SOURCES,
    REAL_SOURCES,
    configured_identifiers,
    create_adapter,
    source_enabled,
)
from job_agent.errors import JobAgentError, PermanentError, RecoverableError
from job_agent.logging_config import get_logger
from job_agent.models import ErrorKind, Permission
from job_agent.security.permissions import require_permission
from job_agent.utils.time import to_iso, utcnow, utcnow_iso

if TYPE_CHECKING:  # pragma: no cover
    from job_agent.context import AppContext
    from job_agent.tasks import RunInfo

log = get_logger(__name__)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_COUNTERS = ("fetched", "invalid", "relevant", "new", "updated", "unchanged", "duplicates", "filtered", "closed",
             "reopened", "reposts", "deferred", "job_errors")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRunStats(_Model):
    source: str
    identifier: str
    is_mock: bool = False
    #: valid listings returned by the source
    fetched: int = 0
    #: listings skipped because they could not be normalized (e.g. no id / title)
    invalid: int = 0
    #: listings that passed the title prefilter (only these are stored)
    relevant: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    #: same listing twice in one fetch, or the same position from another source
    duplicates: int = 0
    #: new/changed listings stored as filtered_out (subset of new + updated)
    filtered: int = 0
    closed: int = 0
    reopened: int = 0
    reposts: int = 0
    #: new/changed listings not written because the run limit was reached
    deferred: int = 0
    job_errors: int = 0
    complete: bool = False
    skipped: str | None = None
    error: str | None = None
    error_kind: str | None = None
    filter_reasons: dict[str, int] = Field(default_factory=dict)
    invalid_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.identifier}"

    @property
    def ok(self) -> bool:
        return self.error is None


class DiscoveryTotals(_Model):
    fetched: int = 0
    invalid: int = 0
    relevant: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    duplicates: int = 0
    filtered: int = 0
    closed: int = 0
    reopened: int = 0
    reposts: int = 0
    deferred: int = 0
    job_errors: int = 0


class DiscoveryResult(_Model):
    started_at: str
    finished_at: str
    limit: int
    limit_reached: bool = False
    partial: bool = False
    sources: list[SourceRunStats] = Field(default_factory=list)
    totals: DiscoveryTotals = Field(default_factory=DiscoveryTotals)
    failed_sources: list[str] = Field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """Compact JSON-safe summary for task runs and audit records."""
        return {
            "partial": self.partial,
            "finished_at": self.finished_at,
            "limit": self.limit,
            "limit_reached": self.limit_reached,
            "totals": self.totals.model_dump(),
            "failed_sources": self.failed_sources,
            "sources": [
                {"source": s.key, "is_mock": s.is_mock, "fetched": s.fetched, "relevant": s.relevant, "new": s.new,
                 "updated": s.updated, "unchanged": s.unchanged, "duplicates": s.duplicates,
                 "filtered": s.filtered, "closed": s.closed, "deferred": s.deferred, "complete": s.complete,
                 "skipped": s.skipped, "error": s.error}
                for s in self.sources
            ],
        }


# --------------------------------------------------------------------------- #
# Target resolution
# --------------------------------------------------------------------------- #
def resolve_targets(config: Any, sources: list[str] | None = None) -> list[tuple[str, str]]:
    """``(source, identifier)`` pairs. Default: every enabled source's configured identifiers.

    Entries may be ``"greenhouse"`` (all configured greenhouse boards, even if the
    source is disabled -- naming it is explicit) or ``"greenhouse:capco"`` (one
    identifier, configured or not). The mock source needs ``"fixture:<file.json>"``.
    """
    targets: list[tuple[str, str]] = []
    if not sources:
        for name in REAL_SOURCES:
            if source_enabled(config, name):
                targets.extend((name, ident) for ident in configured_identifiers(config, name))
    else:
        for entry in sources:
            name, _, ident = str(entry).strip().partition(":")
            name, ident = name.strip().lower(), ident.strip()
            if name not in ALL_SOURCES:
                raise PermanentError(f"unknown job source {name!r}; known sources: {', '.join(ALL_SOURCES)}")
            if ident:
                targets.append((name, ident))
            elif name in MOCK_SOURCES:
                raise PermanentError("the mock fixture source needs a file: fixture:<path/to/file.json>")
            else:
                idents = configured_identifiers(config, name)
                if not idents:
                    raise PermanentError(f"no identifiers configured for {name} (sources.{name} in config)")
                targets.extend((name, i) for i in idents)
    out: list[tuple[str, str]] = []
    for name, ident in targets:
        if name in REAL_SOURCES and not _IDENTIFIER_RE.match(ident):
            raise PermanentError(f"invalid {name} identifier {ident!r} (expected a board token / company slug)")
        if (name, ident) not in out:
            out.append((name, ident))
    return out


def _make_http(ctx: "AppContext", http_client: PoliteHttpClient | httpx.Client | None) -> tuple[PoliteHttpClient, bool]:
    if isinstance(http_client, PoliteHttpClient):
        return http_client, False
    client = PoliteHttpClient(ctx.config.sources, contact_email=ctx.secrets.contact_email, client=http_client)
    return client, True


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def discover_jobs(
    ctx: "AppContext",
    *,
    sources: list[str] | None = None,
    limit: int | None = None,
    http_client: PoliteHttpClient | httpx.Client | None = None,
    actor: str = "system",
    run: "RunInfo | None" = None,
    now: datetime | None = None,
) -> DiscoveryResult:
    """Discover jobs from public job boards and persist them.

    ``limit`` caps new + updated listings *that pass the filters* written in this run (default
    ``search.max_jobs_per_run``); once reached, remaining sources are skipped and
    further new/changed listings are deferred to the next run.
    """
    config = ctx.config
    require_permission(config, Permission.READ_JOBS)
    targets = resolve_targets(config, sources)
    max_writes = config.search.max_jobs_per_run if limit is None else int(limit)
    if max_writes < 1:
        raise PermanentError("limit must be at least 1")
    started_at = utcnow_iso()
    db = ctx.db
    http, owns_http = _make_http(ctx, http_client)
    stats: list[SourceRunStats] = []
    remaining = max_writes
    try:
        for name, ident in targets:
            st = SourceRunStats(source=name, identifier=ident, is_mock=name in MOCK_SOURCES)
            stats.append(st)
            if remaining <= 0:
                st.skipped = "run limit reached"
                continue
            remaining = _run_source(ctx, name, ident, http, st, remaining, run, now)
    finally:
        if owns_http:
            http.close()

    totals = DiscoveryTotals(**{c: sum(getattr(s, c) for s in stats) for c in _COUNTERS})
    failed = [s.key for s in stats if s.error]
    result = DiscoveryResult(
        started_at=started_at, finished_at=utcnow_iso(), limit=max_writes,
        limit_reached=remaining <= 0 or totals.deferred > 0, partial=bool(failed),
        sources=stats, totals=totals, failed_sources=failed,
    )
    repo.audit(db, actor=actor, action="discover_jobs", outcome="partial" if failed else "success",
               entity_type="discovery_run", entity_id=run.run_uid if run else None,
               details=result.summary(), run_id=run.run_id if run else None)
    log.info("discovery finished: %s", result.totals.model_dump())
    return result


def _run_source(ctx: "AppContext", name: str, ident: str, http: PoliteHttpClient, st: SourceRunStats,
                remaining: int, run: "RunInfo | None", now: datetime | None) -> int:
    db = ctx.db
    search = ctx.config.search
    now_dt = now or utcnow()
    now_iso = to_iso(now_dt) or utcnow_iso()
    source_id: int | None = None
    try:
        source_id = jobs_repo.ensure_source(db, name, ident, is_mock=name in MOCK_SOURCES, now=now_iso)
        adapter = create_adapter(name, http=http, config=ctx.config)
        fetch = adapter.fetch(ident)
        st.fetched, st.invalid, st.complete = len(fetch), fetch.invalid, fetch.complete
        st.invalid_reasons = list(fetch.invalid_reasons)
        st.warnings.extend(fetch.warnings)

        if fetch.complete:
            if not fetch.seen_external_ids and jobs_repo.count_active(db, name, ident) > 0:
                st.warnings.append("source returned no listings; not closing stored listings as a precaution")
            else:
                st.closed = len(jobs_repo.close_missing_jobs(db, name, ident, fetch.seen_external_ids, now_iso))
        else:
            st.warnings.append("fetch incomplete; closed listings are not inferred this run")

        reasons: Counter[str] = Counter()
        seen_in_run: set[str] = set()
        for job in fetch:
            if job.external_id in seen_in_run:
                st.duplicates += 1
                continue
            seen_in_run.add(job.external_id)
            relevant = title_is_relevant(job.title, search)
            if relevant:
                st.relevant += 1
            elif jobs_repo.get_job_by_key(db, name, ident, job.external_id) is None:
                continue  # counted in fetched - relevant; irrelevant listings are not stored
            decision = evaluate_filters(job, search, now=now_dt, title_relevant=relevant)
            try:
                # Only listings that pass the filters count toward the run limit;
                # filtered-out rows are cheap and must not crowd out relevant ones.
                outcome = jobs_repo.upsert_job(db, job, source_id=source_id, decision=decision, now=now_iso,
                                               allow_write=remaining > 0 or not decision.passed)
            except JobAgentError as exc:
                st.job_errors += 1
                repo.record_error(db, component=f"discovery:{name}:{ident}", error=exc,
                                  task_name=run.task_name if run else None, run_id=run.run_id if run else None,
                                  details={"external_id": job.external_id})
                continue
            if outcome.status == "new":
                st.new += 1
            elif outcome.status == "updated":
                st.updated += 1
            elif outcome.status == "unchanged":
                st.unchanged += 1
            elif outcome.status == "duplicate":
                st.duplicates += 1
            elif outcome.status == "deferred":
                st.deferred += 1
            if outcome.status in {"new", "updated"} and not outcome.filtered:
                remaining -= 1
            st.reopened += int(outcome.reopened)
            st.reposts += int(outcome.reposted)
            if outcome.filtered:
                st.filtered += 1
                reasons.update(outcome.filter_codes)
        st.filter_reasons = dict(reasons)
        jobs_repo.record_source_result(db, source_id, success=True, now=now_iso)
    except Exception as exc:  # isolate this source; recorded below, never swallowed
        kind = exc.kind if isinstance(exc, JobAgentError) else ErrorKind.PERMANENT
        st.error = f"{type(exc).__name__}: {exc}"[:500]
        st.error_kind = kind.value
        log.warning("source %s:%s failed: %s", name, ident, st.error)
        repo.record_error(db, component=f"discovery:{name}:{ident}", error=exc, kind=kind,
                          task_name=run.task_name if run else None, run_id=run.run_id if run else None,
                          retryable=kind == ErrorKind.RECOVERABLE)
        if source_id is not None:
            jobs_repo.record_source_result(db, source_id, success=False, now=now_iso, error=st.error)
    return remaining


def discover_task(ctx: "AppContext", run: "RunInfo") -> dict[str, Any]:
    """``discover`` task. Partial when any source failed; raises when every source failed."""
    actor = "scheduler" if run.trigger == "scheduled" else "system"
    result = discover_jobs(ctx, actor=actor, run=run)
    attempted = [s for s in result.sources if not s.skipped]
    if attempted and all(s.error for s in attempted):
        kinds = {s.error_kind for s in attempted}
        message = f"all {len(attempted)} source(s) failed: " + "; ".join(
            f"{s.key}: {s.error}" for s in attempted[:5])
        if ErrorKind.RECOVERABLE.value in kinds:
            raise RecoverableError(message[:1500])
        raise PermanentError(message[:1500])
    return result.summary()


# --------------------------------------------------------------------------- #
# Source status / reachability
# --------------------------------------------------------------------------- #
class SourceStatus(_Model):
    source: str
    identifier: str
    configured: bool
    enabled: bool
    is_mock: bool = False
    last_run_at: str | None = None
    last_success_at: str | None = None
    consecutive_failures: int = 0
    last_error: str | None = None
    active_jobs: int = 0


def list_sources(ctx: "AppContext") -> list[SourceStatus]:
    """Configured sources merged with what ``job_sources`` knows about past runs."""
    config = ctx.config
    rows = {(r["source_name"], r["identifier"]): r for r in jobs_repo.list_job_sources(ctx.db)}
    out: list[SourceStatus] = []
    for name in REAL_SOURCES:
        enabled = source_enabled(config, name)
        for ident in configured_identifiers(config, name):
            row = rows.pop((name, ident), None)
            out.append(_status(name, ident, True, enabled, row))
    for (name, ident), row in sorted(rows.items()):
        out.append(_status(name, ident, False, False, row))
    return out


def _status(name: str, ident: str, configured: bool, enabled: bool, row: Any) -> SourceStatus:
    if row is None:
        return SourceStatus(source=name, identifier=ident, configured=configured, enabled=enabled,
                            is_mock=name in MOCK_SOURCES)
    return SourceStatus(
        source=name, identifier=ident, configured=configured, enabled=enabled, is_mock=bool(row["is_mock"]),
        last_run_at=row["last_run_at"], last_success_at=row["last_success_at"],
        consecutive_failures=int(row["consecutive_failures"] or 0), last_error=row["last_error"],
        active_jobs=int(row["active_jobs"] or 0),
    )


class SourceCheck(_Model):
    source: str
    identifier: str
    ok: bool
    detail: str
    error_kind: str | None = None


def check_sources(ctx: "AppContext", *, sources: list[str] | None = None,
                  http_client: PoliteHttpClient | httpx.Client | None = None) -> list[SourceCheck]:
    """One lightweight live request per configured identifier (no database writes)."""
    require_permission(ctx.config, Permission.READ_JOBS)
    targets = resolve_targets(ctx.config, sources)
    http, owns_http = _make_http(ctx, http_client)
    results: list[SourceCheck] = []
    try:
        for name, ident in targets:
            try:
                detail = create_adapter(name, http=http, config=ctx.config).check(ident)
                results.append(SourceCheck(source=name, identifier=ident, ok=True, detail=detail))
            except JobAgentError as exc:
                results.append(SourceCheck(source=name, identifier=ident, ok=False, detail=str(exc)[:300],
                                           error_kind=exc.kind.value))
    finally:
        if owns_http:
            http.close()
    return results
