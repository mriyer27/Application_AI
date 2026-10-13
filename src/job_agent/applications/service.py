"""Application workflow: prepare -> approve -> (browser or manual) submit -> track.

Invariants enforced here and in ``database.repo`` (not by prompts):

* preparing never clicks a submit button; form posts are blocked by the
  browser guard unless armed for the one approved click;
* only ``verified`` answers are entered: profile values, verified answers,
  your own answers and your stored documents (sha256 re-checked on upload);
* final submission needs an approval decided by you and consumed exactly once
  (``SUBMISSION_ATTEMPTED`` is recorded *before* the click, with the rebuilt
  payload whose hash must equal the approved one);
* browser submission is off unless ``applications.browser_submission_enabled``;
* an unknown outcome is never retried automatically: once the click may have
  happened (including Ctrl+C / a stop signal / a killed process) the outcome is
  recorded as unknown, the browser never submits that application again, and
  preparing it again needs your explicit ``--confirm-not-submitted``;
* documents you rejected are never uploaded or offered for approval.
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import threading
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator

from job_agent.applications import documents as docs
from job_agent.applications import repository as arepo
from job_agent.applications.field_mapping import (
    Drafter,
    MappedAnswer,
    MappingInputs,
    SuggestedAnswer,
    apply_drafts,
    map_fields,
)
from job_agent.applications.payload import (
    FORM_PREPARED_EVENT,
    REJECTED_DOCUMENT_STATUS,
    approval_view,
    build_payload,
    submission_blockers,
    summary_line,
    usable_document,
)
from job_agent.browser.detection import detect_closed_listing
from job_agent.browser.errors import ChallengeDetected, FillError, NavigationBlocked, PageUnavailable
from job_agent.browser.forms import FALSY, TRUTHY, FormField, match_option, norm, value_matches
from job_agent.browser.policy import NavigationPolicy
from job_agent.browser.session import (
    ApplicationPage,
    BrowserFactory,
    BrowserOptions,
    SubmitOutcome,
    default_factory,
    ensure_no_challenge,
    poll_outcome,
)
from job_agent.database import Database, repo
from job_agent.database.connection import from_json
from job_agent.errors import (
    ApprovalError,
    HumanActionRequired,
    InvalidTransition,
    JobAgentError,
    PermanentError,
    PermissionDenied,
    RecoverableError,
    UnknownOutcome,
)
from job_agent.logging_config import get_logger
from job_agent.models import ALLOWED_TRANSITIONS, ApprovalStatus, ApprovalType, ErrorKind, Permission, PipelineState
from job_agent.security.permissions import require_permission
from job_agent.security.sanitize import ensure_within, safe_component
from job_agent.utils.time import parse_iso, utcnow, utcnow_iso

if TYPE_CHECKING:  # pragma: no cover
    import sqlite3

    from job_agent.context import AppContext

log = get_logger(__name__)
S = PipelineState

PREPARABLE = frozenset({S.DOCUMENTS_READY, S.IN_PROGRESS, S.AWAITING_APPROVAL, S.NEEDS_MANUAL_ACTION, S.FAILED})
ANSWERABLE = PREPARABLE
MAX_LLM_DRAFTS = 3
#: A submission still "attempted" after this long is treated as interrupted even if
#: its process cannot be checked (another machine, or no pid recorded).
STUCK_SUBMISSION_MINUTES = 15
#: Event you record (``apply --confirm-not-submitted``) after checking that an
#: unknown-outcome submission did NOT reach the employer.
CONFIRMED_NOT_SUBMITTED_EVENT = "confirmed_not_submitted"
UNKNOWN_OUTCOME_EVENT = "submission_outcome_unknown"

__all__ = [
    "PreparationResult", "SubmissionResult", "DecisionResult", "SubmissionBlocked", "SubmissionInProgress",
    "prepare_application", "set_answer", "build_payload", "decide", "submit_approved", "mark_submitted", "withdraw",
    "set_status", "add_note", "list_applications", "application_detail", "approval_view", "pending_approvals",
    "has_unknown_outcome", "recover_interrupted_submission",
]


class SubmissionBlocked(HumanActionRequired):
    """The browser will not submit this application (nothing was sent)."""


class SubmissionInProgress(HumanActionRequired):
    """Another live process is submitting this application right now."""


@dataclass
class PreparationResult:
    application_uid: str
    job_uid: str
    status: PipelineState
    message: str
    approval_uid: str | None = None
    url: str | None = None
    fields_found: int = 0
    filled: int = 0
    requires_review: int = 0
    legal_declarations: int = 0
    unanswered_required: int = 0
    validation_errors: list[str] = field(default_factory=list)
    screenshot: str | None = None
    challenge: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "status": self.status.value}


@dataclass
class SubmissionResult:
    application_uid: str
    approval_uid: str
    status: PipelineState
    outcome: str  # submitted | failed | unknown | not_submitted
    message: str
    evidence: dict[str, Any] | None = None
    validation_errors: list[str] = field(default_factory=list)
    screenshot: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "status": self.status.value}


@dataclass
class DecisionResult:
    approval_uid: str
    approved: bool
    application_uid: str | None  # None for non-application approvals (e.g. follow-ups)
    status: PipelineState | None
    next_action: str


class _Manual(Exception):
    """Internal: the form cannot be prepared automatically (reason for the human)."""

    def __init__(self, reason: str, kind: str = "manual") -> None:
        super().__init__(reason)
        self.reason = reason
        self.kind = kind


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _policy(job: "sqlite3.Row") -> NavigationPolicy:
    return NavigationPolicy.for_job(job_url=job["job_url"], application_url=job["application_url"],
                                    source_name=job["source_name"], source_identifier=job["source_identifier"])


def _target_url(app: "sqlite3.Row", job: "sqlite3.Row", policy: NavigationPolicy) -> str | None:
    """The job's CURRENT application / listing URL (discovery keeps them up to date); the URL copied
    onto the application at an earlier ``prepare`` is only a last resort."""
    candidates = list(dict.fromkeys(u for u in (job["application_url"], job["job_url"], app["application_url"]) if u))
    for url in candidates:
        if policy.initial_problem(url) is None:
            return url
    return candidates[0] if candidates else None


def _screenshot(ctx: "AppContext", page: ApplicationPage, app_uid: str, name: str) -> str | None:
    cfg = ctx.config
    if not cfg.applications.screenshots:
        return None
    base = cfg.data_dir / "screenshots"
    path = ensure_within(base, Path(safe_component(app_uid, max_len=40)) / f"{safe_component(name)}.png")
    try:
        page.screenshot(path)
    except JobAgentError as exc:
        repo.record_error(ctx.db, component="applications:screenshot", error=exc)
        return None
    return path.relative_to(base.resolve().parent).as_posix()


def _verified_variants(db: Database, documents_dir: Path, document_id: int | None, *,
                       job_uid: str) -> list[docs.DocumentRef]:
    """Usable files of the application's document: never a version you rejected.

    A rejected version falls back to the latest version you did not reject; if
    there is none, ``HumanActionRequired`` (regenerate with ``job-agent prepare``).
    """
    stored = docs.get_document(db, document_id)
    if stored is None:
        return []
    base = usable_document(db, stored)
    if base is None:
        label = {"resume": "résumé", "cover_letter": "cover letter"}.get(stored.doc_type, stored.doc_type)
        raise HumanActionRequired(
            f"you rejected the {label} {stored.file_name} and there is no other version you have not rejected; "
            "it will never be uploaded",
            human_action=f"run job-agent prepare {job_uid} to generate a new version, review it, then run "
                         f"job-agent apply {job_uid} again",
        )
    good, first_error = [], None
    for variant in docs.variants(db, base):
        if variant.status == REJECTED_DOCUMENT_STATUS:
            continue
        try:
            docs.verify_file(variant, documents_dir)
            good.append(variant)
        except HumanActionRequired as exc:
            first_error = first_error or exc
    if not good and first_error is not None:
        raise first_error
    return good


def _cover_text(variants: list[docs.DocumentRef], documents_dir: Path) -> tuple[str | None, docs.DocumentRef | None]:
    for variant in sorted(variants, key=lambda d: {"md": 0, "docx": 1}.get(d.format, 9)):
        text = docs.read_text(variant, documents_dir)
        if text:
            return text, variant
    return None, None


def _eligibility_reference(ctx: "AppContext") -> dict[str, Any]:
    wa = ctx.profile.work_authorization.model_dump(mode="json")
    wa.pop("notes", None)  # free-text notes are never copied anywhere
    return wa


def _make_drafter(ctx: "AppContext") -> tuple[Drafter | None, Callable[[], None]]:
    """Optional local-LLM drafter for open-ended questions (``job_agent.llm.generation.draft_answer``)."""
    noop: Callable[[], None] = lambda: None  # noqa: E731
    if not ctx.config.llm.enabled:
        return None, noop
    try:
        from job_agent.llm.client import get_client
        from job_agent.llm.generation import draft_answer
    except ImportError as exc:
        log.info("answer drafting unavailable: %s", exc)
        return None, noop
    try:
        client = get_client(ctx)
    except JobAgentError as exc:
        repo.record_error(ctx.db, component="applications:draft", error=exc)
        return None, noop
    if client is None:
        return None, noop
    from job_agent.security.untrusted import clean_untrusted

    facts = ctx.profile.all_facts()
    valid_ids = {f.id for f in facts}
    skills = list(ctx.profile.skills)

    def drafter(question: str) -> SuggestedAnswer | None:
        # The label comes from the employer's page (untrusted). draft_answer wraps it with
        # security.untrusted.wrap_untrusted itself and refuses legal/eligibility questions.
        try:
            result = draft_answer(client, question=clean_untrusted(question, max_chars=1500), facts=facts,
                                  profile_skills=skills)
        except JobAgentError as exc:
            repo.record_error(ctx.db, component="applications:draft", error=exc)
            return None
        if result is None:
            return None
        answer = str(getattr(result, "answer", "") or "").strip()
        fact_ids = tuple(str(x) for x in (getattr(result, "fact_ids", None) or ()))
        if not answer or any(fid not in valid_ids for fid in fact_ids):
            return None  # drafts must cite verified facts only
        confidence = getattr(result, "confidence", None)
        return SuggestedAnswer(answer[:4000], fact_ids, None if confidence is None else str(confidence))

    def close() -> None:
        closer = getattr(client, "close", None)
        if callable(closer):
            closer()

    return drafter, close


def _manual_next_action(app_uid: str, url: str | None, reason: str, resume_file: str | None) -> str:
    where = f" at {url}" if url else " on the employer's site"
    resume = f" with your résumé {resume_file}" if resume_file else ""
    return (f"{reason}. Apply manually{where}{resume}, using the prepared answers "
            f"(`job-agent applications show {app_uid}`), then run "
            f"`job-agent applications mark-submitted {app_uid}`.")


def _fill_document(ctx: "AppContext", page: ApplicationPage, fld: FormField, source: str) -> str:
    if not source.startswith("document:") or not source[9:].isdigit():
        raise FillError(f"{fld.key}: no stored document for this upload")
    doc = docs.get_document(ctx.db, int(source[9:]))
    if doc is None:
        raise FillError(f"{fld.key}: document {source} no longer exists")
    if doc.status == REJECTED_DOCUMENT_STATUS:
        raise FillError(f"{fld.key}: {doc.file_name} was rejected by you; it is never uploaded")
    path = docs.verify_file(doc, ctx.config.documents_dir)  # sha256 re-verified right before upload
    page.attach_file(fld, path)
    return doc.file_name


def _enter(ctx: "AppContext", page: ApplicationPage, fld: FormField, value: str, source: str | None) -> None:
    """Enter one value and verify it reads back; raises ``FillError``."""
    expected = value
    if fld.is_file:
        expected = _fill_document(ctx, page, fld, source or "")
    else:
        page.fill(fld, value)
    actual = page.read_value(fld)
    if not value_matches(fld, expected, actual):
        raise FillError(f"{fld.key}: value did not stick (page shows {actual!r})")


def _fill_prepared(ctx: "AppContext", page: ApplicationPage, mapped: list[MappedAnswer]) -> tuple[int, dict[str, str]]:
    filled, failures = 0, {}
    for m in mapped:
        value = m.fill_value
        if value is None:
            continue
        try:
            _enter(ctx, page, m.field, value, m.source)
            filled += 1
        except FillError as exc:
            failures[m.key] = f"could not be entered automatically ({exc}); enter it yourself or set it with " \
                              f"`job-agent approvals answer`"
    return filled, failures


def _review_next_action(payload: dict[str, Any], approval_uid: str) -> str:
    parts = []
    if payload["legal_declarations"]:
        parts.append(f"{len(payload['legal_declarations'])} legal declaration(s)")
    if payload["requires_review"]:
        parts.append(f"{len(payload['requires_review'])} item(s) needing your answer")
    if payload["unanswered_required"]:
        parts.append(f"{len(payload['unanswered_required'])} required field(s) unanswered")
    if payload["validation_errors"]:
        parts.append("validation errors shown by the form")
    detail = f" ({', '.join(parts)})" if parts else ""
    return (f"Review approval {approval_uid}{detail}: `job-agent approvals show {approval_uid}`, answer open items "
            f"with `job-agent approvals answer`, then approve or reject it.")


def _close_live_approvals(db: Database, application_id: int, reason: str) -> int:
    """You recorded the outcome yourself: no pending OR approved-but-unused request may stay usable."""
    repo.expire_approvals(db)
    return repo.cancel_live_approvals(db, application_id=application_id, actor="user", reason=reason)


# --------------------------------------------------------------------------- #
# Unknown outcomes and interrupted submissions
# --------------------------------------------------------------------------- #
def _unknown_outcome_marks(db: Database, application_id: int) -> list["sqlite3.Row"]:
    """Every record that a submission's outcome became unknown: the browser's own event, or ANY move
    out of ``submission_attempted`` to ``needs_manual_action`` (e.g. ``repo.recover_stuck_submissions``)."""
    return db.query_all(
        "SELECT id FROM application_events WHERE application_id = ? AND (event_type = ? OR "
        "(from_status = ? AND to_status = ?)) ORDER BY id",
        (application_id, UNKNOWN_OUTCOME_EVENT, S.SUBMISSION_ATTEMPTED.value, S.NEEDS_MANUAL_ACTION.value),
    )


def has_unknown_outcome(db: Database, application_id: int) -> bool:
    """A submission of this application may have reached the employer without us knowing (ever)."""
    return bool(_unknown_outcome_marks(db, application_id))


def _unacknowledged_unknown_outcome(db: Database, application_id: int) -> bool:
    """An unknown outcome you have not yet confirmed as "not submitted" (``apply --confirm-not-submitted``)."""
    marks = _unknown_outcome_marks(db, application_id)
    if not marks:
        return False
    confirmed = db.scalar("SELECT MAX(id) FROM application_events WHERE application_id = ? AND event_type = ?",
                          (application_id, CONFIRMED_NOT_SUBMITTED_EVENT))
    return confirmed is None or int(marks[-1]["id"]) > int(confirmed)


def _unknown_outcome_help(app_uid: str, job_uid: str) -> str:
    return (f"Check your email and the employer's portal first. If it went through, run `job-agent applications "
            f"mark-submitted {app_uid}` (or `job-agent applications set-status {app_uid} confirmation_received`). "
            f"If you are sure it was NOT submitted, run `job-agent apply {job_uid} --confirm-not-submitted`, or "
            f"close it with `job-agent applications set-status {app_uid} closed`.")


def _attempt_owner(db: Database, application_id: int) -> dict[str, Any]:
    """Details of the status change that started the current submission attempt (url, approval, pid, host)."""
    row = db.query_one("SELECT details FROM application_events WHERE application_id = ? AND to_status = ? "
                       "ORDER BY id DESC LIMIT 1", (application_id, S.SUBMISSION_ATTEMPTED.value))
    details = from_json(row["details"], {}) if row else {}
    return details if isinstance(details, dict) else {}


def _attempt_abandoned(app: "sqlite3.Row", owner: dict[str, Any]) -> bool:
    """The process that clicked submit is gone (same machine, pid not running), or the attempt is stale."""
    pid, host = owner.get("pid"), owner.get("host")
    if isinstance(pid, int) and host == socket.gethostname():
        if pid != os.getpid() and not repo.pid_alive(pid):
            return True
    started = parse_iso(app["status_updated_at"])
    return started is None or utcnow() - started > timedelta(minutes=STUCK_SUBMISSION_MINUTES)


def recover_interrupted_submission(ctx: "AppContext", application_ref: str | int) -> bool:
    """Record an unknown outcome for a submission whose process stopped before recording one.

    Returns True if the application was moved to ``needs_manual_action`` (with an
    ``UnknownOutcome`` error); False if it is not stuck in ``submission_attempted``.
    Raises ``SubmissionInProgress`` while the submitting process is still running.
    Never retries anything.
    """
    db = ctx.db
    app = arepo.resolve_application(db, application_ref)
    if app["status"] != S.SUBMISSION_ATTEMPTED.value:
        return False
    app_id, app_uid = int(app["id"]), str(app["application_uid"])
    owner = _attempt_owner(db, app_id)
    if not _attempt_abandoned(app, owner):
        raise SubmissionInProgress(
            f"a browser submission of {app_uid} is still in progress (process {owner.get('pid', '?')})",
            human_action=("Wait for `job-agent applications submit` to finish. If that process is stuck, stop it; "
                          f"its outcome is then recorded as unknown (or after {STUCK_SUBMISSION_MINUTES} minutes)."),
        )
    job = arepo.get_job(db, int(app["job_id"]))
    url = str(owner.get("url") or app["application_url"] or "")
    outcome = SubmitOutcome("interrupted", url, detail="the process that clicked submit stopped before recording "
                                                       "the outcome")
    _record_outcome(ctx, app_id, app_uid, str(owner.get("approval_uid") or ""), job, outcome, None)
    log.warning("submission of %s was interrupted; recorded as an unknown outcome", app_uid)
    return True


def _recover_for_user_action(ctx: "AppContext", app: "sqlite3.Row", target: PipelineState) -> "sqlite3.Row":
    """Before you record an outcome the state machine does not allow from ``submission_attempted``
    (withdraw, close, rejected, ...), an abandoned attempt is first recorded as an unknown outcome."""
    attempted = app["status"] == S.SUBMISSION_ATTEMPTED.value
    if attempted and target not in ALLOWED_TRANSITIONS[S.SUBMISSION_ATTEMPTED]:
        recover_interrupted_submission(ctx, int(app["id"]))
        return arepo.resolve_application(ctx.db, int(app["id"]))
    return app


def _main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


@contextlib.contextmanager
def _stop_signals_raise() -> Iterator[None]:
    """While waiting on the employer's page after the click, SIGTERM/SIGHUP/SIGBREAK (default action:
    die silently) raise ``SystemExit`` instead, so the outcome is still recorded as unknown.
    Only in the main thread and only for signals still at their default handler."""
    previous: dict[int, Any] = {}
    if _main_thread():
        def _raise(signum: int, frame: Any) -> None:
            raise SystemExit(128 + signum)

        for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                if signal.getsignal(sig) is signal.SIG_DFL:
                    previous[sig] = signal.signal(sig, _raise)
            except (ValueError, OSError):  # pragma: no cover - platform specific
                continue
    try:
        yield
    finally:
        for sig, handler in previous.items():
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(sig, handler)


@contextlib.contextmanager
def _signals_deferred() -> Iterator[None]:
    """Recording the outcome must not be cut short by a second Ctrl+C / stop signal (main thread only)."""
    previous: dict[int, Any] = {}
    if _main_thread():
        def _later(signum: int, frame: Any) -> None:
            log.warning("signal %s received while recording the submission outcome; finishing that first", signum)

        for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                previous[sig] = signal.signal(sig, _later)
            except (ValueError, OSError):  # pragma: no cover - platform specific
                continue
    try:
        yield
    finally:
        for sig, handler in previous.items():
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(sig, handler)


# --------------------------------------------------------------------------- #
# Prepare
# --------------------------------------------------------------------------- #
def prepare_application(ctx: "AppContext", job_ref: str | int, *, browser_factory: BrowserFactory | None = None,
                        headless: bool | None = None, confirm_not_submitted: bool = False) -> PreparationResult:
    """Open the official form, fill only verified answers, never submit, and request approval.

    After a submission with an unknown outcome this refuses (it may already have
    reached the employer) unless you pass ``confirm_not_submitted=True`` after
    checking your email / the employer's portal; that confirmation is recorded.
    """
    require_permission(ctx.config, Permission.PREPARE_APPLICATIONS)
    db, cfg = ctx.db, ctx.config
    job = arepo.resolve_job(db, job_ref)
    job_uid = str(job["job_uid"])
    app = repo.get_application_for_job(db, int(job["id"]))
    if app is None or not app["resume_document_id"]:
        raise HumanActionRequired(
            f"job {job_uid} has no prepared résumé yet",
            human_action=f"run job-agent prepare {job_uid} first",
        )
    app_id, app_uid = int(app["id"]), str(app["application_uid"])
    if app["status"] == S.SUBMISSION_ATTEMPTED.value and recover_interrupted_submission(ctx, app_id):
        app = repo.get_application(db, app_id)
        assert app is not None
    status = S(app["status"])
    if status not in PREPARABLE:
        raise InvalidTransition(
            f"application {app_uid} is {status.value}; it cannot be prepared again",
            human_action=("Run `job-agent prepare` to regenerate documents first." if status == S.DOCUMENTS_PENDING
                          else f"See `job-agent applications show {app_uid}`."),
        )
    unacknowledged = _unacknowledged_unknown_outcome(db, app_id)
    if unacknowledged and not confirm_not_submitted:
        raise HumanActionRequired(
            f"a browser submission of {app_uid} has an UNKNOWN outcome: it may already have reached the employer, "
            "so it is not prepared again",
            human_action=_unknown_outcome_help(app_uid, job_uid),
        )
    profile = ctx.profile
    resume_variants = _verified_variants(db, cfg.documents_dir, app["resume_document_id"], job_uid=job_uid)
    if not resume_variants:
        raise HumanActionRequired(f"résumé document for {app_uid} not found",
                                  human_action=f"run job-agent prepare {job_uid} first")
    cover_variants = _verified_variants(db, cfg.documents_dir, app["cover_letter_document_id"], job_uid=job_uid)
    cover_text, cover_text_doc = _cover_text(cover_variants, cfg.documents_dir)
    policy = _policy(job)
    url = _target_url(app, job, policy)

    if unacknowledged:
        with db.transaction():
            repo.add_application_event(db, app_id, CONFIRMED_NOT_SUBMITTED_EVENT, actor="user",
                                       details={"note": "you checked that the earlier submission with an unknown "
                                                        "outcome did not reach the employer"})
            repo.audit(db, actor="user", action="application_confirmed_not_submitted", entity_type="application",
                       entity_id=app_id, outcome="success")
    if status != S.IN_PROGRESS:
        repo.transition_application(db, app_id, S.IN_PROGRESS, actor="system", expected_current=status,
                                    next_action="Preparing the application form in the browser")

    def manual(reason: str, kind: str = "manual", *, challenge: str | None = None) -> PreparationResult:
        next_action = _manual_next_action(app_uid, url, reason, resume_variants[0].file_name)
        with db.transaction():
            repo.add_application_event(db, app_id, "manual_action_required", actor="system",
                                       details={"reason": reason, "kind": kind})
            if url and policy.initial_problem(url) is None:
                arepo.update_application(db, app_id, {"application_url": url})
            repo.transition_application(db, app_id, S.NEEDS_MANUAL_ACTION, actor="system", details={"reason": reason},
                                        next_action=next_action, expected_current=S.IN_PROGRESS)
        return PreparationResult(app_uid, str(job["job_uid"]), S.NEEDS_MANUAL_ACTION, next_action, url=url,
                                 challenge=challenge)

    if job["listing_status"] != "active" or job["pipeline_state"] == S.CLOSED.value:
        return manual(f"The listing is marked {job['listing_status']}; check it on the employer's site", "closed")
    if not url:
        return manual("No application URL is stored for this job", "no_url")
    problem = policy.initial_problem(url)
    if problem:
        return manual(f"Refusing to open {url}: {problem}", "url_refused")

    inputs = MappingInputs(profile=profile, resume_variants=resume_variants, cover_letter_variants=cover_variants,
                           cover_letter_text=cover_text, cover_letter_text_doc=cover_text_doc,
                           user_answers=arepo.user_answers(db, app_id))
    factory = browser_factory or default_factory
    options = BrowserOptions.from_config(cfg, headless=headless)
    try:
        with factory(options) as browser:
            page = browser.open_application(url, policy)
            snap = page.snapshot()
            closed = detect_closed_listing(snap)
            if closed:
                raise _Manual(f"The listing appears closed ({closed})", "closed")
            fields = page.inspect_form()
            if not fields:
                raise _Manual("No application form was found on the page (it may need a login, be inside an "
                              "embedded frame, or be built in a way this tool cannot read)", "no_form")
            mapped = map_fields(fields, inputs)
            filled, failures = _fill_prepared(ctx, page, mapped)
            ensure_no_challenge(page)
            page.check_navigation()
            validation_errors = page.validation_errors()
            shot = _screenshot(ctx, page, app_uid, "prepared")
            blocked_leaks = list(getattr(page, "blocked_leaks", None) or [])[:20]
    except ChallengeDetected as exc:
        repo.record_error(db, component="applications:prepare", error=exc, application_id=app_id, job_id=job["id"])
        return manual(f"A {exc.challenge_kind.replace('_', ' ')} blocks automation ({exc.detail}); it was not "
                      "bypassed", exc.challenge_kind, challenge=exc.challenge_kind)
    except NavigationBlocked as exc:
        repo.record_error(db, component="applications:prepare", error=exc, application_id=app_id, job_id=job["id"])
        return manual(f"The page tried to leave the official site ({exc.url}: {exc.reason})", "navigation_blocked")
    except _Manual as exc:
        return manual(exc.reason, exc.kind)
    except (PageUnavailable, HumanActionRequired, RecoverableError, PermanentError) as exc:
        repo.record_error(db, component="applications:prepare", error=exc, application_id=app_id, job_id=job["id"])
        return manual(f"Automatic preparation failed ({exc})", "automation_failed")
    except Exception as exc:
        repo.record_error(db, component="applications:prepare", error=exc, application_id=app_id, job_id=job["id"])
        manual(f"Unexpected error while preparing the form ({type(exc).__name__})", "error")
        raise

    drafter, close_drafter = _make_drafter(ctx)
    try:
        mapped = apply_drafts(mapped, drafter, max_drafts=MAX_LLM_DRAFTS)
    finally:
        close_drafter()

    with db.transaction():
        arepo.save_mapped_answers(db, app_id, mapped)
        for key, reason in failures.items():
            arepo.mark_fill_failed(db, app_id, key, reason)
        repo.add_application_event(db, app_id, FORM_PREPARED_EVENT, actor="browser", details={
            "fields": [m.field.describe() for m in mapped],
            "validation_errors": validation_errors,
            "screenshot": shot,
            "eligibility_reference": _eligibility_reference(ctx),
            "filled": filled,
            "fill_failures": failures,
            "fields_found": len(fields),
            "blocked_data_requests": blocked_leaks,
        })
        if blocked_leaks:
            repo.audit(db, actor="browser", action="data_leak_blocked", entity_type="application", entity_id=app_id,
                       outcome="blocked", details={"requests": blocked_leaks})
        arepo.update_application(db, app_id, {"prepared_at": utcnow_iso(), "application_url": url})
        payload = build_payload(db, app_id)
        approval_uid = repo.create_approval(db, ApprovalType.SUBMIT_APPLICATION, payload, summary=summary_line(payload),
                                            actor="system", application_id=app_id,
                                            expires_in_hours=cfg.applications.approval_expiry_hours)
        next_action = _review_next_action(payload, approval_uid)
        repo.transition_application(db, app_id, S.AWAITING_APPROVAL, actor="system", expected_current=S.IN_PROGRESS,
                                    details={"approval_request": approval_uid}, next_action=next_action)
        repo.audit(db, actor="system", action="application_prepared", entity_type="application", entity_id=app_id,
                   outcome="success", details={"fields": len(fields), "filled": filled,
                                               "requires_review": len(payload["requires_review"]),
                                               "legal_declarations": len(payload["legal_declarations"])})
    return PreparationResult(
        app_uid, str(job["job_uid"]), S.AWAITING_APPROVAL, next_action, approval_uid=approval_uid, url=url,
        fields_found=len(fields), filled=filled, requires_review=len(payload["requires_review"]),
        legal_declarations=len(payload["legal_declarations"]),
        unanswered_required=len(payload["unanswered_required"]), validation_errors=validation_errors,
        screenshot=shot,
    )


# --------------------------------------------------------------------------- #
# Answers
# --------------------------------------------------------------------------- #
def _field_meta(db: Database, application_id: int, field_key: str) -> dict[str, Any]:
    details = arepo.latest_event_details(db, application_id, FORM_PREPARED_EVENT)
    return next((f for f in details.get("fields") or [] if f.get("key") == field_key), {})


def _canonical_value(row: "sqlite3.Row", meta: dict[str, Any], value: str) -> str:
    ftype = row["field_type"]
    if value == "":
        return value
    if ftype == "file":
        raise PermanentError("file uploads always use your stored résumé / cover letter; "
                             "regenerate documents with `job-agent prepare` to change them",
                             human_action='Use "" to leave an optional upload empty.')
    if ftype == "checkbox":
        n = norm(value)
        if n not in TRUTHY | FALSY:
            raise PermanentError(f"checkbox answers must be yes or no, not {value!r}")
        return "yes" if n in TRUTHY else "no"
    options = [str(o) for o in meta.get("options") or []]
    if ftype in {"select", "radio", "checkbox_group", "select-multiple"} and options:
        from job_agent.browser.forms import FieldOption

        opts = tuple(FieldOption(label=o, value=o) for o in options)
        parts = [p for p in value.split(";") if p.strip()] if ftype in {"checkbox_group", "select-multiple"} \
            else [value]
        chosen = []
        for part in parts:
            option = match_option(part, opts)
            if option is None:
                raise PermanentError(f"{part.strip()!r} is not one of the options: {'; '.join(options)}")
            chosen.append(option.label)
        return "; ".join(chosen)
    return value


def set_answer(ctx: "AppContext", application_ref: str | int, field_key: str, value: str) -> str | None:
    """Record your answer for one field (``""`` = deliberately leave it blank).

    Returns the uid of the new approval request when the application was
    awaiting approval (the old request is superseded), else ``None``.
    """
    db = ctx.db
    app = arepo.resolve_application(db, application_ref)
    app_id, app_uid = int(app["id"]), str(app["application_uid"])
    status = S(app["status"])
    if status not in ANSWERABLE:
        raise InvalidTransition(f"application {app_uid} is {status.value}; its answers can no longer be changed")
    row = arepo.get_answer(db, app_id, field_key)
    if row is None:
        raise arepo.NotFound(f"application {app_uid} has no field {field_key!r}",
                             human_action=f"See the field keys with `job-agent applications show {app_uid}`.")
    canonical = _canonical_value(row, _field_meta(db, app_id, row["field_key"]), value.strip())
    is_legal = bool(row["is_legal_declaration"])
    approval_uid: str | None = None
    with db.transaction():
        arepo.set_user_answer(db, int(row["id"]), canonical, is_legal=is_legal)
        repo.add_application_event(db, app_id, "answer_set", actor="user",
                                   details={"field_key": row["field_key"], "label": row["field_label"],
                                            "legal_declaration": is_legal, "left_blank": canonical == ""})
        repo.audit(db, actor="user", action="application_answer_set", entity_type="application", entity_id=app_id,
                   outcome="success", details={"field_key": row["field_key"], "legal_declaration": is_legal})
        if status == S.AWAITING_APPROVAL:
            payload = build_payload(db, app_id)
            approval_uid = repo.create_approval(db, ApprovalType.SUBMIT_APPLICATION, payload,
                                                summary=summary_line(payload), actor="system", application_id=app_id,
                                                expires_in_hours=ctx.config.applications.approval_expiry_hours)
            arepo.update_application(db, app_id, {"next_action": _review_next_action(payload, approval_uid)})
    return approval_uid


# --------------------------------------------------------------------------- #
# Decide
# --------------------------------------------------------------------------- #
def _submit_approval_row(db: Database, approval_uid: str) -> "sqlite3.Row":
    row = repo.get_approval(db, approval_uid)
    if row is None:
        raise ApprovalError(f"approval {approval_uid} not found")
    if row["request_type"] != ApprovalType.SUBMIT_APPLICATION.value or row["application_id"] is None:
        raise ApprovalError(f"approval {approval_uid} is not an application submission request")
    return row


def _rejected_documents(db: Database, payload: dict[str, Any]) -> list[str]:
    """Files in an approval payload that you have rejected (now, or already when the request was made)."""
    out = []
    for kind in ("resume", "cover_letter"):
        info = payload.get(kind) or {}
        if not isinstance(info, dict) or not info:
            continue
        doc_id = info.get("document_id")
        doc = docs.get_document(db, int(doc_id)) if isinstance(doc_id, int) else None
        if info.get("status") == REJECTED_DOCUMENT_STATUS or (doc and doc.status == REJECTED_DOCUMENT_STATUS):
            out.append(str(info.get("file") or kind.replace("_", " ")))
    return out


def _withdraw_approval(db: Database, row: "sqlite3.Row", *, actor: str, note: str | None,
                       expected_payload_hash: str | None) -> None:
    """You take back an approval you gave that was never used (it can no longer be consumed)."""
    if actor not in {"user", "dashboard", "cli"}:
        raise ApprovalError("approvals can only be decided by the user (cli or dashboard)")
    if expected_payload_hash is not None and expected_payload_hash != row["payload_hash"]:
        raise ApprovalError("the request changed since you reviewed it; review it again")
    reason = "approval withdrawn by you" + (f": {note}" if note else "")
    with db.transaction():
        if not repo.cancel_live_approvals(db, application_id=int(row["application_id"]), actor=actor, reason=reason):
            raise ApprovalError(f"approval {row['approval_uid']} was used or changed meanwhile; nothing was withdrawn")
        repo.audit(db, actor=actor, action="approval_withdrawn", entity_type="approval",
                   entity_id=row["approval_uid"], outcome="success", details={"note": note})


def decide(ctx: "AppContext", approval_uid: str, *, approve: bool, actor: str, note: str | None = None,
           expected_payload_hash: str | None = None) -> DecisionResult:
    """Record your decision on an approval (actor: cli | dashboard | user).

    Submission approvals also update the application (reject -> documents_ready;
    approve -> next action for manual or browser submission). Other approval
    types (e.g. follow-ups) are only recorded via ``repo.decide_approval``.
    """
    db = ctx.db
    row = repo.get_approval(db, approval_uid)
    if row is None:
        raise ApprovalError(f"approval {approval_uid} not found")
    if row["request_type"] != ApprovalType.SUBMIT_APPLICATION.value or row["application_id"] is None:
        repo.decide_approval(db, approval_uid, approve=approve, actor=actor, note=note,
                             expected_payload_hash=expected_payload_hash)
        return DecisionResult(approval_uid, approve, None, None, "Decision recorded.")
    app_id = int(row["application_id"])
    payload = repo.approval_payload(row)
    if approve:
        rejected = _rejected_documents(db, payload)
        if rejected:
            job = arepo.get_job(db, int(repo.get_application(db, app_id)["job_id"]))
            raise ApprovalError(
                f"you rejected {', '.join(rejected)}; a request that would use it cannot be approved",
                human_action=f"Reject this request, run `job-agent prepare {job['job_uid']}` to generate a new "
                             f"version, review it, then run `job-agent apply {job['job_uid']}` again.",
            )
    repo.expire_approvals(db)
    current = repo.get_approval(db, approval_uid)
    withdrawn = (not approve and current is not None and current["status"] == ApprovalStatus.APPROVED.value)
    if withdrawn:
        _withdraw_approval(db, current, actor=actor, note=note, expected_payload_hash=expected_payload_hash)
    else:
        repo.decide_approval(db, approval_uid, approve=approve, actor=actor, note=note,
                             expected_payload_hash=expected_payload_hash)
    app = repo.get_application(db, app_id)
    assert app is not None
    app_uid = str(app["application_uid"])
    url = payload.get("application_url") or app["application_url"]
    status = S(app["status"])
    if not approve:
        job = arepo.get_job(db, int(app["job_id"]))
        verb = "withdrawn (it was approved but never used)" if withdrawn else "rejected"
        next_action = (f"Approval {verb}{': ' + note if note else ''}. Fix answers (`job-agent approvals answer`) "
                       f"or documents (`job-agent prepare {job['job_uid']}`), then run "
                       f"`job-agent apply {job['job_uid']}` again.")
        if status == S.AWAITING_APPROVAL:
            repo.transition_application(db, app_id, S.DOCUMENTS_READY, actor=actor, expected_current=status,
                                        details={"rejected_approval": approval_uid, "note": note},
                                        next_action=next_action)
            status = S.DOCUMENTS_READY
        else:
            repo.add_application_event(db, app_id, "approval_rejected", actor=actor,
                                       details={"approval": approval_uid, "note": note})
        return DecisionResult(approval_uid, False, app_uid, status, next_action)

    if has_unknown_outcome(db, app_id):
        next_action = (f"Approved - an earlier browser submission of this application had an unknown outcome, so the "
                       f"browser will never submit it again. Submit manually at {url} using the prepared answers, "
                       f"then run job-agent applications mark-submitted {app_uid}")
    elif ctx.config.applications.browser_submission_enabled:
        blockers = submission_blockers(payload)
        next_action = (f"Approved - run `job-agent applications submit {approval_uid}` to submit with the browser, "
                       f"or submit manually at {url} and run `job-agent applications mark-submitted {app_uid}`.")
        if blockers:
            next_action += f" Browser submission is blocked until: {'; '.join(blockers[:3])}."
    else:
        next_action = (f"Approved - submit manually at {url} using the prepared answers, then run "
                       f"job-agent applications mark-submitted {app_uid}")
    with db.transaction():
        repo.add_application_event(db, app_id, "approval_approved", actor=actor,
                                   details={"approval": approval_uid, "note": note})
        if status == S.AWAITING_APPROVAL:
            arepo.update_application(db, app_id, {"next_action": next_action})
    return DecisionResult(approval_uid, True, app_uid, status, next_action)


def pending_approvals(db: Database) -> list[dict[str, Any]]:
    """Pending approvals of every type, with application context when there is one."""
    out = []
    for row in repo.pending_approvals(db):
        item = dict(row)
        item.pop("payload", None)
        if row["application_id"] is not None:
            info = db.query_one("SELECT a.application_uid, j.employer, j.title FROM applications a "
                                "JOIN jobs j ON j.id = a.job_id WHERE a.id = ?", (row["application_id"],))
            if info:
                item.update(dict(info))
        out.append(item)
    return out


# --------------------------------------------------------------------------- #
# Submit (browser) -- only with browser_submission_enabled and a live approval
# --------------------------------------------------------------------------- #
def _approval_problem(row: "sqlite3.Row") -> str | None:
    status = row["status"]
    note = str(row["decision_note"] or "")
    superseded = ("was replaced by a newer request after the application changed; review the new one"
                  if not note or note == "payload changed" else f"is no longer valid ({note})")
    return {
        "pending": "has not been approved yet; review it with `job-agent approvals show` and approve it",
        "consumed": "was already used for a submission attempt and can never be reused",
        "superseded": superseded,
        "rejected": "was rejected",
        "expired": "expired; run `job-agent apply` again to prepare a fresh approval",
    }.get(status) if status != ApprovalStatus.APPROVED.value else None


def submit_approved(ctx: "AppContext", approval_uid: str, *,
                    browser_factory: BrowserFactory | None = None) -> SubmissionResult:
    """Submit an approved application with the browser: re-fill exactly the approved answers, click once."""
    cfg, db = ctx.config, ctx.db
    if not cfg.applications.browser_submission_enabled:
        raise PermissionDenied(
            "browser submission is disabled (applications.browser_submission_enabled: false); nothing was submitted",
            human_action=("Submit the application yourself using the approved answers (`job-agent approvals show "
                          f"{approval_uid}`), then run `job-agent applications mark-submitted <application-uid>`. "
                          "To let the browser submit approved applications, set "
                          "applications.browser_submission_enabled: true in config/config.yaml."),
        )
    require_permission(cfg, Permission.SUBMIT_APPLICATIONS)
    repo.expire_approvals(db)
    row = _submit_approval_row(db, approval_uid)
    problem = _approval_problem(row)
    if problem:
        raise ApprovalError(f"approval {approval_uid} {problem}; nothing was submitted")
    app_id = int(row["application_id"])
    app = repo.get_application(db, app_id)
    assert app is not None
    app_uid = str(app["application_uid"])
    if has_unknown_outcome(db, app_id):
        raise SubmissionBlocked(
            f"a previous browser submission of {app_uid} has an unknown outcome; it will never be retried "
            "automatically",
            human_action=("Check your email / the employer portal. If it went through, run "
                          f"`job-agent applications mark-submitted {app_uid}`; otherwise apply manually and then "
                          "run mark-submitted."),
        )
    status = S(app["status"])
    if status != S.AWAITING_APPROVAL:
        raise SubmissionBlocked(f"application {app_uid} is {status.value}, not awaiting approval; nothing was submitted")
    payload = build_payload(db, app_id)
    if repo.payload_hash(payload) != row["payload_hash"]:
        raise ApprovalError("the application changed after approval; a new approval is required (nothing was submitted)")
    blockers = submission_blockers(payload)
    if blockers:
        raise SubmissionBlocked(
            "browser submission refused: " + "; ".join(blockers),
            human_action=(f"Resolve them with `job-agent approvals answer {app_uid} <field> <value>` (this creates a new "
                          "approval to review), or submit manually and run `job-agent applications mark-submitted`."),
        )
    job = arepo.get_job(db, int(app["job_id"]))
    policy = _policy(job)
    url = str(payload["application_url"] or "")
    answers = {a["field_key"]: a for a in payload["answers"]}
    blank_keys = {b["field_key"] for b in payload.get("left_blank_by_you") or []}
    factory = browser_factory or default_factory
    options = BrowserOptions.from_config(cfg)

    def not_submitted(reason: str) -> SubmissionResult:
        next_action = _manual_next_action(app_uid, url, f"Browser submission stopped before clicking submit "
                                                        f"(nothing was sent; the approval was not used): {reason}",
                                          (payload.get("resume") or {}).get("file"))
        with db.transaction():
            repo.add_application_event(db, app_id, "submission_aborted", actor="browser",
                                       details={"reason": reason, "approval": approval_uid})
            repo.transition_application(db, app_id, S.NEEDS_MANUAL_ACTION, actor="browser", expected_current=status,
                                        details={"reason": reason}, next_action=next_action)
        return SubmissionResult(app_uid, approval_uid, S.NEEDS_MANUAL_ACTION, "not_submitted", next_action)

    with factory(options) as browser:
        # Phase 1: open, re-fill exactly the approved answers and verify. Approval not used yet.
        try:
            policy.check_initial(url)
            page = browser.open_application(url, policy)
            live = {f.key: f for f in page.inspect_form()}
            missing = [k for k in answers if k not in live]
            if missing:
                raise _Manual(f"the form changed since approval (missing fields: {', '.join(missing[:5])})")
            unexpected = [f.key for f in live.values() if f.required and f.key not in answers and f.key not in blank_keys]
            if unexpected:
                raise _Manual(f"the form has required fields that were not approved: {', '.join(unexpected[:5])}")
            for key, item in answers.items():
                _enter(ctx, page, live[key], str(item["answer"]), item.get("source"))
            ensure_no_challenge(page)
            page.check_navigation()
            errors = page.validation_errors()
            if errors:
                raise _Manual(f"the form shows validation errors before submitting: {'; '.join(errors[:3])}")
            control = page.find_submit()
            if control is None:
                raise _Manual("no submit button was found")
            if control.form_action and policy.navigation_problem(control.form_action):
                raise _Manual(f"the form would post to {control.form_action}, which is not the official host")
            baseline = page.snapshot()
        except (ChallengeDetected, NavigationBlocked, PageUnavailable, FillError) as exc:
            repo.record_error(db, component="applications:submit", error=exc, application_id=app_id)
            return not_submitted(str(exc))
        except _Manual as exc:
            return not_submitted(exc.reason)
        except RecoverableError as exc:
            repo.record_error(db, component="applications:submit", error=exc, application_id=app_id)
            raise  # nothing happened; approval still unused, the application stays awaiting approval

        # Phase 2: consume the approval exactly once (recorded before the click), then click once.
        # The pid/host let a later command tell an abandoned attempt from one still running.
        repo.transition_application(
            db, app_id, S.SUBMISSION_ATTEMPTED, actor="browser", approval_uid=approval_uid, approved_payload=payload,
            expected_current=S.AWAITING_APPROVAL, details={"url": url, "pid": os.getpid(),
                                                           "host": socket.gethostname()},
            next_action="Submission attempted - if this does not change, check your email/portal before doing "
                        "anything else",
        )
        outcome: SubmitOutcome | None = None
        try:
            repo.audit(db, actor="browser", action="application_submit_clicked", entity_type="application",
                       entity_id=app_id, outcome="attempted", details={"approval_uid": approval_uid})
            with _stop_signals_raise():
                outcome = _click_and_poll(page, control, baseline, url,
                                          timeout_seconds=cfg.applications.browser_timeout_seconds)
                shot = _screenshot(ctx, page, app_uid, f"submit-{utcnow().strftime('%Y%m%dT%H%M%SZ')}")
                with _signals_deferred():
                    result = _record_outcome(ctx, app_id, app_uid, approval_uid, job, outcome, shot)
        except BaseException as exc:
            # Ctrl+C, a stop signal or any failure after the click: the form may have been sent. Record the
            # outcome (unknown unless already known) BEFORE anything else, then re-raise. Never retried.
            if repo.get_application(db, app_id)["status"] == S.SUBMISSION_ATTEMPTED.value:
                known = outcome if outcome is not None else SubmitOutcome(
                    "interrupted", url, detail=f"{type(exc).__name__} while waiting for the employer's page")
                with _signals_deferred():
                    _record_outcome(ctx, app_id, app_uid, approval_uid, job, known, None)
            raise
    return result


def _click_and_poll(page: ApplicationPage, control: Any, baseline: Any, url: str, *,
                    timeout_seconds: float) -> SubmitOutcome:
    """Click the approved submit control once and watch for the outcome (errors become unknown outcomes;
    KeyboardInterrupt / SystemExit propagate to the caller, which records them)."""
    try:
        page.click_submit(control)
    except Exception as exc:  # noqa: BLE001 - the click may or may not have happened
        return SubmitOutcome("click_error", url, detail=f"{type(exc).__name__}: {exc}"[:300])
    try:
        return poll_outcome(page, baseline, timeout_seconds=timeout_seconds)
    except Exception as exc:  # noqa: BLE001 - outcome unknown, handled by the caller
        return SubmitOutcome("error", url, detail=f"{type(exc).__name__}: {exc}"[:300])


def _record_outcome(ctx: "AppContext", app_id: int, app_uid: str, approval_uid: str, job: "sqlite3.Row",
                    outcome: SubmitOutcome, shot: str | None) -> SubmissionResult:
    db = ctx.db
    if outcome.kind == "confirmed":
        evidence = {"type": "page", "url": outcome.url, "matched_text": outcome.matched_text, "screenshot": shot}
        next_action = "Submitted (confirmation page seen) - watch your email for the employer's confirmation"
        repo.transition_application(db, app_id, S.SUBMITTED, actor="browser", evidence=evidence,
                                    expected_current=S.SUBMISSION_ATTEMPTED, next_action=next_action,
                                    details={"approval": approval_uid})
        return SubmissionResult(app_uid, approval_uid, S.SUBMITTED, "submitted", next_action, evidence=evidence,
                                screenshot=shot)
    if outcome.kind == "validation_errors":
        errors = list(outcome.validation_errors)
        next_action = (f"The employer's form rejected the submission: {'; '.join(errors[:3])}. Fix the answers and "
                       f"run `job-agent apply {job['job_uid']}` again (a new approval is required).")
        repo.transition_application(db, app_id, S.FAILED, actor="browser", expected_current=S.SUBMISSION_ATTEMPTED,
                                    details={"validation_errors": errors, "screenshot": shot, "url": outcome.url},
                                    next_action=next_action)
        return SubmissionResult(app_uid, approval_uid, S.FAILED, "failed", next_action, validation_errors=errors,
                                screenshot=shot)
    reason = {"timeout": "no confirmation appeared", "challenge": "a security check appeared after clicking",
              "navigation_blocked": "the page tried to leave the official site after clicking",
              "click_error": "the click failed midway", "error": "the browser failed after clicking",
              "interrupted": "the submission was interrupted (Ctrl+C, a stop signal or a stopped process) after "
                             "the submit click"}.get(outcome.kind, outcome.kind)
    detail = f"{reason} ({outcome.detail})" if outcome.detail else reason
    next_action = (f"Unknown outcome: {detail}. Check your email/portal before doing anything else. If it went "
                   f"through, run `job-agent applications mark-submitted {app_uid}`; otherwise apply manually. "
                   "The agent will not retry this submission.")
    err = UnknownOutcome(f"submission of {app_uid} has an unknown outcome: {detail}",
                         human_action="check your email/portal before doing anything else")
    with db.transaction():
        repo.add_application_event(db, app_id, "submission_outcome_unknown", actor="browser",
                                   details={"kind": outcome.kind, "detail": outcome.detail, "url": outcome.url,
                                            "screenshot": shot, "approval": approval_uid})
        repo.record_error(db, component="applications:submit", error=err, kind=ErrorKind.UNKNOWN_OUTCOME,
                          application_id=app_id, job_id=int(job["id"]), retryable=False,
                          details={"kind": outcome.kind})
        repo.transition_application(db, app_id, S.NEEDS_MANUAL_ACTION, actor="browser",
                                    expected_current=S.SUBMISSION_ATTEMPTED, details={"unknown_outcome": detail},
                                    next_action=next_action)
    return SubmissionResult(app_uid, approval_uid, S.NEEDS_MANUAL_ACTION, "unknown", next_action, screenshot=shot)


# --------------------------------------------------------------------------- #
# Tracking (human-reported changes)
# --------------------------------------------------------------------------- #
def mark_submitted(ctx: "AppContext", application_ref: str | int, *, note: str | None = None) -> PipelineState:
    """You applied yourself (or confirmed an unknown outcome went through)."""
    db = ctx.db
    app = arepo.resolve_application(db, application_ref)
    app_id = int(app["id"])
    with db.transaction():
        previous = repo.transition_application(db, app_id, S.SUBMITTED, actor="user",
                                               details={"note": note, "method": "reported by you"},
                                               next_action="Submitted (reported by you) - watch your email for a "
                                                           "confirmation")
        _close_live_approvals(db, app_id, "application marked submitted by you")
    return previous


#: Statuses you can set yourself to record or correct an outcome (e.g. an email
#: was linked to the wrong application). The state machine decides which moves
#: are valid from the current status.
USER_SETTABLE_STATUSES: frozenset[PipelineState] = frozenset(
    {S.SUBMITTED, S.CONFIRMATION_RECEIVED, S.ASSESSMENT_RECEIVED, S.INTERVIEW_REQUESTED,
     S.INTERVIEW_SCHEDULED, S.REJECTED, S.OFFER_RECEIVED, S.CLOSED}
)


def set_status(ctx: "AppContext", application_ref: str | int, status: PipelineState | str, *,
               note: str | None = None) -> PipelineState:
    """Record or correct an application outcome yourself (actor "user"). Returns the previous state."""
    target = PipelineState(status)
    if target not in USER_SETTABLE_STATUSES:
        allowed = ", ".join(sorted(s.value for s in USER_SETTABLE_STATUSES))
        raise PermanentError(f"status {target.value!r} cannot be set by hand; choose one of: {allowed}")
    db = ctx.db
    app = _recover_for_user_action(ctx, arepo.resolve_application(db, application_ref), target)
    app_id = int(app["id"])
    with db.transaction():
        previous = repo.transition_application(db, app_id, target, actor="user",
                                               details={"note": note, "method": "set by you"},
                                               next_action=f"{target.value.replace('_', ' ').capitalize()} "
                                                           "(set by you)")
        # Every status you can set records an outcome: no earlier submit approval may stay usable.
        _close_live_approvals(db, app_id, f"application marked {target.value} by you")
    return previous


def withdraw(ctx: "AppContext", application_ref: str | int, *, note: str | None = None) -> PipelineState:
    db = ctx.db
    app = _recover_for_user_action(ctx, arepo.resolve_application(db, application_ref), S.WITHDRAWN)
    app_id = int(app["id"])
    with db.transaction():
        previous = repo.transition_application(db, app_id, S.WITHDRAWN, actor="user", details={"note": note},
                                               next_action="Withdrawn")
        _close_live_approvals(db, app_id, "application withdrawn by you")
    return previous


def add_note(ctx: "AppContext", application_ref: str | int, text: str) -> int:
    db = ctx.db
    text = text.strip()
    if not text:
        raise PermanentError("note text is empty")
    app = arepo.resolve_application(db, application_ref)
    app_id = int(app["id"])
    stamp = utcnow_iso()
    with db.transaction():
        event_id = repo.add_application_event(db, app_id, "note", actor="user", details={"note": text[:2000]})
        notes = (app["notes"] + "\n" if app["notes"] else "") + f"[{stamp}] {text[:2000]}"
        arepo.update_application(db, app_id, {"notes": notes})
    return event_id


def list_applications(db: Database, status: PipelineState | str | None = None) -> list[dict[str, Any]]:
    value = status.value if isinstance(status, PipelineState) else status
    return arepo.list_applications(db, value)


def application_detail(db: Database, ref: str | int) -> dict[str, Any]:
    """Row + timeline + answers + documents + approvals for one application."""
    from job_agent.database.connection import from_json

    app = arepo.resolve_application(db, ref)
    app_id = int(app["id"])
    job = arepo.get_job(db, int(app["job_id"]))
    application = dict(app)
    application["confirmation_evidence"] = from_json(app["confirmation_evidence"], {})
    documents: dict[str, list[dict[str, Any]]] = {}
    for doc_type, column in (("resume", "resume_document_id"), ("cover_letter", "cover_letter_document_id")):
        base = docs.get_document(db, app[column])
        documents[doc_type] = [v.summary() for v in docs.variants(db, base)] if base else []
    approvals = [{k: r[k] for k in ("approval_uid", "status", "summary", "requested_at", "expires_at", "decided_at",
                                    "decided_by", "decision_note", "consumed_at", "payload_hash")}
                 for r in arepo.approvals_for(db, app_id)]
    return {
        "application": application,
        "job": {k: job[k] for k in ("id", "job_uid", "employer", "title", "location", "job_url", "application_url",
                                    "listing_status")},
        "events": arepo.events(db, app_id),
        "answers": [dict(r) for r in arepo.get_answers(db, app_id)],
        "documents": documents,
        "approvals": approvals,
        "unknown_outcome": has_unknown_outcome(db, app_id),
        "unknown_outcome_unacknowledged": _unacknowledged_unknown_outcome(db, app_id),
        "submission_abandoned": (app["status"] == S.SUBMISSION_ATTEMPTED.value
                                 and _attempt_abandoned(app, _attempt_owner(db, app_id))),
    }
