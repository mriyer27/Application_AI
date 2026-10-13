"""The approval payload: exactly what you approve, recomputable at submit time.

``build_payload(db, application_id)`` is a pure function of database state
(application, job, documents, prepared answers and the latest ``form_prepared``
event). ``repo.create_approval`` stores its SHA-256; at submission the payload
is rebuilt and must hash to the approved value, so any change after approval
(an edited answer, a regenerated document) requires a new approval.
"""

from __future__ import annotations

from typing import Any

from job_agent.applications import documents as docs
from job_agent.applications import repository as arepo
from job_agent.applications.field_mapping import VERIFIED_SOURCE_PREFIXES, FieldCategory, classify_field
from job_agent.database import Database, repo
from job_agent.errors import ApprovalError
from job_agent.models import ApprovalType

PAYLOAD_VERSION = 1
FORM_PREPARED_EVENT = "form_prepared"
#: ``document_versions.status`` of a version you rejected: never uploaded, approved or offered.
REJECTED_DOCUMENT_STATUS = "rejected"


def _fill_value(row: Any) -> str | None:
    if row["confidence"] == "verified" and row["answer"]:
        return str(row["answer"])
    return None


def _is_verified_source(source: str | None) -> bool:
    return bool(source) and any(str(source).startswith(p) for p in VERIFIED_SOURCE_PREFIXES)


def usable_document(db: Database, doc: docs.DocumentRef | None) -> docs.DocumentRef | None:
    """``doc`` unless you rejected it; then the latest version of the same job's document that you did
    not reject (preferred format first), or ``None`` if every version was rejected."""
    if doc is None or doc.status != REJECTED_DOCUMENT_STATUS:
        return doc
    row = db.query_one(
        "SELECT * FROM document_versions WHERE job_id = ? AND doc_type = ? AND status != ? "
        "ORDER BY version DESC, id DESC LIMIT 1",
        (doc.job_id, doc.doc_type, REJECTED_DOCUMENT_STATUS),
    )
    if row is None:
        return None
    fallback = docs.DocumentRef.from_row(row)
    return next((v for v in docs.variants(db, fallback) if v.status != REJECTED_DOCUMENT_STATUS), fallback)


def chosen_documents(db: Database, app: Any, answers: list[Any]) -> tuple[docs.DocumentRef | None,
                                                                            docs.DocumentRef | None]:
    """Résumé / cover letter that will be used (the uploaded variant if a form field takes one).

    A version you rejected is shown as such (its status is part of the payload, so
    approval is refused); without an upload field the latest non-rejected version is used.
    """
    picked: dict[str, docs.DocumentRef] = {}
    for row in answers:
        source = row["answer_source"] or ""
        if source.startswith("document:") and source[9:].isdigit() and _fill_value(row):
            doc = docs.get_document(db, int(source[9:]))
            if doc and doc.doc_type not in picked and row["field_type"] == "file":
                picked[doc.doc_type] = doc
    result = []
    for doc_type, column in (("resume", "resume_document_id"), ("cover_letter", "cover_letter_document_id")):
        if doc_type in picked:
            result.append(picked[doc_type])
            continue
        base = usable_document(db, docs.get_document(db, app[column]))
        if base is None:
            result.append(None)
            continue
        variants = [v for v in docs.variants(db, base) if v.status != REJECTED_DOCUMENT_STATUS]
        result.append(variants[0] if variants else base)
    return result[0], result[1]


def build_payload(db: Database, application_id: int) -> dict[str, Any]:
    app = repo.get_application(db, application_id)
    if app is None:
        raise arepo.NotFound(f"application {application_id} does not exist")
    job = arepo.get_job(db, int(app["job_id"]))
    answers = arepo.get_answers(db, application_id)
    prepared = arepo.latest_event_details(db, application_id, FORM_PREPARED_EVENT)
    resume_doc, cover_doc = chosen_documents(db, app, answers)

    filled, legal, unanswered, review, agreements, blank = [], [], [], [], [], []
    for row in answers:
        key, label = row["field_key"], row["field_label"]
        category = classify_field(label, key, row["field_type"])
        value = _fill_value(row)
        source = row["answer_source"]
        if value is not None:
            filled.append({"field_key": key, "label": label, "answer": value, "source": source})
        elif row["answer_source"] == "user" and row["answer"] == "":
            blank.append({"field_key": key, "label": label})
        if row["is_legal_declaration"]:
            verified = value is not None and _is_verified_source(source)
            if row["answer_source"] == "user" and row["answer"] == "" and not row["required"]:
                verified = True  # you explicitly chose to leave an optional question blank
            legal.append({"field_key": key, "label": label, "answer": value, "source": source if value else None,
                          "required": bool(row["required"]), "verified": verified,
                          "note": row["review_reason"]})
        if row["required"] and value is None:
            unanswered.append({"field_key": key, "label": label, "reason": row["review_reason"]})
        if category is FieldCategory.AGREEMENT:
            agreements.append({"field_key": key, "label": label, "answer": value, "source": source if value else None})
        if row["requires_review"] and not row["is_legal_declaration"]:
            item: dict[str, Any] = {"field_key": key, "label": label, "category": category.value,
                                    "required": bool(row["required"]), "reason": row["review_reason"]}
            if row["confidence"] == "uncertain" and row["answer"]:
                item["suggested_answer"] = row["answer"]
                item["suggestion_source"] = source
            review.append(item)

    return {
        "payload_version": PAYLOAD_VERSION,
        "application_uid": app["application_uid"],
        "job_uid": job["job_uid"],
        "employer": job["employer"],
        "role": job["title"],
        "application_url": app["application_url"] or job["application_url"] or job["job_url"],
        "resume": _doc_summary(resume_doc),
        "cover_letter": _doc_summary(cover_doc),
        "answers": filled,
        "legal_declarations": legal,
        "unanswered_required": unanswered,
        "requires_review": review,
        "agreements": agreements,
        "left_blank_by_you": blank,
        "validation_errors": list(prepared.get("validation_errors") or []),
        "screenshot": prepared.get("screenshot"),
        "prepared_at": app["prepared_at"],
        "work_authorization_reference": prepared.get("eligibility_reference") or {},
    }


