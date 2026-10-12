"""Persistence and deduplication: upserts, material changes, duplicates, reposts, closures."""

from __future__ import annotations

import pytest

from job_agent.database import repo
from job_agent.database.connection import from_json
from job_agent.discovery import repository as jr
from job_agent.discovery.filters import FilterDecision
from job_agent.models import NormalizedJob, PipelineState

NOW = "2026-10-01T12:00:00+00:00"
LATER = "2026-10-02T12:00:00+00:00"
DESC = ("Join our analytics team to build dashboards, model retail data with SQL and Python, and present "
        "insights to business stakeholders across the company every week.")


def nj(**kw) -> NormalizedJob:
    base = dict(source_name="greenhouse", source_identifier="example", external_id="1001",
                employer="Example Consulting", title="Business Analyst", location="New York, NY",
                description_text=DESC, job_url="https://job-boards.greenhouse.io/example/jobs/1001")
    base.update(kw)
    return NormalizedJob(**base)


def ok() -> FilterDecision:
    return FilterDecision()


def failing(code: str = "location", message: str = "location 'Austin, TX' does not match") -> FilterDecision:
    d = FilterDecision()
    d.fail(code, message)
    return d


def upsert(db, job, decision=None, now=NOW, **kw):
    source_id = jr.ensure_source(db, job.source_name, job.source_identifier, is_mock=job.source_name == "fixture",
                                 now=now)
    return jr.upsert_job(db, job, source_id=source_id, decision=decision or ok(), now=now, **kw)


def row(db, job_id):
    return db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))


def changes(db, job_id):
    return [r["change_type"] for r in jr.job_changes(db, job_id)]


def test_job_uid_is_deterministic():
    uid = jr.job_uid_for("greenhouse", "example", "1001")
    assert uid == jr.job_uid_for("greenhouse", "example", "1001")
    assert uid.startswith("J-") and len(uid) == 12 and int(uid[2:], 16) >= 0
    assert uid != jr.job_uid_for("lever", "example", "1001")


def test_insert_then_unchanged_rerun(db):
    first = upsert(db, nj())
    assert first.status == "new" and first.job_uid == jr.job_uid_for("greenhouse", "example", "1001")
    r = row(db, first.job_id)
    assert r["pipeline_state"] == "discovered" and r["listing_status"] == "active" and r["times_seen"] == 1
    assert r["employer_normalized"] == "example consulting" and r["title_normalized"] == "business analyst"
    assert r["job_url_canonical"] == "https://job-boards.greenhouse.io/example/jobs/1001"
    assert r["source_id"] is not None
    second = upsert(db, nj(), now=LATER)
    assert second.status == "unchanged" and second.job_id == first.job_id
    r = row(db, first.job_id)
    assert r["times_seen"] == 2 and r["last_checked_at"] == LATER and r["last_changed_at"] == NOW
    assert changes(db, first.job_id) == []


def test_filtered_listing_is_stored_with_reason(db):
    out = upsert(db, nj(location="Austin, TX"), failing())
    r = row(db, out.job_id)
    assert out.filtered and r["pipeline_state"] == "filtered_out" and "Austin" in r["filter_reason"]


def test_material_change_is_recorded_and_scored_state_is_kept(db):
    out = upsert(db, nj())
    repo.set_job_state(db, out.job_id, PipelineState.SCORED, actor="system")
    old_hash = row(db, out.job_id)["content_hash"]
    changed = upsert(db, nj(title="Business Analyst II", salary_min=90000.0), now=LATER)
    assert changed.status == "updated"
    r = row(db, out.job_id)
    assert r["title"] == "Business Analyst II" and r["salary_min"] == 90000.0
    assert r["content_hash"] != old_hash and r["last_changed_at"] == LATER
    # scored -> discovered is not an allowed transition: the state stays, the scorer re-scores on hash change
    assert r["pipeline_state"] == "scored"
    (change,) = jr.job_changes(db, out.job_id)
    assert change["change_type"] == "material_change" and change["old_hash"] == old_hash
    assert "title: 'Business Analyst' -> 'Business Analyst II'" in change["summary"]


def test_changed_listing_that_now_fails_filters_moves_to_filtered_out(db):
    out = upsert(db, nj())
    repo.set_job_state(db, out.job_id, PipelineState.SHORTLISTED, actor="system")
    upsert(db, nj(description_text=DESC + " Requires 6+ years of experience."),
           failing("experience_years", "requires 6+ years"), now=LATER)
    r = row(db, out.job_id)
    assert r["pipeline_state"] == "filtered_out" and r["filter_reason"] == "requires 6+ years"


def test_changed_listing_with_application_keeps_state(db):
    out = upsert(db, nj())
    repo.set_job_state(db, out.job_id, PipelineState.SHORTLISTED, actor="system")
    repo.create_application(db, out.job_id, actor="user")
    res = upsert(db, nj(location="Austin, TX"), failing(), now=LATER)
    assert row(db, out.job_id)["pipeline_state"] == "shortlisted"
    assert "application exists" in res.note
    assert "application exists" in jr.job_changes(db, out.job_id)[-1]["summary"]


