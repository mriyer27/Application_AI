from __future__ import annotations

import json

import pytest

from job_agent.database import repo
from job_agent.errors import PermanentError
from job_agent.models import CodingIntensity, JobAnalysis, RunStatus
from job_agent.scoring import repository as score_repo
from job_agent.scoring.engine import score_job, scoring_config_hash
from job_agent.scoring.service import ScoringSummary, score_jobs

DESCRIPTION = (
    "Acme Analytics is hiring a Business Analyst to join our insights team in New York. "
    "You will partner with finance and operations leaders to analyze trends, build clear reports, "
    "and recommend improvements. You will receive mentorship from experienced colleagues. "
    "We value curiosity, clear writing, and ownership of outcomes. This is a full-time position "
    "based in our New York office with a collaborative culture."
)
GOOD = json.dumps(["Proficiency in SQL and Python", "Experience with Tableau", "0-2 years of experience"])
WEAK = json.dumps(["Expert in Java, C++, Kubernetes and AWS", "8+ years of experience"])


@pytest.fixture
def jobs(make_job):
    """One strong match, one weak match (senior engineering), one filtered-out job."""
    common = dict(description_text=DESCRIPTION, salary_min=95000.0, salary_max=120000.0, salary_currency="USD",
                  salary_period="year", salary_is_base=1, work_arrangement="onsite")
    strong = make_job(required_qualifications=GOOD, **common)
    weak = make_job(title="Senior Software Engineer", title_normalized="senior software engineer",
                    required_qualifications=WEAK, **common)
    filtered = make_job(required_qualifications=GOOD, pipeline_state="filtered_out", filter_reason="location",
                        **common)
    return {"strong": strong, "weak": weak, "filtered": filtered}


def _row(db, job_id):
    return db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))


def _scores(db, job_id):
    return db.query_all("SELECT * FROM job_scores WHERE job_id = ? ORDER BY id", (job_id,))


def make_analysis(**kw) -> JobAnalysis:
    values = dict(summary="Analytics role.", role_family="business analysis", seniority="entry",
                  coding_intensity=CodingIntensity.LOW, ai_relevance=0.4, business_relevance=0.9,
                  analytics_relevance=0.9, product_relevance=0.1, fit_explanation="Good fit.",
                  model="test-model:1b", prompt_version="t1")
    values.update(kw)
    return JobAnalysis(**values)


# --------------------------------------------------------------------------- #
# repository
# --------------------------------------------------------------------------- #
def test_save_score_is_idempotent_and_flips_is_current(ctx, db, profile, jobs):
    row = _row(db, jobs["strong"])
    result = score_job(row, profile, ctx.config)
    first = score_repo.save_score(db, row, result, "p1", "c1")
    again = score_repo.save_score(db, row, result, "p1", "c1")
    assert first.created and not again.created and first.score_id == again.score_id
    assert len(_scores(db, jobs["strong"])) == 1
    other = score_repo.save_score(db, row, result, "p1", "c2")  # e.g. config changed
    rows = _scores(db, jobs["strong"])
    assert len(rows) == 2 and other.created
    assert [r["is_current"] for r in rows] == [0, 1]
    current = score_repo.current_score(db, jobs["strong"])
    assert current["id"] == other.score_id and current["config_hash"] == "c2"
    assert current["factors"][0]["name"] == "career_interest" and isinstance(current["shortlisted"], bool)
    # re-saving the old key makes it current again, still without duplicates
    score_repo.save_score(db, row, result, "p1", "c1")
    rows = _scores(db, jobs["strong"])
    assert len(rows) == 2 and [r["is_current"] for r in rows] == [1, 0]


def test_save_score_rejects_mismatched_job(ctx, db, profile, jobs):
    result = score_job(_row(db, jobs["strong"]), profile, ctx.config)
    with pytest.raises(PermanentError):
        score_repo.save_score(db, _row(db, jobs["weak"]), result, "p", "c")


def test_state_transitions_follow_the_state_machine(ctx, db, profile, jobs):
    strong = _row(db, jobs["strong"])
    saved = score_repo.save_score(db, strong, score_job(strong, profile, ctx.config), "p", "c")
    assert saved.pipeline_state == "shortlisted" and saved.state_changed  # discovered -> scored -> shortlisted
    weak = _row(db, jobs["weak"])
    assert score_repo.save_score(db, weak, score_job(weak, profile, ctx.config), "p", "c").pipeline_state == "scored"

    filtered = _row(db, jobs["filtered"])
    result = score_job(filtered, profile, ctx.config)
    kept = score_repo.save_score(db, filtered, result, "p", "c")
    assert kept.pipeline_state == "filtered_out" and not kept.state_changed and "filtered out" in kept.state_note
    revived = score_repo.save_score(db, filtered, result, "p", "c", allow_filtered=True)
    assert revived.pipeline_state == "shortlisted"
    assert _row(db, jobs["filtered"])["filter_reason"] is None


