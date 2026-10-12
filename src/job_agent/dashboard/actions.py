"""State-changing dashboard actions.

The dashboard never changes state directly except for two small, owned fields
(``jobs.user_relevance`` / ``jobs.user_notes``). Everything else goes through
the documented contracts of other modules, imported lazily so the dashboard
keeps working (and says "not available yet") when a module is absent:

* approvals  -> ``applications.service.decide`` (preferred) or ``repo.decide_approval``
* tasks      -> ``tasks.run_task`` for names in ``tasks.TASKS`` with ``dashboard_allowed``
* pause      -> ``repo.set_paused``;  errors -> ``repo.resolve_error``
* email      -> ``email.service.associate_email``
* follow-ups -> ``followups.service.set_followup_status``
"""

from __future__ import annotations

import importlib.util
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import TYPE_CHECKING, Any

from job_agent.database import repo
from job_agent.errors import JobAgentError, PermanentError
from job_agent.logging_config import get_logger
from job_agent.models import FollowupStatus

if TYPE_CHECKING:  # pragma: no cover
    from job_agent.context import AppContext
    from job_agent.database import Database
    from job_agent.tasks import TaskResult, TaskSpec

log = get_logger(__name__)

ACTOR = "dashboard"
#: Tasks offered as buttons on the Controls page (when present in ``tasks.TASKS``).
DASHBOARD_TASKS: tuple[str, ...] = ("discover_and_score", "report", "health", "followups", "email_sync")
FOLLOWUP_DASHBOARD_STATUSES = frozenset({FollowupStatus.APPROVED, FollowupStatus.COMPLETED,
                                         FollowupStatus.DISMISSED})
MAX_NOTES_CHARS = 5000
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class ActionRejected(PermanentError):
    """The request is invalid or not permitted (HTTP 400)."""


class NotFound(PermanentError):
    """The referenced record does not exist (HTTP 404)."""


class FeatureUnavailable(PermanentError):
    """The module behind this action is not installed / implemented yet."""


def module_available(module_name: str) -> bool:
    """True if ``module_name`` can be found (without importing the module itself)."""
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# Approvals
# --------------------------------------------------------------------------- #
def decide_approval(ctx: "AppContext", approval_uid: str, *, approve: bool, note: str | None,
                    expected_payload_hash: str) -> str:
    """Record the human decision. Returns which implementation handled it.

    ``expected_payload_hash`` is mandatory: a request that changed after it was
    shown cannot be approved blindly.
    """
    if not _HASH_RE.match(expected_payload_hash or ""):
        raise ActionRejected("missing or malformed expected payload hash; reload the approval page")
    try:
        from job_agent.applications.service import decide
    except ImportError:
        decide = None
    if decide is not None:
        decide(ctx, approval_uid, approve=approve, actor=ACTOR, note=note,
               expected_payload_hash=expected_payload_hash)
        return "applications.service"
    repo.decide_approval(ctx.db, approval_uid, approve=approve, actor=ACTOR, note=note,
                         expected_payload_hash=expected_payload_hash)
    return "repo"


# --------------------------------------------------------------------------- #
# Jobs (owned fields)
# --------------------------------------------------------------------------- #
def set_job_relevance(db: "Database", job_id: int, value: str | None) -> None:
    if value not in (None, "relevant", "irrelevant"):
        raise ActionRejected(f"invalid relevance {value!r}")
    with db.transaction():
        row = db.query_one("SELECT user_relevance FROM jobs WHERE id = ?", (job_id,))
        if row is None:
            raise NotFound(f"job {job_id} not found")
        db.execute("UPDATE jobs SET user_relevance = ? WHERE id = ?", (value, job_id))
        repo.audit(db, actor=ACTOR, action="job_relevance_set", entity_type="job", entity_id=job_id,
                   outcome="success", details={"from": row["user_relevance"], "to": value})


def set_job_notes(db: "Database", job_id: int, notes: str) -> None:
    text = (notes or "").replace("\r\n", "\n").strip()
    if len(text) > MAX_NOTES_CHARS:
        raise ActionRejected(f"notes are limited to {MAX_NOTES_CHARS} characters")
    with db.transaction():
        if db.query_one("SELECT 1 FROM jobs WHERE id = ?", (job_id,)) is None:
            raise NotFound(f"job {job_id} not found")
        db.execute("UPDATE jobs SET user_notes = ? WHERE id = ?", (text or None, job_id))
        repo.audit(db, actor=ACTOR, action="job_notes_updated", entity_type="job", entity_id=job_id,
                   outcome="success", details={"length": len(text)})


# --------------------------------------------------------------------------- #
# Email / follow-ups (contracts of other modules)
# --------------------------------------------------------------------------- #
def _ref(value: str | None) -> int | str | None:
    text = (value or "").strip()
    if not text:
        return None
    if not _REF_RE.match(text):
        raise ActionRejected("references must be an id or uid (letters, digits, '-', '_')")
    return int(text) if text.isdigit() else text