def test_filtered_out_listing_that_now_passes_is_noted_not_forced(db):
    out = upsert(db, nj(location="Austin, TX"), failing())
    res = upsert(db, nj(location="New York, NY"), ok(), now=LATER)
    assert res.status == "updated" and "not an allowed transition" in res.note
    assert row(db, out.job_id)["pipeline_state"] == "filtered_out"


def test_cross_source_duplicate_by_url_is_stored_as_duplicate_and_idempotent(db):
    canonical = upsert(db, nj())
    dup_job = nj(source_name="fixture", source_identifier="data/mock.json", external_id="m-1",
                 employer="Example Consulting, Inc.",
                 job_url="https://JOB-BOARDS.greenhouse.io/example/jobs/1001/?utm_source=feed")
    dup = upsert(db, dup_job)
    assert dup.status == "duplicate" and dup.duplicate_of == canonical.job_uid
    r = row(db, dup.job_id)
    assert r["duplicate_of_job_id"] == canonical.job_id and r["pipeline_state"] == "filtered_out"
    assert r["filter_reason"].startswith(f"duplicate of {canonical.job_uid}")
    assert changes(db, canonical.job_id) == ["duplicate_detected"]
    # Second run: recognized by its own key, no new rows or change records.
    again = upsert(db, dup_job, now=LATER)
    assert again.status == "unchanged" and again.job_id == dup.job_id
    assert changes(db, canonical.job_id) == ["duplicate_detected"]
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE duplicate_of_job_id IS NULL") == 1


def test_cross_source_duplicate_by_fingerprint_and_similarity(db):
    canonical = upsert(db, nj(job_url=None))
    similar = nj(source_name="lever", source_identifier="example", external_id="uuid-1", job_url=None,
                 description_text=DESC + " Apply now.")
    out = upsert(db, similar)
    assert out.status == "duplicate" and out.duplicate_of == canonical.job_uid
    assert "similar" in out.duplicate_reason


def test_fingerprint_with_different_description_is_not_merged(db):
    upsert(db, nj(job_url=None))
    other = nj(source_name="lever", source_identifier="example", external_id="uuid-2", job_url=None,
               description_text="Completely different listing about warehouse logistics and forklift safety.")
    assert upsert(db, other).status == "new"


def test_same_title_different_location_or_requisition_are_distinct(db):
    upsert(db, nj(requisition_id="R-1", job_url=None))
    other_city = nj(source_name="lever", source_identifier="example", external_id="x1", job_url=None,
                    location="Houston, TX")
    other_req = nj(source_name="lever", source_identifier="example", external_id="x2", job_url=None,
                   requisition_id="R-2")
    assert upsert(db, other_city).status == "new"
    assert upsert(db, other_req).status == "new"
    # Same source key, same requisition, different city: distinct postings.
    same_board = nj(external_id="1002", requisition_id="R-1", location="Houston, TX",
                    job_url="https://job-boards.greenhouse.io/example/jobs/1002")
    assert upsert(db, same_board).status == "new"


def test_cross_source_requisition_match_is_duplicate(db):
    canonical = upsert(db, nj(requisition_id="REQ-77", job_url=None))
    other = nj(source_name="ashby", source_identifier="example", external_id="a-1", job_url=None,
               title="Business Analyst (Retail)", requisition_id="REQ-77", description_text="Short text.")
    out = upsert(db, other)
    assert out.status == "duplicate" and out.duplicate_of == canonical.job_uid
    assert "requisition" in out.duplicate_reason


def test_same_key_url_collision_keeps_both_listings(db):
    shared = "https://careers.example.com/jobs"
    a = upsert(db, nj(external_id="1", job_url=shared))
    b = upsert(db, nj(external_id="2", title="Data Analyst", job_url=shared))
    assert a.status == b.status == "new"
    assert row(db, a.job_id)["job_url_canonical"] == shared
    assert row(db, b.job_id)["job_url_canonical"] is None and row(db, b.job_id)["job_url"] == shared


def test_closure_and_reopen(db):
    a = upsert(db, nj(external_id="1", job_url=None))
    b = upsert(db, nj(external_id="2", title="Data Analyst", job_url=None))
    closed = jr.close_missing_jobs(db, "greenhouse", "example", {"1"}, LATER)
    assert closed == [b.job_uid]
    assert row(db, b.job_id)["listing_status"] == "closed" and row(db, a.job_id)["listing_status"] == "active"
    assert changes(db, b.job_id) == ["closed"]
    back = upsert(db, nj(external_id="2", title="Data Analyst", job_url=None), now="2026-10-03T00:00:00+00:00")
    assert back.reopened and back.status == "unchanged"
    assert row(db, b.job_id)["listing_status"] == "active"
    assert changes(db, b.job_id) == ["closed", "reopened"]