def _doc_summary(doc: docs.DocumentRef | None) -> dict[str, Any] | None:
    if doc is None:
        return None
    return {"document_id": doc.id, "file": doc.file_name, "format": doc.format, "sha256": doc.sha256,
            "status": doc.status}


def submission_blockers(payload: dict[str, Any]) -> list[str]:
    """Why the *browser* may not submit this payload (manual submission is always possible)."""
    problems = []
    if not payload.get("resume"):
        problems.append("no résumé document")
    for kind, label in (("resume", "résumé"), ("cover_letter", "cover letter")):
        doc = payload.get(kind)
        if doc and doc.get("status") == REJECTED_DOCUMENT_STATUS:
            problems.append(f"the {label} {doc.get('file', '')} was rejected by you; it is never uploaded "
                            "(run job-agent prepare to generate a new version)")
        elif doc and doc.get("status") != "approved":
            problems.append(f"the {label} has not been approved by you yet "
                            f"(job-agent documents approve {doc.get('document_id', '<document-id>')})")
    for item in payload.get("unanswered_required") or []:
        problems.append(f"required field unanswered: {item['label']} [{item['field_key']}]")
    for item in payload.get("requires_review") or []:
        problems.append(f"needs your review: {item['label']} [{item['field_key']}]")
    for item in payload.get("legal_declarations") or []:
        if not item.get("verified"):
            problems.append(f"legal declaration not answered from a verified source: {item['label']} "
                            f"[{item['field_key']}]")
    if payload.get("validation_errors"):
        problems.append("the form showed validation errors when it was prepared")
    return list(dict.fromkeys(problems))


def summary_line(payload: dict[str, Any]) -> str:
    return (f"Submit application to {payload['employer']} for {payload['role']}: "
            f"{len(payload['answers'])} answer(s), {len(payload['legal_declarations'])} legal declaration(s), "
            f"{len(payload['requires_review'])} item(s) to review, "
            f"{len(payload['unanswered_required'])} required unanswered")


def approval_view(db: Database, approval_uid: str) -> dict[str, Any]:
    """Sectioned, display-ready view of a submit-application approval (CLI / dashboard)."""
    row = repo.get_approval(db, approval_uid)
    if row is None:
        raise ApprovalError(f"approval {approval_uid} not found")
    if row["request_type"] != ApprovalType.SUBMIT_APPLICATION.value or row["application_id"] is None:
        raise ApprovalError(f"approval {approval_uid} is a {row['request_type']} request, not an application")
    payload = repo.approval_payload(row)
    app = repo.get_application(db, int(row["application_id"]))
    review = list(payload.get("requires_review") or [])
    numbered = {r["field_key"]: i for i, r in enumerate(arepo.get_answers(db, int(row["application_id"])), start=1)}

    def num(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**it, "ref": f"#{numbered[it['field_key']]}" if it.get("field_key") in numbered else None}
                for it in items]

    return {
        "approval_uid": row["approval_uid"],
        "status": row["status"],
        "requested_at": row["requested_at"],
        "expires_at": row["expires_at"],
        "decided_at": row["decided_at"],
        "decided_by": row["decided_by"],
        "decision_note": row["decision_note"],
        "summary": row["summary"],
        "payload_hash": row["payload_hash"],
        "application_uid": payload.get("application_uid") or (app["application_uid"] if app else None),
        "application_status": app["status"] if app else None,
        "sections": {
            "employer_role": {"employer": payload.get("employer"), "role": payload.get("role"),
                              "job_uid": payload.get("job_uid")},
            "url": payload.get("application_url"),
            "documents": {"resume": payload.get("resume"), "cover_letter": payload.get("cover_letter")},
            "answers": num(list(payload.get("answers") or [])),
            "legal_declarations": num(list(payload.get("legal_declarations") or [])),
            "unanswered": num(list(payload.get("unanswered_required") or [])),
            "uncertain": num(review),
            "agreements": num(list(payload.get("agreements") or [])),
            "left_blank_by_you": num(list(payload.get("left_blank_by_you") or [])),
            "validation_errors": list(payload.get("validation_errors") or []),
            "screenshot": payload.get("screenshot"),
            "work_authorization_reference": payload.get("work_authorization_reference") or {},
        },
        "browser_submission_blockers": submission_blockers(payload),
    }