def test_jobs_with_applications_keep_their_state(ctx, db, profile, jobs):
    repo.create_application(db, jobs["strong"], actor="user")
    row = _row(db, jobs["strong"])
    saved = score_repo.save_score(db, row, score_job(row, profile, ctx.config), "p", "c")
    assert not saved.state_changed and "application" in saved.state_note
    assert _row(db, jobs["strong"])["pipeline_state"] == "discovered"
    assert score_repo.current_score(db, jobs["strong"]) is not None


# --------------------------------------------------------------------------- #
# service
# --------------------------------------------------------------------------- #
def test_score_jobs_scores_active_unfiltered_and_skips_unchanged(ctx, db, jobs, make_job):
    closed = make_job(listing_status="closed", description_text=DESCRIPTION)
    summary = score_jobs(ctx)
    assert isinstance(summary, ScoringSummary)
    assert summary.scored == 2 and summary.skipped_unchanged == 0 and summary.errors == []
    assert summary.shortlisted == 1
    assert summary.by_category == {"strong_match": 1, "weak_match": 1}
    assert not _scores(db, jobs["filtered"]) and not _scores(db, closed)
    assert _row(db, jobs["strong"])["pipeline_state"] == "shortlisted"
    assert _row(db, jobs["weak"])["pipeline_state"] == "scored"

    again = score_jobs(ctx)
    assert again.scored == 0 and again.skipped_unchanged == 2
    assert len(_scores(db, jobs["strong"])) == 1
    audit = db.query_all("SELECT * FROM audit_events WHERE action = 'jobs_scored'")
    assert len(audit) == 1  # nothing scored the second time -> no audit noise


def test_content_hash_change_makes_score_stale(ctx, db, jobs):
    score_jobs(ctx)
    db.update("jobs", jobs["weak"], {"content_hash": "changed", "description_text": DESCRIPTION + " Updated."})
    summary = score_jobs(ctx)
    assert summary.scored == 1 and summary.skipped_unchanged == 1
    rows = _scores(db, jobs["weak"])
    assert len(rows) == 2 and [r["is_current"] for r in rows] == [0, 1]
    assert rows[1]["content_hash"] == "changed"


def test_profile_and_config_changes_make_scores_stale(ctx, db, jobs, profile):
    score_jobs(ctx)
    ctx._profile = profile.model_copy(update={"skills": [*profile.skills, "Java"]})
    summary = score_jobs(ctx)
    assert summary.scored == 2
    current = score_repo.current_score(db, jobs["weak"])
    assert current["profile_hash"] == ctx.profile.profile_hash()
    assert any(m.startswith("Java (required; profile skill)") for m in current["matching_qualifications"])

    ctx.config.scoring.salary_target_base = 150000
    assert score_jobs(ctx).scored == 2
    ctx.config.scoring.max_llm_jobs_per_run = 3  # not a scoring input: no rescoring
    assert score_jobs(ctx).scored == 0
    assert score_repo.current_score(db, jobs["strong"])["config_hash"] == scoring_config_hash(ctx.config)


def test_rescore_limit_and_explicit_job_ids(ctx, db, jobs):
    first = score_jobs(ctx, limit=1)
    assert first.scored == 1 and first.deferred == 1
    forced = score_jobs(ctx, rescore=True)
    assert forced.scored == 2
    assert len(_scores(db, jobs["strong"])) == 1  # same inputs -> same UNIQUE row refreshed
    explicit = score_jobs(ctx, rescore=True, job_ids=[jobs["filtered"]])
    assert explicit.scored == 1
    assert _row(db, jobs["filtered"])["pipeline_state"] == "shortlisted"
    with pytest.raises(PermanentError):
        score_jobs(ctx, job_ids=[99999])


def test_analyzer_runs_only_for_top_rules_jobs(ctx, db, jobs):
    ctx.config.scoring.max_llm_jobs_per_run = 1
    calls = []

    def analyzer(job):
        calls.append(job["job_uid"])
        return make_analysis()

    summary = score_jobs(ctx, analyzer=analyzer)
    strong_uid = _row(db, jobs["strong"])["job_uid"]
    assert calls == [strong_uid]
    assert summary.llm_scored == 1 and summary.scored == 2
    rows = _scores(db, jobs["strong"])
    assert [(r["used_llm"], r["is_current"]) for r in rows] == [(0, 0), (1, 1)]
    assert rows[1]["scoring_version"] == "rules-1.0+llm" and rows[1]["llm_model"] == "test-model:1b"
    assert score_repo.current_score(db, jobs["weak"])["used_llm"] is False
    # unchanged next run: the LLM score stays current and the analyzer is not called again
    calls.clear()
    assert score_jobs(ctx, analyzer=analyzer).skipped_unchanged == 2 and calls == []


