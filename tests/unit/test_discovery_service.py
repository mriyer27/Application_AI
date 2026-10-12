"""End-to-end discovery with mocked public APIs (respx) and the mock fixture source."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from job_agent.discovery import service
from job_agent.discovery.http import PoliteHttpClient
from job_agent.errors import PermanentError, PermissionDenied, RecoverableError
from job_agent.tasks import RunInfo

FX = Path(__file__).resolve().parents[1] / "fixtures" / "discovery"
NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
GH = "https://boards-api.greenhouse.io/v1/boards/example"
LEVER = "https://api.lever.co/v0/postings/acme"
ASHBY = "https://api.ashbyhq.com/posting-api/job-board/examplefin"
REAL = ["greenhouse:example", "lever:acme", "ashby:examplefin"]


def load(name: str):
    return json.loads((FX / name).read_text(encoding="utf-8"))


@pytest.fixture
def http(ctx):
    client = PoliteHttpClient(ctx.config.sources, sleep=lambda s: None)
    yield client
    client.close()


@pytest.fixture
def lever_two_pages(monkeypatch):
    monkeypatch.setattr("job_agent.discovery.sources.lever.DEFAULT_PAGE_SIZE", 2)


def mock_apis(router: respx.MockRouter, *, gh_jobs_status: int = 200, gh_jobs=None) -> dict[str, respx.Route]:
    pages = {0: load("lever_page1.json"), 2: load("lever_page2.json")}

    def lever(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages.get(int(request.url.params["skip"]), []))

    routes = {
        "gh_board": router.get(GH).mock(return_value=httpx.Response(200, json=load("greenhouse_board.json"))),
        "gh_jobs": router.get(f"{GH}/jobs").mock(
            return_value=httpx.Response(gh_jobs_status, json=gh_jobs or load("greenhouse_jobs.json"))),
        "lever": router.get(LEVER).mock(side_effect=lever),
        "ashby": router.get(ASHBY).mock(return_value=httpx.Response(200, json=load("ashby_board.json"))),
    }
    return routes


def write_fixture(home: Path, name: str, jobs: list[dict], *, complete: bool = True) -> str:
    path = home / "data" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"employer": "Mock Analytics Co", "complete": complete, "jobs": jobs}))
    return f"data/{name}"


def mock_job(id_: str, title: str, **kw) -> dict:
    return {"id": id_, "title": title, "location": "New York, NY", "employment_type": "Full-time",
            "posted_at": "2026-09-28T00:00:00Z", "url": f"https://example.invalid/mock/{id_}",
            "description": f"Mock listing {id_} for {title}.", **kw}


def by_key(result, key):
    return next(s for s in result.sources if s.key == key)


def states(db, source):
    return {r["external_id"]: r["pipeline_state"] for r in db.query_all(
        "SELECT external_id, pipeline_state FROM jobs WHERE source_name = ?", (source,))}


# --------------------------------------------------------------------------- #
def test_discover_real_sources_counts_and_states(ctx, db, http, lever_two_pages):
    with respx.mock(assert_all_called=True) as router:
        routes = mock_apis(router)
        result = service.discover_jobs(ctx, sources=REAL, http_client=http, now=NOW)
    assert routes["lever"].call_count == 2  # skip=0 and skip=2
    gh = by_key(result, "greenhouse:example")
    assert (gh.fetched, gh.invalid, gh.relevant, gh.new, gh.filtered, gh.complete) == (10, 2, 9, 9, 6, True)
    assert gh.filter_reasons == {"location": 1, "employment_type": 1, "excluded_title_keyword": 1,
                                 "experience_years": 1, "closing_date_passed": 1, "posting_age": 1}
    lv = by_key(result, "lever:acme")
    assert (lv.fetched, lv.relevant, lv.new, lv.filtered) == (3, 3, 3, 0)  # Washington, D.C. is US
    ab = by_key(result, "ashby:examplefin")
    assert (ab.fetched, ab.relevant, ab.new, ab.filtered) == (3, 2, 2, 0)
    t = result.totals
    assert (t.fetched, t.invalid, t.relevant, t.new, t.filtered, t.updated, t.unchanged) == (16, 2, 14, 14, 6, 0, 0)
    assert not result.partial and not result.limit_reached and result.failed_sources == []

    gh_states = states(db, "greenhouse")
    assert "1004" not in gh_states  # irrelevant title: counted, never stored
    assert gh_states["1003"] == "filtered_out"  # London, United Kingdom is not in search.locations
    assert gh_states["1009"] == "discovered"  # "lead" does not exclude "Leadership"
    assert {k for k, v in gh_states.items() if v == "discovered"} == {"1001", "1002", "1009"}
    assert db.scalar("SELECT filter_reason FROM jobs WHERE external_id = '1003'").startswith("location")
    assert db.scalar("SELECT COUNT(*) FROM jobs") == 14
    src = db.query_one("SELECT * FROM job_sources WHERE source_name = 'greenhouse' AND identifier = 'example'")
    assert src["last_success_at"] and src["consecutive_failures"] == 0 and src["is_mock"] == 0
    audit = db.query_all("SELECT * FROM audit_events WHERE action = 'discover_jobs'")
    assert len(audit) == 1 and audit[0]["outcome"] == "success"


def test_rerun_is_idempotent(ctx, db, http, lever_two_pages):
    with respx.mock(assert_all_called=False) as router:
        mock_apis(router)
        first = service.discover_jobs(ctx, sources=REAL, http_client=http, now=NOW)
        second = service.discover_jobs(ctx, sources=REAL, http_client=http, now=NOW)
    assert first.totals.new == 14
    t = second.totals
    assert (t.new, t.updated, t.unchanged, t.closed, t.duplicates) == (0, 0, 14, 0, 0)
    assert db.scalar("SELECT COUNT(*) FROM jobs") == 14
    assert db.scalar("SELECT MIN(times_seen) FROM jobs") == 2
    assert db.scalar("SELECT COUNT(*) FROM job_changes") == 0


def test_one_failing_source_does_not_stop_others(ctx, db, http, lever_two_pages):
    with respx.mock(assert_all_called=False) as router:
        routes = mock_apis(router, gh_jobs_status=500)
        result = service.discover_jobs(ctx, sources=REAL, http_client=http, now=NOW)
    assert routes["gh_jobs"].call_count == ctx.config.sources.max_retries + 1
    gh = by_key(result, "greenhouse:example")
    assert gh.error and "HTTP 500" in gh.error and gh.error_kind == "recoverable" and gh.new == 0
    assert result.partial and result.failed_sources == ["greenhouse:example"]
    assert by_key(result, "lever:acme").new == 3 and by_key(result, "ashby:examplefin").new == 2
    err = db.query_one("SELECT * FROM errors WHERE component = 'discovery:greenhouse:example'")
    assert err["error_kind"] == "recoverable" and err["retryable"] == 1
    src = db.query_one("SELECT * FROM job_sources WHERE source_name = 'greenhouse'")
    assert src["consecutive_failures"] == 1 and "HTTP 500" in src["last_error"] and src["last_success_at"] is None
    assert db.query_one("SELECT outcome FROM audit_events WHERE action = 'discover_jobs'")["outcome"] == "partial"


def test_403_is_permanent_and_recorded(ctx, db, http):
    with respx.mock(assert_all_called=False) as router:
        router.get(GH).mock(return_value=httpx.Response(403))
        result = service.discover_jobs(ctx, sources=["greenhouse:example"], http_client=http, now=NOW)
    gh = result.sources[0]
    assert gh.error_kind == "permanent" and "access denied" in gh.error
    assert db.scalar("SELECT requires_human_action FROM errors WHERE component = 'discovery:greenhouse:example'") == 1


def test_429_with_retry_after_then_success_through_service(ctx, http):
    sleeps: list[float] = []
    client = PoliteHttpClient(ctx.config.sources, sleep=sleeps.append)
    with respx.mock(assert_all_called=False) as router:
        router.get(ASHBY).mock(side_effect=[httpx.Response(429, headers={"Retry-After": "3"}),
                                            httpx.Response(200, json=load("ashby_board.json"))])
        result = service.discover_jobs(ctx, sources=["ashby:examplefin"], http_client=client, now=NOW)
    client.close()
    assert result.sources[0].new == 2 and not result.partial and 3.0 in sleeps


def test_closures_only_after_complete_successful_fetch(ctx, db, home):
    ident = write_fixture(home, "mock.json", [mock_job("m-1", "Data Analyst"), mock_job("m-2", "Product Analyst")])
    first = service.discover_jobs(ctx, sources=[f"fixture:{ident}"], now=NOW)
    assert first.sources[0].new == 2 and first.sources[0].is_mock

    write_fixture(home, "mock.json", [mock_job("m-1", "Data Analyst")], complete=False)
    partial = service.discover_jobs(ctx, sources=[f"fixture:{ident}"], now=NOW)
    assert partial.sources[0].closed == 0 and not partial.sources[0].complete
    assert db.scalar("SELECT listing_status FROM jobs WHERE external_id = 'm-2'") == "active"

    (home / "data" / "mock.json").write_text("{not json")
    failed = service.discover_jobs(ctx, sources=[f"fixture:{ident}"], now=NOW)
    assert failed.partial and failed.sources[0].closed == 0
    assert db.scalar("SELECT listing_status FROM jobs WHERE external_id = 'm-2'") == "active"

    write_fixture(home, "mock.json", [mock_job("m-1", "Data Analyst")])
    complete = service.discover_jobs(ctx, sources=[f"fixture:{ident}"], now=NOW)
    assert complete.sources[0].closed == 1
    assert db.scalar("SELECT listing_status FROM jobs WHERE external_id = 'm-2'") == "closed"
    job_id = db.scalar("SELECT id FROM jobs WHERE external_id = 'm-2'")
    assert db.scalar("SELECT change_type FROM job_changes WHERE job_id = ?", (job_id,)) == "closed"

    write_fixture(home, "mock.json", [mock_job("m-1", "Data Analyst"), mock_job("m-2", "Product Analyst")])
    reopened = service.discover_jobs(ctx, sources=[f"fixture:{ident}"], now=NOW)
    assert reopened.sources[0].reopened == 1
    assert db.scalar("SELECT listing_status FROM jobs WHERE external_id = 'm-2'") == "active"


def test_empty_fetch_does_not_close_everything(ctx, db, home):
    ident = write_fixture(home, "mock.json", [mock_job("m-1", "Data Analyst")])
    service.discover_jobs(ctx, sources=[f"fixture:{ident}"], now=NOW)
    write_fixture(home, "mock.json", [])
    result = service.discover_jobs(ctx, sources=[f"fixture:{ident}"], now=NOW)
    assert result.sources[0].closed == 0 and "precaution" in result.sources[0].warnings[0]


def test_material_change_and_repost_through_service(ctx, db, home):
    ident = write_fixture(home, "mock.json", [mock_job("m-1", "Data Analyst")])
    service.discover_jobs(ctx, sources=[f"fixture:{ident}"], now=NOW)
    write_fixture(home, "mock.json", [mock_job("m-1", "Data Analyst", description="Now requires Tableau.")])
    changed = service.discover_jobs(ctx, sources=[f"fixture:{ident}"], now=NOW)
    assert changed.sources[0].updated == 1
    job_id = db.scalar("SELECT id FROM jobs WHERE external_id = 'm-1'")
    assert db.scalar("SELECT change_type FROM job_changes WHERE job_id = ?", (job_id,)) == "material_change"
    # Removed, then re-posted under a new id with the same content.
    write_fixture(home, "mock.json", [mock_job("m-9", "Data Analyst", description="Now requires Tableau.",
                                               url="https://example.invalid/mock/m-9")])
    reposted = service.discover_jobs(ctx, sources=[f"fixture:{ident}"], now=NOW)
    s = reposted.sources[0]
    assert (s.closed, s.new, s.reposts) == (1, 1, 1)


def test_cross_source_duplicate_through_service(ctx, db, home, http):
    gh_url = "https://job-boards.greenhouse.io/example/jobs/1001"
    ident = write_fixture(home, "mock.json", [mock_job("m-1", "Business Analyst - Retail Energy", url=gh_url)])
    with respx.mock(assert_all_called=False) as router:
        mock_apis(router)
        result = service.discover_jobs(ctx, sources=["greenhouse:example", f"fixture:{ident}"], http_client=http,
                                       now=NOW)
    fx = by_key(result, f"fixture:{ident}")
    assert fx.duplicates == 1 and fx.new == 0
    canonical = db.query_one("SELECT * FROM jobs WHERE source_name = 'greenhouse' AND external_id = '1001'")
    dup = db.query_one("SELECT * FROM jobs WHERE source_name = 'fixture'")
    assert dup["duplicate_of_job_id"] == canonical["id"] and dup["pipeline_state"] == "filtered_out"
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE job_url_canonical = ? AND duplicate_of_job_id IS NULL",
                     (gh_url,)) == 1


def test_duplicate_within_one_fetch(ctx, home):
    ident = write_fixture(home, "mock.json", [mock_job("m-1", "Data Analyst"), mock_job("m-1", "Data Analyst")])
    result = service.discover_jobs(ctx, sources=[f"fixture:{ident}"], now=NOW)
    assert (result.sources[0].new, result.sources[0].duplicates) == (1, 1)


def test_limit_defers_and_skips_then_next_run_continues(ctx, db, home):
    a = write_fixture(home, "a.json", [mock_job(f"a-{i}", "Data Analyst", location=f"City {i}, NY")
                                       for i in range(3)])
    b = write_fixture(home, "b.json", [mock_job("b-1", "Product Analyst")])
    first = service.discover_jobs(ctx, sources=[f"fixture:{a}", f"fixture:{b}"], limit=2, now=NOW)
    sa, sb = first.sources
    assert (sa.new, sa.deferred) == (2, 1) and sb.skipped == "run limit reached"
    assert first.limit_reached and first.limit == 2 and not first.partial
    assert db.scalar("SELECT COUNT(*) FROM jobs") == 2
    second = service.discover_jobs(ctx, sources=[f"fixture:{a}", f"fixture:{b}"], limit=10, now=NOW)
    sa, sb = second.sources
    assert (sa.new, sa.unchanged, sb.new) == (1, 2, 1) and not second.limit_reached


def test_default_targets_and_limit_from_config(ctx, http):
    ctx.config.sources.greenhouse.boards = ["example"]
    ctx.config.sources.lever.companies = []
    ctx.config.sources.ashby.enabled = False
    ctx.config.sources.ashby.boards = ["examplefin"]
    assert service.resolve_targets(ctx.config) == [("greenhouse", "example")]
    assert service.resolve_targets(ctx.config, ["ashby"]) == [("ashby", "examplefin")]  # explicit beats disabled
    with respx.mock(assert_all_called=False) as router:
        mock_apis(router)
        result = service.discover_jobs(ctx, http_client=http, now=NOW)
    assert result.limit == ctx.config.search.max_jobs_per_run and [s.key for s in result.sources] == \
        ["greenhouse:example"]


@pytest.mark.parametrize("entry,match", [
    ("monster", "unknown job source"),
    ("fixture", "needs a file"),
    ("greenhouse:../../etc", "invalid greenhouse identifier"),
    ("lever", "no identifiers configured"),
])
def test_bad_source_entries(ctx, entry, match):
    ctx.config.sources.lever.companies = []
    with pytest.raises(PermanentError, match=match):
        service.discover_jobs(ctx, sources=[entry])


def test_fixture_path_must_stay_inside_home(ctx, db):
    result = service.discover_jobs(ctx, sources=["fixture:../outside.json"], now=NOW)
    assert result.partial and "outside" in result.sources[0].error


def test_permission_required(ctx):
    ctx.config.permissions.read_jobs = False
    with pytest.raises(PermissionDenied):
        service.discover_jobs(ctx, sources=["fixture:data/x.json"])


def test_discover_task_summary_and_all_failed(ctx, db, home, monkeypatch):
    ident = write_fixture(home, "mock.json", [mock_job("m-1", "Data Analyst")])
    for name in ("greenhouse", "lever", "ashby"):
        monkeypatch.setattr(getattr(ctx.config.sources, name), "enabled", False)
    run = RunInfo(run_id=1, run_uid="R-test", task_name="discover", trigger="manual")
    db.execute("INSERT INTO agent_runs (id, run_uid, task_name, trigger, status, started_at) "
               "VALUES (1, 'R-test', 'discover', 'manual', 'running', '2026-10-01T00:00:00+00:00')")
    original = service.discover_jobs

    def with_fixture(ctx_, **kw):
        return original(ctx_, sources=[f"fixture:{ident}"], **kw)

    monkeypatch.setattr(service, "discover_jobs", with_fixture)
    summary = service.discover_task(ctx, run)
    assert summary["partial"] is False and summary["totals"]["new"] == 1 and summary["finished_at"]
    json.dumps(summary)  # JSON-serializable for agent_runs.summary
    assert db.scalar("SELECT run_id FROM audit_events WHERE action = 'discover_jobs'") == 1

    (home / "data" / "mock.json").write_text("{broken")
    with pytest.raises(PermanentError, match="all 1 source"):
        service.discover_task(ctx, run)


def test_discover_task_raises_recoverable_when_network_down(ctx, http, monkeypatch):
    run = RunInfo(run_id=0, run_uid="R-x", task_name="discover", trigger="scheduled")
    original = service.discover_jobs
    monkeypatch.setattr(service, "discover_jobs",
                        lambda ctx_, **kw: original(ctx_, sources=["ashby:examplefin"], http_client=http,
                                                    **{**kw, "run": None}))
    with respx.mock(assert_all_called=False) as router:
        router.get(ASHBY).mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(RecoverableError, match="connection error"):
            service.discover_task(ctx, run)


def test_list_and_check_sources(ctx, db, http):
    ctx.config.sources.greenhouse.boards = ["example"]
    ctx.config.sources.lever.companies = ["acme"]
    ctx.config.sources.ashby.boards = ["examplefin"]
    with respx.mock(assert_all_called=False) as router:
        mock_apis(router)
        router.get(LEVER).mock(return_value=httpx.Response(404))
        service.discover_jobs(ctx, sources=["greenhouse:example"], http_client=http, now=NOW)
        checks = service.check_sources(ctx, http_client=http)
    assert [(c.source, c.ok) for c in checks] == [("greenhouse", True), ("lever", False), ("ashby", True)]
    assert "Example Consulting" in checks[0].detail and "board not found" in checks[1].detail
    assert checks[2].detail == "reachable; 3 listed job(s)"
    status = {(s.source, s.identifier): s for s in service.list_sources(ctx)}
    gh = status[("greenhouse", "example")]
    assert gh.configured and gh.enabled and gh.last_success_at and gh.active_jobs == 9
    assert status[("lever", "acme")].last_success_at is None


def test_limit_counts_only_jobs_that_pass_filters(ctx, db, http):
    with respx.mock(assert_all_called=False) as router:
        mock_apis(router)
        result = service.discover_jobs(ctx, sources=["greenhouse:example"], http_client=http, now=NOW, limit=1)
    gh = by_key(result, "greenhouse:example")
    stored = {r["pipeline_state"]: r["n"] for r in db.query_all(
        "SELECT pipeline_state, COUNT(*) n FROM jobs GROUP BY pipeline_state")}
    assert stored.get("discovered") == 1  # exactly `limit` passing listings written
    assert stored.get("filtered_out") == gh.filtered == 6  # filtered rows never consume the limit
    assert gh.deferred == 2 and result.limit_reached  # the other passing listings wait for the next run