def associate_email(ctx: "AppContext", email_id: int, *, job_ref: str | None, application_ref: str | None,
                    apply_status: bool) -> Any:
    jref, aref = _ref(job_ref), _ref(application_ref)
    if jref is None and aref is None:
        raise ActionRejected("enter a job or application reference")
    if ctx.db.query_one("SELECT 1 FROM email_messages WHERE id = ?", (email_id,)) is None:
        raise NotFound(f"email {email_id} not found")
    try:
        from job_agent.email.service import associate_email as _associate
    except ImportError as exc:
        raise FeatureUnavailable("Email association is not available yet (email module not installed).") from exc
    return _associate(ctx, email_id, job_ref=jref, application_ref=aref, apply_status=apply_status)


def set_followup_status(ctx: "AppContext", followup_id: int, status: str) -> Any:
    try:
        target = FollowupStatus(status)
    except ValueError as exc:
        raise ActionRejected(f"invalid follow-up status {status!r}") from exc
    if target not in FOLLOWUP_DASHBOARD_STATUSES:
        raise ActionRejected(f"follow-ups cannot be set to {target.value} from the dashboard")
    if ctx.db.query_one("SELECT 1 FROM followups WHERE id = ?", (followup_id,)) is None:
        raise NotFound(f"follow-up {followup_id} not found")
    try:
        from job_agent.followups.service import set_followup_status as _set
    except ImportError as exc:
        raise FeatureUnavailable("Follow-up actions are not available yet (follow-ups module not installed).") \
            from exc
    return _set(ctx, followup_id, target, actor=ACTOR)


# --------------------------------------------------------------------------- #
# Pause / errors
# --------------------------------------------------------------------------- #
def set_paused(db: "Database", paused: bool, reason: str | None = None) -> None:
    repo.set_paused(db, paused, actor=ACTOR, reason=(reason or "").strip()[:200] or None)


def resolve_error(db: "Database", error_id: int) -> bool:
    return repo.resolve_error(db, error_id, actor=ACTOR)


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #
def task_available(spec: "TaskSpec") -> bool:
    return module_available(spec.target.partition(":")[0])


def triggerable_spec(name: str) -> "TaskSpec":
    """The TaskSpec for ``name`` if the dashboard may run it; raises ActionRejected otherwise."""
    from job_agent.tasks import TASKS

    spec = TASKS.get(name) if isinstance(name, str) else None
    if spec is None:
        raise ActionRejected("unknown task")
    if not spec.dashboard_allowed:
        raise ActionRejected(f"task {spec.name!r} may not be triggered from the dashboard")
    if not task_available(spec):
        raise ActionRejected(f"task {spec.name!r} is not available yet in this installation")
    return spec


def dashboard_tasks() -> list[dict[str, Any]]:
    from job_agent.tasks import TASKS

    out = []
    for name in DASHBOARD_TASKS:
        spec = TASKS.get(name)
        if spec is None or not spec.dashboard_allowed:
            continue
        out.append({"name": name, "description": spec.description, "available": task_available(spec)})
    return out


def retry_allowed(run: dict[str, Any]) -> bool:
    if run.get("status") != "failed":
        return False
    try:
        spec = triggerable_spec(run.get("task_name", ""))
    except ActionRejected:
        return False
    return spec.retry_safe


def task_locked(db: "Database", name: str) -> bool:
    from job_agent.utils.time import utcnow_iso

    return db.query_one(
        "SELECT 1 FROM scheduled_tasks WHERE task_name = ? AND lock_owner IS NOT NULL "
        "AND (lock_expires_at IS NULL OR lock_expires_at > ?)", (name, utcnow_iso())
    ) is not None


def _run_in_background(ctx: "AppContext", name: str, attempt: int) -> "TaskResult | None":
    from job_agent.tasks import run_task

    try:
        result = run_task(ctx, name, trigger=ACTOR, attempt=attempt, ignore_pause=True)
        log.info("dashboard-triggered task %s finished: %s", name, result.status.value)
        return result
    except Exception as exc:  # run_task records task failures itself; this is a runner-level failure
        log.exception("dashboard-triggered task %s could not run", name)
        try:
            repo.record_error(ctx.db, component="dashboard:task_runner", error=exc, task_name=name)
        except JobAgentError:
            log.exception("could not record task runner error for %s", name)
        return None
    finally:
        ctx.close()


class TaskRunner:
    """One background worker thread; requests return immediately. The DB task lock prevents overlap."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dashboard-task")
        self._futures: list[Future[Any]] = []
        self._lock = threading.Lock()

    def submit(self, ctx: "AppContext", name: str, *, attempt: int = 1) -> Future[Any]:
        spec = triggerable_spec(name)
        worker_ctx = ctx.fork()  # own DB connection for the worker thread
        future = self._executor.submit(_run_in_background, worker_ctx, spec.name, attempt)
        with self._lock:
            self._futures = [f for f in self._futures if not f.done()] + [future]
        return future

    def pending(self) -> int:
        with self._lock:
            return sum(1 for f in self._futures if not f.done())

    def wait(self, timeout: float | None = None) -> bool:
        """Block until queued work finishes (used by tests and shutdown). True if all done."""
        with self._lock:
            futures = list(self._futures)
        _, not_done = wait(futures, timeout=timeout)
        return not not_done

    def shutdown(self, *, wait_for_tasks: bool = False) -> None:
        self._executor.shutdown(wait=wait_for_tasks, cancel_futures=not wait_for_tasks)
