"""Document preparation service: job -> tailored, validated résumé (+ cover letter) files.

``prepare_documents`` is the only entry point that writes. It:

1. checks ``permissions.generate_documents``;
2. loads the job (by id or job_uid) and the verified profile;
3. creates the application if none exists (status ``documents_pending``);
4. selects verified facts, optionally rewords them with the local LLM (accepted
   only when ``validate_claim`` finds nothing), validates the whole document,
   renders DOCX/PDF (+ Markdown preview) and stores new versions;
5. links the newest résumé/cover letter (PDF preferred) to the application and
   moves it to ``documents_ready`` -- unless the application is already further
   along, in which case only a ``documents_regenerated`` event is added. Once the
   documents were (or may have been) sent -- ``submitted_at`` is set, the status
   is ``submission_attempted`` or later, or a submission outcome is unknown and
   not confirmed as "not submitted" -- the new versions are stored for your
   records but the application keeps pointing at the documents actually used
   (event ``documents_regenerated_not_linked``);
6. on failure: ``failed`` + ``repo.record_error``; nothing half-written remains.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

from job_agent.database import repo
from job_agent.database.connection import from_json
from job_agent.errors import JobAgentError, PermanentError
from job_agent.logging_config import get_logger
from job_agent.models import DocumentStatus, DocumentType, Permission, PipelineState
from job_agent.profile import CandidateProfile
from job_agent.resume import render
from job_agent.resume.cover_letter import build_cover_letter, title_slot
from job_agent.resume.keywords import JobKeywords, extract_job_keywords
from job_agent.resume.llm_assist import LLMAssist, get_assist, reword_bullets
from job_agent.resume.models import (
    Claim,
    DocumentInfo,
    DocumentRecord,
    GeneratedCoverLetter,
    GeneratedResume,
    Issue,
    JobListing,
    PreparedDocuments,
)
from job_agent.resume.repository import (
    get_document as _get_document,
)
from job_agent.resume.repository import (
    list_documents as _list_documents,
)
from job_agent.resume.repository import (
    preferred,
    set_document_status,
    store_document_set,
)
from job_agent.resume.selection import select_resume_content
from job_agent.resume.validation import document_status, validate_resume
from job_agent.security.permissions import require_permission
from job_agent.utils.time import local_today

if TYPE_CHECKING:  # pragma: no cover
    from job_agent.context import AppContext
    from job_agent.database.connection import Database

log = get_logger(__name__)

__all__ = ["prepare_documents", "generate_resume", "list_documents", "get_document", "set_document_status",
           "load_job"]

_S = PipelineState
#: Application states from which (re)generation moves the application to documents_ready.
_READY_FROM = {_S.DOCUMENTS_PENDING, _S.FAILED, _S.NEEDS_MANUAL_ACTION}
#: Application states in which the linked documents were (or may have been) sent to the employer.
_SENT_STATES = {_S.SUBMISSION_ATTEMPTED, _S.SUBMITTED, _S.CONFIRMATION_RECEIVED, _S.ASSESSMENT_RECEIVED,
                _S.INTERVIEW_REQUESTED, _S.INTERVIEW_SCHEDULED, _S.REJECTED, _S.OFFER_RECEIVED}
NOT_LINKED_EVENT = "documents_regenerated_not_linked"


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_job(db: "Database", job_ref: int | str) -> sqlite3.Row:
    if isinstance(job_ref, int) or (isinstance(job_ref, str) and job_ref.strip().isdigit()):
        row = db.query_one("SELECT * FROM jobs WHERE id = ?", (int(job_ref),))
    else:
        row = db.query_one("SELECT * FROM jobs WHERE job_uid = ?", (str(job_ref).strip(),))
    if row is None:
        raise PermanentError(f"job {job_ref!r} not found",
                             human_action="Check the job id (job_uid such as J-1a2b3c4d5e) in the report or dashboard.")
    return row


def _job_listing(db: "Database", row: sqlite3.Row) -> JobListing:
    score = db.query_one(
        "SELECT matching_qualifications FROM job_scores WHERE job_id = ? AND is_current = 1 ORDER BY id DESC LIMIT 1",
        (row["id"],),
    )
    matching = [str(x) for x in from_json(score["matching_qualifications"], [])] if score else []
    return JobListing(
        id=int(row["id"]), job_uid=row["job_uid"], employer=row["employer"], title=row["title"],
        description_text=row["description_text"] or "",
        required_qualifications=[str(x) for x in from_json(row["required_qualifications"], [])],
        preferred_qualifications=[str(x) for x in from_json(row["preferred_qualifications"], [])],
        matching_qualifications=matching, application_url=row["application_url"], job_url=row["job_url"],
        listing_status=row["listing_status"] or "active",
    )


def keywords_for(job: JobListing) -> JobKeywords:
    return extract_job_keywords(job.title, job.required_qualifications, job.preferred_qualifications,
                                job.description_text, emphasis=job.matching_qualifications)


# --------------------------------------------------------------------------- #
# Generation (no DB writes)
# --------------------------------------------------------------------------- #
def generate_resume(profile: CandidateProfile, job: JobListing, kw: JobKeywords, *, max_bullets_per_role: int,
                    assist: LLMAssist | None = None, profile_label: str = "your profile") -> GeneratedResume:
    content = select_resume_content(profile, kw, max_bullets_per_role=max_bullets_per_role)
    issues: list[Issue] = []
    if assist is not None:
        issues += reword_bullets(assist, content, profile, kw, job_title=title_slot(job.title) or "",
                                 forbidden_names=[job.employer])
    issues += validate_resume(content, profile, profile_label=profile_label)
    if kw.injection_suspected:
        issues.append(Issue(code="listing_injection_suspected", severity="info", document="resume",
                            message="The job listing contains instruction-like text; it was treated as data and "
                                    "could not add content (only verified facts are used)."))
    if job.listing_status != "active":
        issues.append(Issue(code="listing_not_active", severity="info", document="resume",
                            message=f"The listing is marked {job.listing_status}."))
    claims: list[Claim] = []
    for section, bullet in content.all_bullets():
        claims.append(Claim(text=bullet.text, fact_ids=list(bullet.fact_ids), generator=bullet.generator,
                            section=section, rejected_rewording=bullet.rejected_rewording))
    generator = next((c.generator for c in claims if c.generator.startswith("llm")), "rules")
    return GeneratedResume(content=content, claims=claims, issues=issues,
                           status=DocumentStatus(document_status(issues)), generator=generator)


def _formats(ctx: "AppContext") -> list[str]:
    fmts = [f for f in ctx.config.documents.formats if f in {"docx", "pdf"}]
    return list(dict.fromkeys(fmts)) + ["md"]  # md preview is always generated


def _render_all(kind: DocumentType, content: Any, formats: list[str]) -> dict[str, bytes]:
    fn = render.render_resume if kind == DocumentType.RESUME else render.render_cover_letter
    return {fmt: fn(content, fmt) for fmt in formats}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def prepare_documents(ctx: "AppContext", job_ref: int | str, *, use_llm: bool | None = None,
                      include_cover_letter: bool | None = None, actor: str = "system") -> PreparedDocuments:
    """Generate a new version of the tailored résumé (and cover letter) for one job."""
    require_permission(ctx.config, Permission.GENERATE_DOCUMENTS)
    db = ctx.db
    row = load_job(db, job_ref)
    job = _job_listing(db, row)
    profile = ctx.profile  # ProfileError propagates before anything is created
    want_llm = ctx.config.documents.use_llm_rewording if use_llm is None else use_llm
    want_cover = ctx.config.documents.generate_cover_letter if include_cover_letter is None else include_cover_letter

    app_id = repo.create_application(db, job.id, actor="system", application_url=job.application_url or job.job_url)
    app = repo.get_application(db, app_id)
    if app is None:  # pragma: no cover - create_application just returned it
        raise PermanentError(f"application for job {job.job_uid} could not be loaded")
    status = _S(app["status"])
    keep_links = documents_sent(db, app)  # the linked documents are the record of what was sent
    submission_started = keep_links or int(app["submission_attempts"] or 0) > 0 or bool(app["submitted_at"])
    if status == _S.DOCUMENTS_READY and not submission_started:
        repo.transition_application(db, app_id, _S.DOCUMENTS_PENDING, actor="system",
                                    details={"reason": "regenerating documents"}, expected_current=status)
        status = _S.DOCUMENTS_PENDING
    moves_to_ready = status in _READY_FROM and not submission_started

    created: list[Path] = []
    try:
        result = _generate_and_store(ctx, job, profile, app_id=app_id, want_llm=want_llm, want_cover=want_cover,
                                     status=status, moves_to_ready=moves_to_ready, keep_links=keep_links, actor=actor,
                                     created=created)
    except Exception as exc:
        for p in created:
            p.unlink(missing_ok=True)
        error = exc if isinstance(exc, JobAgentError) else PermanentError(
            f"document generation failed: {type(exc).__name__}: {exc}")
        _record_failure(ctx, job, app_id, status, error, moves_to_ready=moves_to_ready, actor=actor)
        if error is exc:
            raise
        raise error from exc
    return result


def documents_sent(db: "Database", app: sqlite3.Row) -> bool:
    """True once the application's linked documents were (or may have been) sent to the employer.

    Then they are the record of what was submitted and must not be re-pointed:
    ``submitted_at`` is set, the status is ``submission_attempted`` or later, or a
    submission's outcome became unknown and you have not confirmed (``apply
    --confirm-not-submitted``) that nothing was sent.
    """
    if app["submitted_at"] or _S(app["status"]) in _SENT_STATES:
        return True
    unknown = db.scalar(
        "SELECT MAX(id) FROM application_events WHERE application_id = ? AND (event_type = ? OR "
        "(from_status = ? AND to_status = ?))",
        (app["id"], "submission_outcome_unknown", _S.SUBMISSION_ATTEMPTED.value, _S.NEEDS_MANUAL_ACTION.value))
    if unknown is None:
        return False
    confirmed = db.scalar("SELECT MAX(id) FROM application_events WHERE application_id = ? AND event_type = ?",
                          (app["id"], "confirmed_not_submitted"))
    return confirmed is None or int(unknown) > int(confirmed)


def _generate_and_store(ctx: "AppContext", job: JobListing, profile: CandidateProfile, *, app_id: int,
                        want_llm: bool, want_cover: bool, status: PipelineState, moves_to_ready: bool, actor: str,
                        created: list[Path], keep_links: bool = False) -> PreparedDocuments:
    db = ctx.db
    kw = keywords_for(job)
    profile_label = str(ctx.config.app.profile_path)
    assist: LLMAssist | None = None
    llm_note = ""
    if want_llm and not ctx.config.llm.enabled:
        llm_note = "local LLM is disabled in config (llm.enabled: false)"
    elif want_llm:
        assist, llm_note = get_assist(ctx, _error_sink(ctx, job, app_id))
    today = local_today(ctx.config.app.timezone)

    resume = generate_resume(profile, job, kw, max_bullets_per_role=ctx.config.documents.max_bullets_per_role,
                             assist=assist, profile_label=profile_label)
    if want_llm and assist is None and llm_note:
        resume.issues.append(Issue(code="llm_unavailable", severity="info", document="resume",
                                   message=f"{llm_note}; verbatim verified facts were used."))
    cover: GeneratedCoverLetter | None = None
    if want_cover:
        drafter = assist.draft_cover_letter if assist is not None and not assist.failed else None
        cover = build_cover_letter(profile, kw, job_title=job.title, employer=job.employer, on=today,
                                   drafter=drafter, model_label=assist.model if assist else None)

    formats = _formats(ctx)
    resume_files = _render_all(DocumentType.RESUME, resume.content, formats)
    cover_files = _render_all(DocumentType.COVER_LETTER, cover.content, formats) if cover else {}
    profile_hash = profile.profile_hash()

    with db.transaction():
        resume_infos = store_document_set(
            ctx, job=job, doc_type=DocumentType.RESUME, files=resume_files, status=resume.status,
            claims=resume.claims, issues=resume.issues, generator=resume.generator, profile_hash=profile_hash,
            on=today, created=created)
        cover_infos: list[DocumentInfo] = []
        if cover is not None:
            cover_infos = store_document_set(
                ctx, job=job, doc_type=DocumentType.COVER_LETTER, files=cover_files, status=cover.status,
                claims=cover.claims, issues=cover.issues, generator=cover.generator, profile_hash=profile_hash,
                on=today, created=created)
        details: dict[str, Any] = {
            "resume_version": resume_infos[0].version,
            "cover_letter_version": cover_infos[0].version if cover_infos else None,
            "resume_status": resume.status.value,
            "generator": resume.generator,
            "document_ids": [i.id for i in resume_infos + cover_infos],
        }
        if keep_links:  # already sent: the application keeps the documents actually used
            current = repo.get_application(db, app_id)
            details.update(linked=False,
                           kept_resume_document_id=current["resume_document_id"] if current else None,
                           kept_cover_letter_document_id=current["cover_letter_document_id"] if current else None)
            repo.add_application_event(db, app_id, NOT_LINKED_EVENT, actor="system", details=details)
        else:
            updates: dict[str, Any] = {"resume_document_id": preferred(resume_infos).id}  # type: ignore[union-attr]
            if cover_infos:
                updates["cover_letter_document_id"] = preferred(cover_infos).id  # type: ignore[union-attr]
            db.update("applications", app_id, updates)
            if moves_to_ready:
                repo.transition_application(
                    db, app_id, _S.DOCUMENTS_READY, actor="system", details=details, expected_current=status,
                    next_action=f"Review the generated documents (job-agent documents list {job.job_uid})")
            else:
                repo.add_application_event(db, app_id, "documents_regenerated", actor="system", details=details)
        repo.audit(db, actor=actor, action="documents_generated", entity_type="job", entity_id=job.job_uid,
                   outcome="success", details={**details, "application_id": app_id,
                                               "issues": sum(1 for i in resume.issues if i.severity != "info")})

    app_row = repo.get_application(db, app_id)
    all_issues = resume.issues + (cover.issues if cover else [])
    if keep_links:
        all_issues.append(Issue(
            code="documents_not_linked", severity="info", document="resume",
            message=f"This application was already submitted (status {status.value}). The new "
                    f"v{resume_infos[0].version} files are stored for your records only; the application still "
                    "points at the documents that were actually used."))
    overall = DocumentStatus.NEEDS_REVIEW if (resume.status == DocumentStatus.NEEDS_REVIEW or cover) \
        else resume.status
    return PreparedDocuments(
        job_id=job.id, job_uid=job.job_uid, application_id=app_id,
        application_status=_S(app_row["status"]) if app_row else status,
        resume=resume_infos, cover_letter=cover_infos, status=overall, issues=all_issues,
        claims=resume.claims + (cover.claims if cover else []),
        resume_version=resume_infos[0].version,
        cover_letter_version=cover_infos[0].version if cover_infos else None,
        generator=resume.generator,
        next_step=(_NOT_LINKED_STEP if keep_links else _next_step(job.job_uid, resume, cover, resume_infos)),
        documents_linked=not keep_links,
    )


_NOT_LINKED_STEP = ("Nothing to send: this application was already submitted, so it keeps the résumé/cover letter "
                    "that were used. The new versions are only for your records (see `job-agent documents list`).")


def _next_step(job_uid: str, resume: GeneratedResume, cover: GeneratedCoverLetter | None,
               infos: list[DocumentInfo]) -> str:
    placeholders = [i for i in resume.issues if i.code in {"profile_placeholder", "placeholder_text"}]
    if placeholders:
        return ("Fill in the placeholder fields listed above in your profile, then run "
                f"`job-agent prepare {job_uid}` again.")
    errors = [i for i in resume.issues if i.severity == "error"]
    doc_id = preferred(infos).id if infos else "?"  # type: ignore[union-attr]
    if errors:
        return (f"Fix the issues above before using these files; then review and run "
                f"`job-agent documents approve {doc_id}` or `job-agent documents reject {doc_id}`.")
    letter = " Review the cover letter carefully too." if cover else ""
    return (f"Open the files, check every line, then run `job-agent documents approve {doc_id}` "
            f"(or `reject`).{letter}")


def _error_sink(ctx: "AppContext", job: JobListing, app_id: int):
    def sink(what: str, exc: BaseException) -> None:
        log.warning("%s for job %s failed: %s", what, job.job_uid, type(exc).__name__)
        repo.record_error(ctx.db, component="resume:llm", error=exc, job_id=job.id, application_id=app_id,
                          details={"step": what}, retryable=False)

    return sink


def _record_failure(ctx: "AppContext", job: JobListing, app_id: int, status: PipelineState, error: JobAgentError,
                    *, moves_to_ready: bool, actor: str) -> None:
    db = ctx.db
    repo.record_error(db, component="resume", error=error, job_id=job.id, application_id=app_id,
                      details={"step": "prepare_documents"})
    details = {"error": f"{type(error).__name__}: {error}"[:500]}
    current = repo.get_application(db, app_id)
    current_status = _S(current["status"]) if current else status
    if moves_to_ready and current_status != _S.FAILED:
        repo.transition_application(db, app_id, _S.FAILED, actor="system", details=details,
                                    next_action="Document generation failed; see errors and retry `job-agent prepare`")
    else:
        repo.add_application_event(db, app_id, "documents_failed", actor="system", details=details)
    repo.audit(db, actor=actor, action="documents_generated", entity_type="job", entity_id=job.job_uid,
               outcome="failure", details={**details, "application_id": app_id})


# --------------------------------------------------------------------------- #
# Read helpers (stable API)
# --------------------------------------------------------------------------- #
def list_documents(db: "Database", job_id: int, *, documents_dir: Path | None = None) -> list[DocumentRecord]:
    return _list_documents(db, job_id, documents_dir=documents_dir)


def get_document(db: "Database", document_id: int, *, documents_dir: Path | None = None) -> DocumentRecord | None:
    return _get_document(db, document_id, documents_dir=documents_dir)