def test_analyzer_failure_falls_back_to_rules(ctx, db, jobs):
    def broken(job):
        raise RuntimeError("ollama crashed")

    summary = score_jobs(ctx, analyzer=broken)
    assert summary.scored == 2 and summary.llm_failed == 2 and summary.llm_scored == 0
    assert all("rules-based score kept" in e for e in summary.errors)
    errors = db.query_all("SELECT * FROM errors WHERE component = 'scoring:llm'")
    assert len(errors) == 2 and "ollama crashed" in errors[0]["message"]
    current = score_repo.current_score(db, jobs["strong"])
    assert current["used_llm"] is False and current["scoring_version"] == "rules-1.0"
    assert _row(db, jobs["strong"])["pipeline_state"] == "shortlisted"


def test_analyzer_none_or_invalid(ctx, db, jobs):
    unavailable = score_jobs(ctx, analyzer=lambda job: None)
    assert unavailable.llm_unavailable == 2 and unavailable.errors == []
    invalid = score_jobs(ctx, rescore=True, analyzer=lambda job: {"summary": "x"})  # fails JobAnalysis validation
    assert invalid.llm_failed == 2 and invalid.scored == 2


def test_score_task_via_task_runner(ctx, db, jobs):
    from job_agent.tasks import run_task

    result = run_task(ctx, "score")
    assert result.status == RunStatus.SUCCEEDED, result.error
    assert result.summary["scored"] == 2 and "rules-only" in result.summary["llm"]
    audit = db.query_one("SELECT * FROM audit_events WHERE action = 'jobs_scored'")
    assert audit["run_id"] is not None


# --------------------------------------------------------------------------- #
# queries + annotations
# --------------------------------------------------------------------------- #
def test_top_jobs_and_job_detail(ctx, db, jobs):
    score_jobs(ctx, job_ids=list(jobs.values()), rescore=True)  # explicit request also scores the filtered job
    db.update("jobs", jobs["filtered"], {"pipeline_state": "filtered_out"})
    top = score_repo.top_jobs(db)
    assert [t["id"] for t in top] == [jobs["strong"], jobs["weak"]]  # filtered-out excluded by default
    assert top[0]["overall_score"] >= top[1]["overall_score"]
    assert "description_text" not in top[0] and isinstance(top[0]["eligibility_flags"], list)
    assert top[0]["required_qualifications"][0] == "Proficiency in SQL and Python"
    assert [t["id"] for t in score_repo.top_jobs(db, category="weak_match")] == [jobs["weak"]]
    with pytest.raises(PermanentError):
        score_repo.top_jobs(db, category="great_match")

    assert jobs["filtered"] in [t["id"] for t in score_repo.top_jobs(db, include_filtered=True)]
    db.update("jobs", jobs["weak"], {"listing_status": "closed"})
    assert jobs["weak"] not in [t["id"] for t in score_repo.top_jobs(db)]
    assert jobs["weak"] in [t["id"] for t in score_repo.top_jobs(db, include_closed=True)]
    assert len(score_repo.top_jobs(db, limit=1, include_closed=True)) == 1

    uid = _row(db, jobs["strong"])["job_uid"]
    by_uid, by_id = score_repo.job_detail(db, uid), score_repo.job_detail(db, jobs["strong"])
    assert by_uid == by_id and by_uid["job"]["description_text"] == DESCRIPTION
    assert by_uid["score"]["factors"][1]["name"] == "skills" and by_uid["application"] is None
    assert score_repo.job_detail(db, str(jobs["strong"]))["job"]["id"] == jobs["strong"]
    assert score_repo.job_detail(db, "J-missing") is None
    repo.create_application(db, jobs["strong"], actor="user")
    assert score_repo.job_detail(db, uid)["application"]["status"] == "documents_pending"
    assert score_repo.top_jobs(db)[0]["application_status"] == "documents_pending"


def test_user_relevance_and_notes_are_audited(ctx, db, jobs):
    score_jobs(ctx)
    uid = _row(db, jobs["strong"])["job_uid"]
    assert score_repo.set_user_relevance(db, uid, "irrelevant")["user_relevance"] == "irrelevant"
    assert jobs["strong"] not in [t["id"] for t in score_repo.top_jobs(db)]
    assert jobs["strong"] in [t["id"] for t in score_repo.top_jobs(db, include_filtered=True)]
    assert score_repo.set_user_relevance(db, uid, None)["user_relevance"] is None
    with pytest.raises(PermanentError):
        score_repo.set_user_relevance(db, uid, "maybe")
    with pytest.raises(PermanentError):
        score_repo.set_user_relevance(db, "J-nope", "relevant")

    assert score_repo.set_user_note(db, uid, "Ask about the team")["user_notes"] == "Ask about the team"
    assert score_repo.set_user_note(db, uid, "Referral from Sam", append=True)["user_notes"] == \
        "Ask about the team\nReferral from Sam"
    actions = [r["action"] for r in db.query_all("SELECT action FROM audit_events WHERE entity_id = ?", (uid,))]
    assert actions == ["job_relevance_marked", "job_relevance_marked", "job_note_updated", "job_note_updated"]
    note_audit = db.query_one("SELECT details FROM audit_events WHERE action = 'job_note_updated'")
    assert "Ask about" not in note_audit["details"]
