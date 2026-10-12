from __future__ import annotations

import sqlite3
import threading

import pytest

from job_agent.database import Database, repo
from job_agent.database.migrations import apply_migrations, available_migrations
from job_agent.errors import ApprovalError, InvalidTransition
from job_agent.models import ApprovalType, PipelineState as S, RunStatus


def test_migrations_apply_once_and_are_recorded(tmp_path):
    path = tmp_path / "a.db"
    db = Database(path)
    assert db.scalar("SELECT MAX(version) FROM schema_migrations") == len(available_migrations())
    assert apply_migrations(db) == []  # idempotent
    db.close()
    db2 = Database(path)
    assert db2.scalar("SELECT COUNT(*) FROM schema_migrations") == len(available_migrations())
    assert db2.scalar("PRAGMA foreign_keys") == 1
    db2.close()


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "a.db"
    Database(path).close()
    raw = sqlite3.connect(path)
    raw.execute("INSERT INTO schema_migrations VALUES (999, 'future', 'x', '2030-01-01T00:00:00+00:00')")
    raw.commit()
    raw.close()
    with pytest.raises(Exception, match="newer"):
        Database(path)


def test_transaction_rollback(db):
    with pytest.raises(RuntimeError):
        with db.transaction():
            repo.set_state(db, "k", "v")
            raise RuntimeError("boom")
    assert repo.get_state(db, "k") is None


def test_nested_transaction_savepoint(db):
    with db.transaction():
        repo.set_state(db, "outer", "1")
        with pytest.raises(RuntimeError):
            with db.transaction():
                repo.set_state(db, "inner", "1")
                raise RuntimeError
    assert repo.get_state(db, "outer") == "1"
    assert repo.get_state(db, "inner") is None


def test_restart_persistence(config, make_job):
    job_id = make_job()
    db2 = Database(config.db_path)
    assert db2.scalar("SELECT employer FROM jobs WHERE id = ?", (job_id,)) == "Acme Analytics"
    db2.close()


def test_job_uniqueness_constraints(db, make_job):
    make_job(external_id="dup-1", job_url_canonical="https://x.test/1")
    with pytest.raises(sqlite3.IntegrityError):
        make_job(external_id="dup-1")  # same source + external id
    with pytest.raises(sqlite3.IntegrityError):
        make_job(external_id="other", job_url_canonical="https://x.test/1")  # same canonical URL


