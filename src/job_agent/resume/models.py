"""Data models for generated documents (content, claims, validation issues, results)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from job_agent.models import DocumentStatus, DocumentType, PipelineState

Severity = Literal["error", "review", "info"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Issue(_M):
    """One validation finding.

    ``error``: an unverifiable or forbidden claim (a rewording with any error is
    rejected; a document with one needs review). ``review``: something the human
    must fix or check (e.g. placeholder text in the profile). ``info``: context
    only (e.g. a rejected LLM rewording that was replaced by the verbatim fact).
    """

    code: str
    severity: Severity = "error"
    message: str
    location: str | None = None
    document: Literal["resume", "cover_letter"] | None = None


class RejectedRewording(_M):
    text: str  # truncated, for transparency only; never used in a document
    reasons: list[str]


class Claim(_M):
    """A statement in a generated document and the verified fact(s) it comes from."""

    text: str
    fact_ids: list[str]
    generator: str = "rules"  # rules | llm:<model>
    section: str = ""  # e.g. "experience:exp_x", "education:edu_ms", "cover_letter:body"
    rejected_rewording: RejectedRewording | None = None


# --------------------------------------------------------------------------- #
# Job input
# --------------------------------------------------------------------------- #
class JobListing(_M):
    """The subset of a stored job used for tailoring. All text here is untrusted."""

    id: int
    job_uid: str
    employer: str
    title: str
    description_text: str = ""
    required_qualifications: list[str] = Field(default_factory=list)
    preferred_qualifications: list[str] = Field(default_factory=list)
    matching_qualifications: list[str] = Field(default_factory=list)  # from job_scores, if scored
    application_url: str | None = None
    job_url: str | None = None
    listing_status: str = "active"


# --------------------------------------------------------------------------- #
# Résumé content
# --------------------------------------------------------------------------- #
class Header(_M):
    full_name: str
    email: str
    phone: str | None = None
    location: str | None = None
    links: list[str] = Field(default_factory=list)

    def contact_parts(self) -> list[str]:
        return [p for p in (self.location, self.phone, self.email, *self.links) if p]


class Bullet(_M):
    text: str
    fact_ids: list[str]
    generator: str = "rules"
    relevance: float = 0.0
    matched_keywords: list[str] = Field(default_factory=list)
    rejected_rewording: RejectedRewording | None = None


class EducationEntry(_M):
    id: str
    institution: str
    degree: str
    field: str | None = None
    location: str | None = None
    dates: str  # built from the profile's verbatim start/end values
    gpa: str | None = None
    bullets: list[Bullet] = Field(default_factory=list)

    @property
    def degree_line(self) -> str:
        return f"{self.degree}, {self.field}" if self.field else self.degree


class ExperienceEntry(_M):
    id: str
    kind: Literal["experience", "project"] = "experience"
    title: str  # job title, or project name
    organization: str | None = None  # employer (None for projects)
    team: str | None = None
    location: str | None = None
    dates: str | None = None
    bullets: list[Bullet] = Field(default_factory=list)


class ResumeContent(_M):
    header: Header
    summary: str | None = None
    education: list[EducationEntry] = Field(default_factory=list)
    experience: list[ExperienceEntry] = Field(default_factory=list)
    projects: list[ExperienceEntry] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)

    def all_bullets(self) -> list[tuple[str, Bullet]]:
        out: list[tuple[str, Bullet]] = []
        for e in self.education:
            out.extend((f"education:{e.id}", b) for b in e.bullets)
        for x in self.experience:
            out.extend((f"experience:{x.id}", b) for b in x.bullets)
        for p in self.projects:
            out.extend((f"project:{p.id}", b) for b in p.bullets)
        return out

    def plain_text(self) -> str:
        """All visible text, for document-level checks."""
        parts: list[str] = [self.header.full_name, *self.header.contact_parts()]
        if self.summary:
            parts.append(self.summary)
        for e in self.education:
            parts += [e.institution, e.degree_line, e.location or "", e.dates, e.gpa or ""]
        for x in [*self.experience, *self.projects]:
            parts += [x.title, x.organization or "", x.team or "", x.location or "", x.dates or ""]
        parts += [b.text for _, b in self.all_bullets()]
        parts += self.skills + self.certifications
        return "\n".join(p for p in parts if p)


class CoverLetterContent(_M):
    header: Header
    date_text: str
    employer: str
    job_title: str
    greeting: str = "Dear Hiring Team,"
    paragraphs: list[str]
    closing: str = "Sincerely,"
    signature: str

    def plain_text(self) -> str:
        return "\n".join([self.header.full_name, *self.header.contact_parts(), self.date_text, self.greeting,
                          *self.paragraphs, self.closing, self.signature])


# --------------------------------------------------------------------------- #
# Generation results
# --------------------------------------------------------------------------- #
class GeneratedResume(_M):
    content: ResumeContent
    claims: list[Claim]
    issues: list[Issue]
    status: DocumentStatus
    generator: str  # rules | llm:<model>


class GeneratedCoverLetter(_M):
    content: CoverLetterContent
    claims: list[Claim]
    issues: list[Issue]
    status: DocumentStatus = DocumentStatus.NEEDS_REVIEW  # always reviewed by the human
    generator: str = "rules"


class DocumentInfo(_M):
    id: int
    job_id: int
    doc_type: DocumentType
    version: int
    format: Literal["docx", "pdf", "md"]
    file_name: str
    relative_path: str  # relative to config.documents_dir
    path: str | None = None  # absolute path (when the config is known)
    sha256: str
    status: DocumentStatus
    generator: str
    profile_hash: str
    created_at: str
    reviewed_at: str | None = None


class DocumentRecord(DocumentInfo):
    claims: list[dict[str, Any]] = Field(default_factory=list)
    validation_issues: list[dict[str, Any]] = Field(default_factory=list)


class PreparedDocuments(_M):
    job_id: int
    job_uid: str
    application_id: int
    application_status: PipelineState
    resume: list[DocumentInfo]
    cover_letter: list[DocumentInfo] = Field(default_factory=list)
    status: DocumentStatus  # needs_review if any generated document needs review
    issues: list[Issue] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    resume_version: int
    cover_letter_version: int | None = None
    generator: str = "rules"
    next_step: str = ""
