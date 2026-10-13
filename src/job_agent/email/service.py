"""Gmail sync: list -> dedupe -> fetch -> parse -> classify -> associate -> evidence-based status.

``sync_gmail`` is read-only towards Gmail. Message bodies are processed in
memory and dropped; only metadata (+ optional short snippet / summary) is
stored. Email content can never trigger anything other than the deterministic
status rules in ``email.status``; nothing is ever sent, accepted or replied to.

A message is always stored first; its status update runs in its own savepoint,
so a refused or failed update can never lose the message (which would make
every later sync download and re-process it). Anything status-relevant that is
not applied automatically is flagged ``requires_attention`` with the reasons
recorded (``job-agent emails show <id>``).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from job_agent.database import repo
from job_agent.email import auth
from job_agent.email import repository as email_repo
from job_agent.email.associate import (
    AUTO_THRESHOLD,
    VERIFIED_METHODS,
    AssociationCandidate,
    find_candidates,
    store_candidates,
)
from job_agent.email.classify import (
    ACTIONABLE,
    RULES_CONFIDENT,
    classify_rules,
    combine,
    is_promotional,
    rules_summary,
    validate_llm_classification,
)
from job_agent.email.gmail_client import GmailReadClient
from job_agent.email.parse import ParsedEmail, parse_message
from job_agent.email.replies import REPLY_CATEGORIES, suggest_reply
from job_agent.email.status import CATEGORY_TARGET, StatusOutcome, apply_email_status, evidence_for
from job_agent.errors import (
    AuthorizationRequired,
    HumanActionRequired,
    JobAgentError,
    PermanentError,
    RecoverableError,
)
from job_agent.logging_config import get_logger
from job_agent.models import EmailCategory, EmailClassification, Permission, PipelineState
from job_agent.security.permissions import is_allowed, require_permission
from job_agent.security.redaction import redact_text
from job_agent.security.untrusted import injection_suspected
from job_agent.utils.time import utcnow_iso

if TYPE_CHECKING:  # pragma: no cover
    from job_agent.context import AppContext
    from job_agent.tasks import RunInfo

log = get_logger(__name__)

_QUERY_GUARD = " -in:sent -in:drafts -in:chats"


@dataclass
class EmailSyncResult:
    fetched: int = 0  # full messages downloaded this run
    new: int = 0  # rows stored this run
    skipped_existing: int = 0  # listed ids already stored (not re-downloaded)
    by_category: dict[str, int] = field(default_factory=dict)
    associated: int = 0  # new messages linked to a job/application
    status_updates: int = 0  # evidence-based application status changes
    errors: list[str] = field(default_factory=list)
    listed: int = 0
    attention: int = 0  # new messages flagged for the user's attention
    injection_flagged: int = 0
    aborted: bool = False  # a transient error stopped fetching early

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Processed:
    email_id: int | None
    category: EmailCategory
    associated: bool
    status_applied: PipelineState | None
    requires_attention: bool
    injection: bool


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _list_params(config: Any) -> tuple[str | None, list[str] | None, str]:
    """(query, label_ids, message format) for the configured scope.

    The gmail.metadata scope forbids search queries and full bodies, so only the
    inbox is listed and only headers are read in that mode.
    """
    if auth.SCOPE_READONLY in set(config.gmail.scopes):
        return (config.gmail.query or "").strip() + _QUERY_GUARD, None, "full"
    return None, ["INBOX"], "metadata"


def _short(exc: BaseException) -> str:
    return redact_text(f"{type(exc).__name__}: {exc}", pii=True)[:300]


def ensure_gmail_enabled(config: Any) -> None:
    """Raise ``HumanActionRequired`` (with the setup doc as next step) unless ``gmail.enabled``."""
    if not config.gmail.enabled:
        raise HumanActionRequired("Gmail integration is disabled (gmail.enabled: false)",
                                  human_action=f"Follow {auth.SETUP_DOC}, then set gmail.enabled: true")


def _scope_list(value: Any) -> list[str] | None:
    if isinstance(value, str):
        return value.split()
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return [v for v in value if v]
    return None


def require_verified_token_scopes(ctx: "AppContext") -> None:
    """Refuse a token file that does not record its scopes: it cannot be verified as read-only.

    ``google-auth`` happily loads a token without ``scopes``; the access it then
    carries is whatever the grant had, which may include sending mail.
    """
    path = auth.token_file(ctx.config)
    if not path.is_file():
        return  # load_credentials reports the missing token
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return  # load_credentials records the unreadable token
    if not isinstance(info, dict):
        return
    stored, granted = _scope_list(info.get("scopes")), _scope_list(info.get("granted_scopes"))
    if stored or granted:
        return
    auth.record_auth_error(ctx.db, ctx.config, "token file does not record its scopes (not verified read-only)")
    raise AuthorizationRequired(
        "Gmail token does not record which scopes it carries, so it cannot be verified as read-only; refusing "
        "to use it",
        human_action="Run job-agent gmail revoke, then job-agent gmail auth",
    )


class _Processor:
    """Per-sync state: lazily created LLM client, profile facts, error de-duplication."""

    def __init__(self, ctx: "AppContext", run: "RunInfo | None") -> None:
        self.ctx = ctx
        self.db = ctx.db
        self.config = ctx.config
        self.run = run
        self._client: Any = None
        self._client_loaded = False
        self._llm_failed = False
        self.drafts_allowed = is_allowed(self.config, Permission.DRAFT_EMAILS)
        self.self_address, self.candidate_name = self._profile_identity()

    def _profile_identity(self) -> tuple[str | None, str | None]:
        try:
            personal = self.ctx.profile.personal
        except JobAgentError:
            return None, None
        return (personal.email or "").strip().lower() or None, personal.full_name

    # -- optional local LLM -------------------------------------------- #
    def _record_llm_error(self, exc: BaseException) -> None:
        if self._llm_failed:
            return
        self._llm_failed = True
        repo.record_error(self.db, component="email:llm", error=exc, task_name="email_sync",
                          run_id=self.run.run_id if self.run else None, retryable=False)

    def _llm(self) -> Any:
        if self._client_loaded:
            return None if self._llm_failed else self._client
        self._client_loaded = True
        if not self.config.llm.enabled:
            return None
        try:
            from job_agent.llm.client import get_client

            self._client = get_client(self.ctx)
        except ImportError:
            log.info("local LLM module not available; rules-only email classification")
            self._client = None
        except JobAgentError as exc:
            self._record_llm_error(exc)
            self._client = None
        return self._client

    def llm_classify(self, parsed: ParsedEmail) -> EmailClassification | None:
        client = self._llm()
        if client is None:
            return None
        try:
            from job_agent.llm.generation import classify_email

            raw = classify_email(client, subject=parsed.subject, sender=parsed.sender, body=parsed.body_text)
            return validate_llm_classification(raw)
        except ImportError:
            return None
        except JobAgentError as exc:
            self._record_llm_error(exc)
            return None

    def llm_reply(self) -> Callable[..., str] | None:
        client = self._llm()
        if client is None:
            return None

        def draft(**kwargs: Any) -> str:
            try:
                from job_agent.llm.generation import draft_email_reply

                return draft_email_reply(client, **kwargs)
            except ImportError:
                return ""
            except JobAgentError as exc:
                self._record_llm_error(exc)
                return ""

        return draft

    # -- one message ---------------------------------------------------- #
    def process(self, parsed: ParsedEmail) -> _Processed:
        base = {
            "gmail_message_id": parsed.gmail_message_id,
            "gmail_thread_id": parsed.gmail_thread_id,
            "received_at": parsed.received_at,
            "from_address": parsed.from_address,
            "from_domain": parsed.from_domain,
            "subject": parsed.subject,
            "snippet": parsed.snippet if self.config.gmail.store_snippets else None,
            "processed_at": utcnow_iso(),
        }
        own = parsed.is_from_self or (self.self_address is not None and parsed.from_address == self.self_address)
        if own:
            # Your own messages are recorded (so they are not re-downloaded) but never acted on.
            with self.db.transaction():
                email_id = email_repo.insert_message(self.db, {
                    **base, "category": EmailCategory.OTHER.value, "category_confidence": 1.0,
                    "classifier": "skipped:own_message", "summary": "Your own message (ignored)",
                    "requires_attention": 0, "injection_suspected": 0})
            return _Processed(email_id, EmailCategory.OTHER, False, None, False, False)

        rules = classify_rules(subject=parsed.subject, body=parsed.body_text, from_address=parsed.from_address,
                               from_domain=parsed.from_domain, from_name=parsed.from_name, bulk=parsed.bulk)
        llm = self.llm_classify(parsed) if rules.confidence < RULES_CONFIDENT else None
        cls = combine(rules, llm)
        suspicious = injection_suspected(parsed.subject) or injection_suspected(parsed.body_text)
        bulk = parsed.bulk or is_promotional(subject=parsed.subject, body=parsed.body_text)

        # Newsletters / unrelated mail are not linked to anything (avoids noisy guesses).
        candidates = [] if cls.category == EmailCategory.OTHER else find_candidates(
            self.db, thread_id=parsed.gmail_thread_id, from_address=parsed.from_address,
            from_domain=parsed.from_domain, from_name=parsed.from_name, subject=parsed.subject,
            body=parsed.body_text, employer_guess=cls.employer_guess)
        best: AssociationCandidate | None = candidates[0] if candidates else None
        status_relevant = cls.category in CATEGORY_TARGET
        held = auto_update_blockers(parsed, cls, suspicious=suspicious, bulk=bulk, best=best) \
            if status_relevant else []

        reply: str | None = None
        if cls.category in REPLY_CATEGORIES and self.drafts_allowed:
            # Suspicious content never reaches the LLM drafter; the template does not use the content.
            reply, _source = suggest_reply(cls.category, candidate_name=self.candidate_name,
                                           subject=parsed.subject, body=parsed.body_text,
                                           llm_draft=None if suspicious else self.llm_reply())
        if self.config.gmail.store_snippets:
            summary = cls.summary or rules_summary(cls.category, subject=parsed.subject, from_name=parsed.from_name,
                                                   from_domain=parsed.from_domain)
        else:  # metadata-only summary (no body-derived text)
            summary = rules_summary(cls.category, subject=parsed.subject, from_name=parsed.from_name,
                                    from_domain=parsed.from_domain)

        outcome = StatusOutcome()
        attention = cls.requires_attention or cls.category in ACTIONABLE or suspicious
        with self.db.transaction():
            # Stored first, flagged until the status question is settled below.
            email_id = email_repo.insert_message(self.db, {
                **base, "category": cls.category.value, "category_confidence": cls.confidence,
                "classifier": cls.classifier, "summary": summary,
                "requires_attention": int(attention or status_relevant),
                "suggested_reply": reply, "injection_suspected": int(suspicious)})
            if email_id is None:  # stored concurrently by another run
                return _Processed(None, cls.category, False, None, False, suspicious)
            assoc_ids = store_candidates(self.db, email_id, candidates)
            if status_relevant and not held and best is not None:
                outcome = self._apply_status(parsed, cls.category, best, assoc_ids[0], email_id)
            if status_relevant and outcome.applied is None and not (
                    outcome.skipped_reason or "").startswith("application already"):
                attention = True  # status-relevant but not applied automatically: ask the user
                repo.audit(self.db, actor="email", action="email_status_needs_review", entity_type="email",
                           entity_id=email_id, outcome="needs_review", run_id=self.run.run_id if self.run else None,
                           details={"category": cls.category.value,
                                    "application_id": best.application_id if best else None,
                                    "reasons": held or [outcome.skipped_reason or "not applied"]})
            email_repo.update_message(self.db, email_id, {"requires_attention": int(attention)})
        return _Processed(email_id, cls.category, bool(candidates), outcome.applied, attention, suspicious)

    def _apply_status(self, parsed: ParsedEmail, category: EmailCategory, best: AssociationCandidate,
                      association_id: int, email_id: int) -> StatusOutcome:
        """Automatic status update in its own savepoint; a refusal is recorded, never raised."""
        try:
            with self.db.transaction():
                return apply_email_status(
                    self.db, application_id=int(best.application_id or 0), association_id=association_id,
                    category=category, actor="email", email_id=email_id,
                    evidence=evidence_for(gmail_message_id=parsed.gmail_message_id, subject=parsed.subject,
                                          received_at=parsed.received_at, category=category))
        except (JobAgentError, sqlite3.Error) as exc:
            repo.record_error(self.db, component="email:status", error=exc, task_name="email_sync",
                              run_id=self.run.run_id if self.run else None, application_id=best.application_id,
                              details={"gmail_message_id": parsed.gmail_message_id, "email_id": email_id},
                              retryable=False)
            return StatusOutcome(skipped_reason=f"status update refused: {_short(exc)}")


def auto_update_blockers(parsed: ParsedEmail, cls: EmailClassification, *, suspicious: bool, bulk: bool,
                         best: AssociationCandidate | None) -> list[str]:
    """Why this status-relevant email must not change a status by itself (empty = it may)."""
    reasons: list[str] = []
    if cls.classifier != "rules":
        reasons.append(f"category suggested by the local AI ({cls.classifier}), not the deterministic rules")
    elif cls.confidence < RULES_CONFIDENT:
        reasons.append(f"category uncertain (confidence {cls.confidence:.2f})")
    if suspicious:
        reasons.append("message contains instruction-like text (possible prompt injection)")
    if bulk:
        reasons.append("bulk or promotional mail")
    if not parsed.auth.authenticated:
        reasons.append(f"sender not verified by Gmail ({parsed.auth.reason})")
    if best is None:
        reasons.append("not linked to any job you are tracking")
    elif best.application_id is None:
        reasons.append("the linked job has no application")
    elif best.confidence < AUTO_THRESHOLD:
        detail = "; ".join(best.reasons[-2:])
        reasons.append(f"link to the application is uncertain ({best.confidence:.2f}" + (f": {detail})" if detail
                                                                                          else ")"))
    elif best.method != "thread" and (best.method not in VERIFIED_METHODS or best.title_status != "match"):
        # Defence in depth: associate already caps these below the threshold.
        reasons.append("the link is not from a verified sender naming this application's role")
    return reasons


def _make_client(ctx: "AppContext", service: Any, fmt: str) -> GmailReadClient:
    if service is not None:
        return GmailReadClient(service, message_format=fmt)
    require_verified_token_scopes(ctx)
    creds = auth.load_credentials(ctx)
    return GmailReadClient.from_credentials(creds, message_format=fmt)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def sync_gmail(ctx: "AppContext", *, service: Any = None, max_messages: int | None = None,
               client: GmailReadClient | None = None, run: "RunInfo | None" = None) -> EmailSyncResult:
    """Read recent messages (read-only), store metadata, link and apply evidence-based status updates.

    ``service`` (a Gmail API service object) or ``client`` (a ``GmailReadClient``)
    can be injected; otherwise the stored OAuth token is loaded.
    """
    config = ctx.config
    db = ctx.db
    ensure_gmail_enabled(config)
    require_permission(config, Permission.READ_EMAILS)
    auth.requested_scopes(config)  # refuses anything but read-only scopes
    auth.fail_fast_if_auth_broken(db, config)

    query, label_ids, fmt = _list_params(config)
    # load_credentials records gmail_auth_error itself when the token is rejected.
    gmail = client or _make_client(ctx, service, fmt)
    limit = max_messages or config.gmail.max_messages_per_sync
    result = EmailSyncResult()
    run_id = run.run_id if run else None

    try:
        refs = gmail.list_message_ids(query, limit, label_ids=label_ids)
    except AuthorizationRequired as exc:
        auth.record_auth_error(db, config, str(exc))
        raise
    result.listed = len(refs)
    known = email_repo.known_message_ids(db, [r.id for r in refs])
    result.skipped_existing = sum(1 for r in refs if r.id in known)

    parsed_messages: list[ParsedEmail] = []
    for ref in refs:
        if ref.id in known:
            continue
        try:
            message = gmail.get_message(ref.id)
        except AuthorizationRequired as exc:
            auth.record_auth_error(db, config, str(exc))
            raise
        except RecoverableError as exc:
            repo.record_error(db, component="email:gmail", error=exc, task_name="email_sync", run_id=run_id)
            result.errors.append(_short(exc))
            result.aborted = True
            break
        except PermanentError as exc:  # e.g. deleted between list and get
            repo.record_error(db, component="email:gmail", error=exc, task_name="email_sync", run_id=run_id,
                              details={"gmail_message_id": ref.id})
            result.errors.append(_short(exc))
            continue
        result.fetched += 1
        try:
            parsed_messages.append(parse_message(message, body_char_limit=config.gmail.body_char_limit))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            repo.record_error(db, component="email:parse", error=exc, task_name="email_sync", run_id=run_id,
                              details={"gmail_message_id": ref.id})
            result.errors.append(_short(exc))

    # Oldest first, so e.g. a confirmation is applied before a later rejection.
    parsed_messages.sort(key=lambda p: p.received_at or "")
    processor = _Processor(ctx, run)
    for parsed in parsed_messages:
        try:
            done = processor.process(parsed)
        except (JobAgentError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            repo.record_error(db, component="email:process", error=exc, task_name="email_sync", run_id=run_id,
                              details={"gmail_message_id": parsed.gmail_message_id})
            result.errors.append(_short(exc))
            continue
        finally:
            parsed.body_text = ""  # drop the body as soon as the message is handled
        if done.email_id is None:
            result.skipped_existing += 1
            continue
        result.new += 1
        result.by_category[done.category.value] = result.by_category.get(done.category.value, 0) + 1
        result.associated += int(done.associated)
        result.status_updates += int(done.status_applied is not None)
        result.attention += int(done.requires_attention)
        result.injection_flagged += int(done.injection)

    # Listing succeeded, so authorization works: clear any stale auth error.
    auth.clear_auth_error(db)
    if not result.aborted:
        repo.set_state(db, auth.STATE_LAST_SUCCESS, utcnow_iso())
    repo.audit(db, actor="system", action="gmail_sync", outcome="partial" if result.errors else "success",
               run_id=run_id, details={k: v for k, v in result.as_dict().items() if k != "errors"}
               | {"error_count": len(result.errors)})
    log.info("gmail sync: listed=%d new=%d skipped=%d status_updates=%d errors=%d", result.listed, result.new,
             result.skipped_existing, result.status_updates, len(result.errors))
    return result


def email_sync_task(ctx: "AppContext", run: "RunInfo") -> dict[str, Any]:
    result = sync_gmail(ctx, run=run)
    summary = result.as_dict()
    summary["partial"] = bool(result.errors)
    summary["finished_at"] = utcnow_iso()
    return summary


# --------------------------------------------------------------------------- #
# User actions (CLI / dashboard)
# --------------------------------------------------------------------------- #
@dataclass
class AssociationResult:
    email_id: int
    job_id: int
    application_id: int | None
    association_id: int
    status_applied: str | None = None
    previous_status: str | None = None
    skipped_reason: str | None = None
    removed_guesses: int = 0


def _resolve_job(db: Any, ref: int | str) -> sqlite3.Row:
    text = str(ref).strip()
    row = (db.query_one("SELECT * FROM jobs WHERE id = ?", (int(text),)) if text.isdigit()
           else db.query_one("SELECT * FROM jobs WHERE job_uid = ?", (text,)))
    if row is None:
        raise PermanentError(f"job {text!r} not found")
    return row


def _resolve_application(db: Any, ref: int | str) -> sqlite3.Row:
    text = str(ref).strip()
    row = (db.query_one("SELECT * FROM applications WHERE id = ?", (int(text),)) if text.isdigit()
           else db.query_one("SELECT * FROM applications WHERE application_uid = ?", (text,)))
    if row is None:
        raise PermanentError(f"application {text!r} not found")
    return row


def associate_email(ctx: "AppContext", email_id: int, *, job_ref: int | str | None = None,
                    application_ref: int | str | None = None, apply_status: bool = False) -> AssociationResult:
    """User-confirmed link of an email to a job/application (actor ``user``).

    With ``apply_status`` the status implied by the email's category is applied
    (if the state machine allows it) with the email as evidence.
    """
    db = ctx.db
    email = email_repo.get_message(db, int(email_id))
    if email is None:
        raise PermanentError(f"email {email_id} not found")
    if job_ref is None and application_ref is None:
        raise PermanentError("give a job or an application to associate the email with")
    app = _resolve_application(db, application_ref) if application_ref is not None else None
    job = _resolve_job(db, job_ref) if job_ref is not None else None
    if app is not None and job is not None and int(app["job_id"]) != int(job["id"]):
        raise PermanentError("that application belongs to a different job")
    if job is None:
        job = _resolve_job(db, int(app["job_id"]))  # type: ignore[index]
    if app is None:
        app = repo.get_application_for_job(db, int(job["id"]))
    job_id = int(job["id"])
    app_id = int(app["id"]) if app is not None else None
    category = EmailCategory(email["category"])

    with db.transaction():
        removed = db.execute(
            "DELETE FROM email_associations WHERE email_id = ? AND confirmed_by_user = 0 "
            "AND NOT (job_id IS ? AND application_id IS ?)", (int(email_id), job_id, app_id)).rowcount
        existing = email_repo.find_association(db, int(email_id), job_id, app_id)
        assoc_id = email_repo.upsert_association(db, email_id=int(email_id), job_id=job_id, application_id=app_id,
                                                 method="user", confidence=1.0, confirmed_by_user=True)
        repo.audit(db, actor="user", action="email_associated", entity_type="email", entity_id=email_id,
                   outcome="success", details={"job_id": job_id, "application_id": app_id,
                                               "removed_guesses": removed, "apply_status": apply_status})
        result = AssociationResult(email_id=int(email_id), job_id=job_id, application_id=app_id,
                                   association_id=assoc_id, removed_guesses=removed)
        if apply_status:
            if app_id is None:
                result.skipped_reason = "no application exists for this job"
            elif existing is not None and existing["status_applied"]:
                result.skipped_reason = f"status {existing['status_applied']} was already applied from this email"
            else:
                outcome = apply_email_status(
                    db, application_id=app_id, association_id=assoc_id, category=category, actor="user",
                    email_id=int(email_id),
                    evidence=evidence_for(gmail_message_id=email["gmail_message_id"], subject=email["subject"],
                                          received_at=email["received_at"], category=category))
                result.status_applied = outcome.applied.value if outcome.applied else None
                result.previous_status = outcome.previous.value if outcome.previous else None
                result.skipped_reason = outcome.skipped_reason
        if category not in ACTIONABLE or result.status_applied:
            email_repo.update_message(db, int(email_id), {"requires_attention": 0})
    return result


def mark_email_handled(ctx: "AppContext", email_id: int, *, actor: str = "user") -> bool:
    """Clear ``requires_attention`` (e.g. you replied yourself). Returns whether it changed."""
    db = ctx.db
    if email_repo.get_message(db, int(email_id)) is None:
        raise PermanentError(f"email {email_id} not found")
    with db.transaction():
        changed = db.execute("UPDATE email_messages SET requires_attention = 0 WHERE id = ? AND requires_attention = 1",
                             (int(email_id),)).rowcount
        repo.audit(db, actor=actor, action="email_marked_handled", entity_type="email", entity_id=email_id,
                   outcome="success" if changed else "skipped")
    return bool(changed)