def test_foreign_keys_enforced(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.insert("application_events", {"application_id": 999, "event_type": "x", "actor": "system",
                                         "created_at": "2026-01-01T00:00:00+00:00"})


def test_unsafe_identifiers_rejected(db):
    with pytest.raises(Exception, match="unsafe"):
        db.insert("jobs; DROP TABLE jobs", {"a": 1})


def test_backup(db, tmp_path, make_job):
    make_job()
    dest = db.backup(tmp_path / "bk" / "b.db")
    copy = Database(dest, migrate=False)
    assert copy.scalar("SELECT COUNT(*) FROM jobs") == 1
    copy.close()


# ---------------- applications / state machine -------------------------- #
def test_one_application_per_job(db, make_job):
    job = make_job()
    a1 = repo.create_application(db, job, actor="system")
    a2 = repo.create_application(db, job, actor="system")
    assert a1 == a2
    assert db.scalar("SELECT COUNT(*) FROM applications") == 1


def test_valid_transition_records_event_and_audit(db, make_job):
    app_id = repo.create_application(db, make_job(), actor="system")
    prev = repo.transition_application(db, app_id, S.DOCUMENTS_READY, actor="system")
    assert prev == S.DOCUMENTS_PENDING
    events = db.query_all("SELECT * FROM application_events WHERE application_id = ? ORDER BY id", (app_id,))
    assert [e["to_status"] for e in events] == ["documents_pending", "documents_ready"]
    assert db.scalar("SELECT COUNT(*) FROM audit_events WHERE action = 'application_status_changed'") == 1


def test_invalid_transition_rejected(db, make_job):
    app_id = repo.create_application(db, make_job(), actor="system")
    with pytest.raises(InvalidTransition):
        repo.transition_application(db, app_id, S.OFFER_RECEIVED, actor="system")
    with pytest.raises(InvalidTransition):
        repo.transition_application(db, app_id, S.SHORTLISTED, actor="system")  # not an application state
    assert repo.get_application(db, app_id)["status"] == "documents_pending"


def test_submitted_requires_user_or_evidence(db, make_job):
    app_id = repo.create_application(db, make_job(), actor="system")
    repo.transition_application(db, app_id, S.DOCUMENTS_READY, actor="system")
    with pytest.raises(InvalidTransition):
        repo.transition_application(db, app_id, S.SUBMITTED, actor="system", evidence={"type": "page"})
    repo.transition_application(db, app_id, S.SUBMITTED, actor="user", details={"note": "applied manually"})
    row = repo.get_application(db, app_id)
    assert row["confirmation_status"] == "user_reported" and row["submitted_at"]


def _ready_for_approval(db, make_job):
    app_id = repo.create_application(db, make_job(), actor="system")
    repo.transition_application(db, app_id, S.DOCUMENTS_READY, actor="system")
    repo.transition_application(db, app_id, S.AWAITING_APPROVAL, actor="system")
    payload = {"employer": "Acme", "answers": [{"q": "Name", "a": "Test"}]}
    uid = repo.create_approval(db, ApprovalType.SUBMIT_APPLICATION, payload, summary="Submit to Acme",
                               actor="system", application_id=app_id)
    return app_id, uid, payload


def test_submission_requires_consumed_approval(db, make_job):
    app_id, uid, payload = _ready_for_approval(db, make_job)
    with pytest.raises(ApprovalError):  # no approval
        repo.transition_application(db, app_id, S.SUBMISSION_ATTEMPTED, actor="browser")
    with pytest.raises(ApprovalError):  # pending, not approved
        repo.transition_application(db, app_id, S.SUBMISSION_ATTEMPTED, actor="browser", approval_uid=uid,
                                    approved_payload=payload)
    assert repo.get_application(db, app_id)["status"] == "awaiting_approval"
    repo.decide_approval(db, uid, approve=True, actor="user")
    repo.transition_application(db, app_id, S.SUBMISSION_ATTEMPTED, actor="browser", approval_uid=uid,
                                approved_payload=payload)
    row = repo.get_application(db, app_id)
    assert row["status"] == "submission_attempted" and row["submission_attempts"] == 1
    assert repo.get_approval(db, uid)["status"] == "consumed"


def test_approval_cannot_be_reused(db, make_job):
    app_id, uid, payload = _ready_for_approval(db, make_job)
    repo.decide_approval(db, uid, approve=True, actor="user")
    repo.consume_approval(db, uid, application_id=app_id, actor="browser", current_payload=payload)
    with pytest.raises(ApprovalError):
        repo.consume_approval(db, uid, application_id=app_id, actor="browser", current_payload=payload)


def test_approval_bound_to_payload(db, make_job):
    app_id, uid, payload = _ready_for_approval(db, make_job)
    repo.decide_approval(db, uid, approve=True, actor="user")
    changed = {**payload, "answers": [{"q": "Name", "a": "Someone Else"}]}
    with pytest.raises(ApprovalError, match="changed"):
        repo.transition_application(db, app_id, S.SUBMISSION_ATTEMPTED, actor="browser", approval_uid=uid,
                                    approved_payload=changed)
    assert repo.get_approval(db, uid)["status"] == "approved"  # untouched


def test_only_humans_can_decide(db, make_job):
    _, uid, _ = _ready_for_approval(db, make_job)
    for actor in ("system", "browser", "email", "scheduler"):
        with pytest.raises(ApprovalError):
            repo.decide_approval(db, uid, approve=True, actor=actor)


def test_rejected_and_expired_approvals_unusable(db, make_job):
    app_id, uid, payload = _ready_for_approval(db, make_job)
    repo.decide_approval(db, uid, approve=False, actor="user", note="wrong resume")
    with pytest.raises(ApprovalError):
        repo.consume_approval(db, uid, application_id=app_id, actor="browser", current_payload=payload)
    uid2 = repo.create_approval(db, ApprovalType.SUBMIT_APPLICATION, {"v": 2}, summary="again", actor="system",
                                application_id=app_id)
    db.execute("UPDATE approval_requests SET expires_at = '2000-01-01T00:00:00+00:00' WHERE approval_uid = ?",
               (uid2,))
    with pytest.raises(ApprovalError):
        repo.decide_approval(db, uid2, approve=True, actor="user")
    assert repo.get_approval(db, uid2)["status"] == "expired"


def test_new_payload_supersedes_and_same_payload_is_idempotent(db, make_job):
    app_id, uid, payload = _ready_for_approval(db, make_job)
    again = repo.create_approval(db, ApprovalType.SUBMIT_APPLICATION, payload, summary="x", actor="system",
                                 application_id=app_id)
    assert again == uid
    new = repo.create_approval(db, ApprovalType.SUBMIT_APPLICATION, {"different": True}, summary="x",
                               actor="system", application_id=app_id)
    assert new != uid
    assert repo.get_approval(db, uid)["status"] == "superseded"
    assert db.scalar("SELECT COUNT(*) FROM approval_requests WHERE status = 'pending'") == 1


def test_expected_current_guards_concurrent_change(db, make_job):
    app_id = repo.create_application(db, make_job(), actor="system")
    with pytest.raises(InvalidTransition, match="expected"):
        repo.transition_application(db, app_id, S.DOCUMENTS_READY, actor="system",
                                    expected_current=S.IN_PROGRESS)


def test_job_state_transitions(db, make_job):
    job = make_job()
    assert repo.set_job_state(db, job, S.SCORED, actor="system")
    assert repo.set_job_state(db, job, S.FILTERED_OUT, actor="system", reason="too senior")
    assert db.scalar("SELECT filter_reason FROM jobs WHERE id = ?", (job,)) == "too senior"
    with pytest.raises(InvalidTransition):
        repo.set_job_state(db, job, S.SUBMITTED, actor="system")


# ---------------- runs, errors, locks, audit ---------------------------- #
def test_errors_are_redacted(db):
    err_id = repo.record_error(db, component="test", error=RuntimeError("token=abcdef1234567890 for me@x.com"))
    msg = db.scalar("SELECT message FROM errors WHERE id = ?", (err_id,))
    assert "abcdef1234567890" not in msg and "me@x.com" not in msg


def test_task_lock_prevents_overlap(db):
    assert repo.acquire_task_lock(db, "discover", "owner-a", 30)
    assert not repo.acquire_task_lock(db, "discover", "owner-b", 30)
    repo.release_task_lock(db, "discover", "owner-b")  # wrong owner: no effect
    assert not repo.acquire_task_lock(db, "discover", "owner-b", 30)
    repo.release_task_lock(db, "discover", "owner-a")
    assert repo.acquire_task_lock(db, "discover", "owner-b", 30)


def test_expired_lock_can_be_taken(db):
    assert repo.acquire_task_lock(db, "t", "a", 30)
    db.execute("UPDATE scheduled_tasks SET lock_expires_at = '2000-01-01T00:00:00+00:00' WHERE task_name = 't'")
    assert repo.acquire_task_lock(db, "t", "b", 30)


def test_concurrent_lock_acquisition_single_winner(config):
    results = []

    def worker(name):
        d = Database(config.db_path)
        results.append(repo.acquire_task_lock(d, "race", name, 30))
        d.close()

    Database(config.db_path).close()
    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1


def test_interrupted_runs_marked_on_startup(db):
    run_id, _ = repo.start_run(db, "discover", "scheduled")
    db.execute("UPDATE agent_runs SET host_pid = 999999 WHERE id = ?", (run_id,))
    assert repo.mark_interrupted_runs(db) == 1
    assert db.scalar("SELECT status FROM agent_runs WHERE id = ?", (run_id,)) == RunStatus.INTERRUPTED.value


def test_pause_resume_audited(db):
    repo.set_paused(db, True, actor="cli", reason="vacation")
    assert repo.is_paused(db)
    repo.set_paused(db, False, actor="cli")
    assert not repo.is_paused(db)
    assert db.scalar("SELECT COUNT(*) FROM audit_events WHERE action LIKE 'automation_%'") == 2