def test_repost_with_new_id_after_closure(db):
    old = upsert(db, nj(external_id="1", job_url=None))
    jr.close_missing_jobs(db, "greenhouse", "example", set(), LATER)
    new = upsert(db, nj(external_id="9", job_url=None), now="2026-10-03T00:00:00+00:00")
    assert new.status == "new" and new.reposted
    (change,) = jr.job_changes(db, new.job_id)
    assert change["change_type"] == "repost" and old.job_uid in change["summary"]


def test_repost_by_bumped_posting_date(db):
    out = upsert(db, nj(posted_at="2026-09-01T00:00:00+00:00", raw_metadata={"posted_at_source": "first_published"}))
    res = upsert(db, nj(posted_at="2026-09-20T00:00:00+00:00", raw_metadata={"posted_at_source": "first_published"}),
                 now=LATER)
    assert res.status == "unchanged" and res.reposted
    assert row(db, out.job_id)["posted_at"] == "2026-09-20T00:00:00+00:00"
    assert changes(db, out.job_id) == ["repost"]


def test_deferred_when_limit_reached(db):
    existing = upsert(db, nj(external_id="1", job_url=None))
    assert upsert(db, nj(external_id="2", title="Data Analyst", job_url=None), allow_write=False).status == "deferred"
    assert db.scalar("SELECT COUNT(*) FROM jobs") == 1
    changed = upsert(db, nj(external_id="1", job_url=None, title="Business Analyst II"), allow_write=False,
                     now=LATER)
    assert changed.status == "deferred"
    r = row(db, existing.job_id)
    assert r["title"] == "Business Analyst" and r["last_checked_at"] == LATER  # bookkeeping only


def test_integrity_error_from_concurrent_insert_is_handled(db, monkeypatch):
    job = nj()
    upsert(db, job)  # the "other run" already inserted it
    real = jr.get_job_by_key
    calls = {"n": 0}

    def stale_first_read(*args, **kwargs):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(*args, **kwargs)

    monkeypatch.setattr(jr, "get_job_by_key", stale_first_read)
    out = jr.upsert_job(db, job, source_id=None, decision=ok(), now=LATER)
    assert out.status == "unchanged" and "concurrently" in out.note
    assert db.scalar("SELECT COUNT(*) FROM jobs") == 1


def test_job_uid_collision_gets_a_salted_uid(db, make_job):
    taken = jr.job_uid_for("greenhouse", "example", "1001")
    make_job(job_uid=taken, source_name="other", source_identifier="x", external_id="zzz",
             job_url_canonical=None)
    out = upsert(db, nj())
    assert out.status == "new" and out.job_uid != taken
    assert out.job_uid == jr.job_uid_for("greenhouse", "example", "1001", salt=1)


def test_json_columns_and_salary_flags(db):
    job = nj(required_qualifications=["SQL"], preferred_qualifications=["dbt"], salary_is_base=False,
             raw_metadata={"years_experience_min": 2})
    r = row(db, upsert(db, job).job_id)
    assert from_json(r["required_qualifications"]) == ["SQL"]
    assert from_json(r["preferred_qualifications"]) == ["dbt"]
    assert r["salary_is_base"] == 0
    assert from_json(r["raw_metadata"])["years_experience_min"] == 2


def test_job_sources_bookkeeping(db):
    sid = jr.ensure_source(db, "lever", "acme", is_mock=False, now=NOW)
    assert jr.ensure_source(db, "lever", "acme", is_mock=False, now=LATER) == sid
    jr.record_source_result(db, sid, success=False, now=NOW, error="RecoverableError: HTTP 500 token=abc123secret")
    jr.record_source_result(db, sid, success=False, now=LATER, error="again")
    r = db.query_one("SELECT * FROM job_sources WHERE id = ?", (sid,))
    assert r["consecutive_failures"] == 2 and r["last_error"] == "again" and r["last_success_at"] is None
    jr.record_source_result(db, sid, success=True, now=LATER)
    r = db.query_one("SELECT * FROM job_sources WHERE id = ?", (sid,))
    assert r["consecutive_failures"] == 0 and r["last_error"] is None and r["last_success_at"] == LATER


def test_record_source_failure_redacts(db):
    sid = jr.ensure_source(db, "lever", "acme", is_mock=False, now=NOW)
    jr.record_source_result(db, sid, success=False, now=NOW, error="failed token=abc123secret")
    assert "abc123secret" not in db.scalar("SELECT last_error FROM job_sources WHERE id = ?", (sid,))


@pytest.mark.parametrize("field", ["description_text", "salary_max", "closing_at"])
def test_content_hash_covers_material_fields(field):
    base = jr.job_fields(nj())
    changed = dict(base)
    changed[field] = "x" if field != "salary_max" else 1.0
    assert jr.content_hash(base) != jr.content_hash(changed)
    meta_only = dict(base)
    meta_only["raw_metadata"] = "{}"
    meta_only["posted_at"] = "2026-01-01T00:00:00+00:00"
    assert jr.content_hash(base) == jr.content_hash(meta_only)
