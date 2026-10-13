"""Shared helpers for the applications tests (imported by test_applications_*.py; contains no tests)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from job_agent.browser.fakes import FakeBrowser, FakeForm
from job_agent.browser.forms import FieldOption, FormField
from job_agent.database import repo
from job_agent.models import PipelineState
from job_agent.utils.time import utcnow_iso

URL = "https://jobs.example-employer.com/apply/123"


def opt(*labels: str) -> tuple[FieldOption, ...]:
    return tuple(FieldOption(label=l, value=l.lower().replace(" ", "_")) for l in labels)


def standard_fields() -> list[FormField]:
    return [
        FormField("first_name", "First name", "text", required=True, locator="#first_name"),
        FormField("last_name", "Last name", "text", required=True, locator="#last_name"),
        FormField("email", "Email", "email", required=True, locator="#email"),
        FormField("phone", "Phone", "tel", locator="#phone"),
        FormField("resume", "Resume/CV", "file", required=True, is_file=True, accept=".pdf,.docx", locator="#resume"),
        FormField("source", "How did you hear about us?", "select", options=opt("LinkedIn", "Company careers page"),
                  locator="#source"),
        FormField("sponsorship", "Will you now or in the future require visa sponsorship?", "select", required=True,
                  options=opt("Yes", "No"), locator="#sponsorship"),
        FormField("work_auth", "Are you legally authorized to work in the United States?", "radio", required=True,
                  options=opt("Yes", "No"), locator="input[name=work_auth]"),
        FormField("gender", "Gender", "select", options=opt("Female", "Male", "Decline to self-identify"),
                  locator="#gender"),
        FormField("certify", "I certify that the information provided is true.", "checkbox", required=True,
                  locator="#certify"),
        FormField("salary", "Salary expectations", "text", locator="#salary"),
        FormField("why", "Why do you want to work here?", "textarea", locator="#why"),
    ]


def simple_fields() -> list[FormField]:
    """A small form that can be fully resolved for browser submission."""
    return [
        FormField("first_name", "First name", "text", required=True, locator="#first_name"),
        FormField("last_name", "Last name", "text", required=True, locator="#last_name"),
        FormField("email", "Email", "email", required=True, locator="#email"),
        FormField("resume", "Resume", "file", required=True, is_file=True, accept=".pdf", locator="#resume"),
        FormField("sponsorship", "Will you now or in the future require visa sponsorship?", "select", required=True,
                  options=opt("Yes", "No"), locator="#sponsorship"),
        FormField("certify", "I certify that the information above is accurate.", "checkbox", required=True,
                  locator="#certify"),
    ]


def update_profile(ctx, mutate) -> None:
    path = ctx.config.profile_path
    data = yaml.safe_load(path.read_text())
    mutate(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    ctx.reload_profile()


def add_sponsorship_answer(ctx, answer: str = "No") -> None:
    def mutate(d: dict[str, Any]) -> None:
        d["verified_answers"].append({"id": "sponsorship", "question_patterns": ["require visa sponsorship"],
                                      "answer": answer, "is_legal_declaration": True})
    update_profile(ctx, mutate)


def _write_doc(ctx, job_id: int, doc_type: str, fmt: str, content: bytes, version: int = 1) -> int:
    rel = Path("Acme-Analytics") / f"Acme-Analytics_Business-Analyst_{doc_type}_v{version}.{fmt}"
    path = ctx.config.documents_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return ctx.db.insert("document_versions", {
        "job_id": job_id, "doc_type": doc_type, "version": version, "format": fmt, "file_name": rel.name,
        "relative_path": rel.as_posix(), "sha256": hashlib.sha256(content).hexdigest(), "status": "approved",
        "generator": "rules", "profile_hash": "test", "created_at": utcnow_iso(),
    })


def setup_application(ctx, make_job, *, url: str = URL, cover_letter: bool = True,
                      formats: tuple[str, ...] = ("docx", "pdf"), **job_overrides: Any) -> dict[str, Any]:
    """Job + application in DOCUMENTS_READY with real résumé (and cover letter) files."""
    db = ctx.db
    job_id = make_job(job_url=url, job_url_canonical=url, application_url=url, **job_overrides)
    app_id = repo.create_application(db, job_id, actor="system", application_url=url)
    ids = {}
    for fmt in formats:
        ids[fmt] = _write_doc(ctx, job_id, "resume", fmt, f"%PDF-1.4 resume {fmt}".encode())
    cover_id = _write_doc(ctx, job_id, "cover_letter", "md", b"Dear hiring team,\n\nI am applying.") \
        if cover_letter else None
    db.update("applications", app_id, {"resume_document_id": ids[formats[0]], "cover_letter_document_id": cover_id})
    repo.transition_application(db, app_id, PipelineState.DOCUMENTS_READY, actor="system")
    app = repo.get_application(db, app_id)
    job = db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    return {"job_id": job_id, "app_id": app_id, "app_uid": app["application_uid"], "job_uid": job["job_uid"],
            "resume_ids": ids, "cover_id": cover_id, "url": url}


def fake_for(url: str = URL, fields: list[FormField] | None = None, **kw: Any) -> FakeBrowser:
    return FakeBrowser(FakeForm(url=url, fields=fields if fields is not None else standard_fields(), **kw))


def status_of(ctx, app_id: int) -> str:
    return repo.get_application(ctx.db, app_id)["status"]
