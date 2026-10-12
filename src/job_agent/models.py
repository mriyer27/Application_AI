"""Shared enums, the application state machine, and cross-module data contracts.

This module is the contract between components. It must stay dependency-light
(pydantic only) and must not import other ``job_agent`` modules.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------- #
# Outcomes / error classification
# --------------------------------------------------------------------------- #
class ErrorKind(StrEnum):
    RECOVERABLE = "recoverable"
    PERMANENT = "permanent"
    AUTHORIZATION_REQUIRED = "authorization_required"
    HUMAN_ACTION_REQUIRED = "human_action_required"
    UNKNOWN_OUTCOME = "unknown_outcome"


class Outcome(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    RECOVERABLE_ERROR = "recoverable_error"
    PERMANENT_ERROR = "permanent_error"
    AUTHORIZATION_REQUIRED = "authorization_required"
    HUMAN_ACTION_REQUIRED = "human_action_required"
    UNKNOWN_OUTCOME = "unknown_outcome"
    SKIPPED = "skipped"
    DENIED = "denied"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"
    INTERRUPTED = "interrupted"


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
class WorkArrangement(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    INTERNSHIP = "internship"
    CONTRACT = "contract"
    TEMPORARY = "temporary"
    UNKNOWN = "unknown"


class SalaryPeriod(StrEnum):
    YEAR = "year"
    MONTH = "month"
    WEEK = "week"
    HOUR = "hour"
    UNKNOWN = "unknown"


class JobListingStatus(StrEnum):
    """Lifecycle of the *listing* itself (independent of our pipeline state)."""

    ACTIVE = "active"
    CLOSED = "closed"  # no longer returned by source / past closing date
    EXPIRED = "expired"


class MatchCategory(StrEnum):
    STRONG = "strong_match"
    POSSIBLE = "possible_match"
    WEAK = "weak_match"
    NEEDS_REVIEW = "needs_review"


# --------------------------------------------------------------------------- #
# Pipeline / application state machine
# --------------------------------------------------------------------------- #
class PipelineState(StrEnum):
    DISCOVERED = "discovered"
    SCORED = "scored"
    SHORTLISTED = "shortlisted"
    FILTERED_OUT = "filtered_out"
    DOCUMENTS_PENDING = "documents_pending"
    DOCUMENTS_READY = "documents_ready"
    IN_PROGRESS = "in_progress"
    AWAITING_APPROVAL = "awaiting_approval"
    SUBMISSION_ATTEMPTED = "submission_attempted"
    SUBMITTED = "submitted"
    CONFIRMATION_RECEIVED = "confirmation_received"
    ASSESSMENT_RECEIVED = "assessment_received"
    INTERVIEW_REQUESTED = "interview_requested"
    INTERVIEW_SCHEDULED = "interview_scheduled"
    REJECTED = "rejected"
    OFFER_RECEIVED = "offer_received"
    WITHDRAWN = "withdrawn"
    CLOSED = "closed"
    FAILED = "failed"
    NEEDS_MANUAL_ACTION = "needs_manual_action"


#: States a *job* row's ``pipeline_state`` may hold before an application exists.
JOB_STATES: frozenset[PipelineState] = frozenset(
    {
        PipelineState.DISCOVERED,
        PipelineState.SCORED,
        PipelineState.SHORTLISTED,
        PipelineState.FILTERED_OUT,
        PipelineState.CLOSED,
    }
)

#: States an *application* row may hold.
APPLICATION_STATES: frozenset[PipelineState] = frozenset(PipelineState) - {
    PipelineState.DISCOVERED,
    PipelineState.SCORED,
    PipelineState.SHORTLISTED,
    PipelineState.FILTERED_OUT,
}

_S = PipelineState
_POST_SUBMIT_OUTCOMES = {
    _S.CONFIRMATION_RECEIVED,
    _S.ASSESSMENT_RECEIVED,
    _S.INTERVIEW_REQUESTED,
    _S.INTERVIEW_SCHEDULED,
    _S.REJECTED,
    _S.OFFER_RECEIVED,
    _S.WITHDRAWN,
    _S.CLOSED,
}

ALLOWED_TRANSITIONS: dict[PipelineState, frozenset[PipelineState]] = {
    _S.DISCOVERED: frozenset({_S.SCORED, _S.FILTERED_OUT, _S.CLOSED}),
    _S.SCORED: frozenset({_S.SCORED, _S.SHORTLISTED, _S.FILTERED_OUT, _S.CLOSED}),
    _S.SHORTLISTED: frozenset({_S.SCORED, _S.FILTERED_OUT, _S.CLOSED}),
    _S.FILTERED_OUT: frozenset({_S.DISCOVERED, _S.SCORED, _S.SHORTLISTED, _S.CLOSED}),  # DISCOVERED: re-filter
    _S.DOCUMENTS_PENDING: frozenset(
        {_S.DOCUMENTS_READY, _S.FAILED, _S.NEEDS_MANUAL_ACTION, _S.WITHDRAWN, _S.CLOSED}
    ),
    _S.DOCUMENTS_READY: frozenset(
        {
            _S.DOCUMENTS_PENDING,  # regenerate
            _S.IN_PROGRESS,
            _S.AWAITING_APPROVAL,
            _S.NEEDS_MANUAL_ACTION,
            _S.SUBMITTED,  # user applied manually (actor must be "user")
            _S.WITHDRAWN,
            _S.CLOSED,
        }
    ),
    _S.IN_PROGRESS: frozenset(
        {_S.AWAITING_APPROVAL, _S.DOCUMENTS_READY, _S.NEEDS_MANUAL_ACTION, _S.FAILED, _S.WITHDRAWN, _S.CLOSED}
    ),
    _S.AWAITING_APPROVAL: frozenset(
        {
            _S.SUBMISSION_ATTEMPTED,  # requires a consumed approval
            _S.DOCUMENTS_READY,  # approval rejected -> back to editing
            _S.IN_PROGRESS,
            _S.NEEDS_MANUAL_ACTION,
            _S.SUBMITTED,  # user applied manually (actor must be "user")
            _S.WITHDRAWN,
            _S.CLOSED,
        }
    ),
    _S.SUBMISSION_ATTEMPTED: frozenset(
        {_S.SUBMITTED, _S.CONFIRMATION_RECEIVED, _S.NEEDS_MANUAL_ACTION, _S.FAILED}
    ),
    _S.SUBMITTED: frozenset(_POST_SUBMIT_OUTCOMES),
    _S.CONFIRMATION_RECEIVED: frozenset(_POST_SUBMIT_OUTCOMES - {_S.CONFIRMATION_RECEIVED}),
    _S.ASSESSMENT_RECEIVED: frozenset(_POST_SUBMIT_OUTCOMES - {_S.CONFIRMATION_RECEIVED}),
    _S.INTERVIEW_REQUESTED: frozenset(_POST_SUBMIT_OUTCOMES - {_S.CONFIRMATION_RECEIVED}),
    _S.INTERVIEW_SCHEDULED: frozenset(_POST_SUBMIT_OUTCOMES - {_S.CONFIRMATION_RECEIVED}),
    _S.OFFER_RECEIVED: frozenset({_S.WITHDRAWN, _S.CLOSED}),
    _S.REJECTED: frozenset({_S.CLOSED}),
    _S.WITHDRAWN: frozenset({_S.CLOSED}),
    _S.FAILED: frozenset(
        {_S.DOCUMENTS_PENDING, _S.DOCUMENTS_READY, _S.IN_PROGRESS, _S.NEEDS_MANUAL_ACTION, _S.WITHDRAWN, _S.CLOSED}
    ),
    _S.NEEDS_MANUAL_ACTION: frozenset(
        {
            _S.DOCUMENTS_PENDING,
            _S.DOCUMENTS_READY,
            _S.IN_PROGRESS,
            _S.AWAITING_APPROVAL,
            _S.SUBMITTED,  # user applied manually (actor must be "user")
            _S.CONFIRMATION_RECEIVED,  # e.g. confirmation email resolved an unknown outcome
            _S.FAILED,
            _S.WITHDRAWN,
            _S.CLOSED,
        }
    ),
    _S.CLOSED: frozenset(),
}

#: Transitions that only a human (actor == "user") may perform without
#: machine-verified evidence.
USER_ONLY_WITHOUT_EVIDENCE: frozenset[tuple[PipelineState, PipelineState]] = frozenset(
    {
        (_S.DOCUMENTS_READY, _S.SUBMITTED),
        (_S.AWAITING_APPROVAL, _S.SUBMITTED),
        (_S.NEEDS_MANUAL_ACTION, _S.SUBMITTED),
    }
)

#: Target states that may only be reached with an evidence payload (or by the user).
EVIDENCE_REQUIRED: frozenset[PipelineState] = frozenset(
    {
        _S.SUBMITTED,
        _S.CONFIRMATION_RECEIVED,
        _S.ASSESSMENT_RECEIVED,
        _S.INTERVIEW_REQUESTED,
        _S.INTERVIEW_SCHEDULED,
        _S.REJECTED,
        _S.OFFER_RECEIVED,
    }
)


def can_transition(current: PipelineState, target: PipelineState) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


# --------------------------------------------------------------------------- #
# Approvals, documents, email, follow-ups
# --------------------------------------------------------------------------- #
class ApprovalType(StrEnum):
    SUBMIT_APPLICATION = "submit_application"
    SEND_EMAIL = "send_email"
    SEND_FOLLOWUP = "send_followup"
    CONFIG_CHANGE = "config_change"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CONSUMED = "consumed"  # approved and used exactly once
    SUPERSEDED = "superseded"  # payload changed; a new request replaced it


class DocumentType(StrEnum):
    RESUME = "resume"
    COVER_LETTER = "cover_letter"


class DocumentStatus(StrEnum):
    DRAFT = "draft"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class EmailCategory(StrEnum):
    APPLICATION_CONFIRMATION = "application_confirmation"
    RECRUITER_MESSAGE = "recruiter_message"
    INTERVIEW_INVITATION = "interview_invitation"
    SCHEDULING_REQUEST = "scheduling_request"
    ASSESSMENT = "assessment"
    REJECTION = "rejection"
    OFFER = "offer"
    FOLLOW_UP_OPPORTUNITY = "follow_up_opportunity"
    OTHER = "other"


class FollowupType(StrEnum):
    APPLICATION_NO_RESPONSE = "application_no_response"
    RECRUITER_REPLY = "recruiter_reply"
    INTERVIEW_UPCOMING = "interview_upcoming"
    ASSESSMENT_DUE = "assessment_due"
    DEADLINE = "deadline"
    STALE_APPLICATION = "stale_application"


class FollowupStatus(StrEnum):
    PENDING = "pending"
    DRAFTED = "drafted"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    COMPLETED = "completed"
    DISMISSED = "dismissed"


class Permission(StrEnum):
    READ_JOBS = "read_jobs"
    GENERATE_DOCUMENTS = "generate_documents"
    PREPARE_APPLICATIONS = "prepare_applications"
    SUBMIT_APPLICATIONS = "submit_applications"
    READ_EMAILS = "read_emails"
    DRAFT_EMAILS = "draft_emails"
    SEND_EMAILS = "send_emails"
    CHANGE_CONFIG = "change_config"


# --------------------------------------------------------------------------- #
# Cross-module data contracts
# --------------------------------------------------------------------------- #
class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class NormalizedJob(_Model):
    """A job listing normalized from any source, before persistence.

    Only facts present in the source may be set. Unknown values stay ``None`` /
    ``UNKNOWN`` -- never guess salary, sponsorship, or arrangement.
    """

    source_name: str  # e.g. "greenhouse"
    source_identifier: str  # e.g. the board token / company slug
    external_id: str  # id within that source
    requisition_id: str | None = None
    employer: str
    title: str
    location: str | None = None
    work_arrangement: WorkArrangement = WorkArrangement.UNKNOWN
    employment_type: EmploymentType = EmploymentType.UNKNOWN
    experience_level: str | None = None
    department: str | None = None
    description_text: str = ""
    required_qualifications: list[str] = Field(default_factory=list)
    preferred_qualifications: list[str] = Field(default_factory=list)
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: SalaryPeriod = SalaryPeriod.UNKNOWN
    salary_is_base: bool | None = None  # None = source did not say
    salary_text: str | None = None
    posted_at: str | None = None  # ISO-8601 UTC
    closing_at: str | None = None
    job_url: str | None = None
    application_url: str | None = None
    sponsorship_info: str | None = None  # verbatim excerpt from listing, never inferred
    work_authorization_requirements: str | None = None  # verbatim excerpt
    raw_metadata: dict[str, Any] = Field(default_factory=dict)


class FactorScore(_Model):
    name: str
    weight: float  # 0..100, share of total
    score: float  # 0..1
    explanation: str


class EligibilityFlag(_Model):
    code: str  # e.g. "no_sponsorship", "citizenship_required", "clearance_required"
    severity: str  # "info" | "review" | "blocker"
    excerpt: str  # verbatim text that triggered the flag
    message: str


class ScoreResult(_Model):
    job_id: int
    overall_score: float  # 0..100
    category: MatchCategory
    shortlisted: bool
    factors: list[FactorScore]
    matching_qualifications: list[str] = Field(default_factory=list)
    missing_qualifications: list[str] = Field(default_factory=list)
    potential_disqualifiers: list[str] = Field(default_factory=list)
    eligibility_flags: list[EligibilityFlag] = Field(default_factory=list)
    evidence: list[dict[str, str]] = Field(default_factory=list)  # {"requirement","fact_id","fact_text"}
    reasons_to_apply: list[str] = Field(default_factory=list)
    reasons_not_to_apply: list[str] = Field(default_factory=list)
    recommended_next_action: str = ""
    scoring_version: str
    used_llm: bool = False
    llm_model: str | None = None


# --------------------------------------------------------------------------- #
# Local-LLM output contracts (validated by deterministic code before use)
# --------------------------------------------------------------------------- #
class CodingIntensity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


class JobAnalysis(_Model):
    """Validated local-LLM analysis of one job description.

    Produced by ``job_agent.llm.analysis.analyze_job``. Before this object is
    returned, the llm module drops any ``required_skills``/``preferred_skills``
    not literally present in the description and any ``sponsorship_statements``
    that are not verbatim substrings, so consumers can trust that these come
    from the listing (not from the model's imagination). It says nothing about
    whether the *candidate* has a skill -- that is decided deterministically
    against the verified profile.
    """

    summary: str = Field(max_length=800)
    role_family: str = Field(max_length=80)  # e.g. "business analysis", "data analytics", "software engineering"
    seniority: str = Field(max_length=40)  # e.g. "entry", "mid", "senior", "unknown"
    years_experience_required: int | None = Field(default=None, ge=0, le=40)
    required_skills: list[str] = Field(default_factory=list, max_length=40)
    preferred_skills: list[str] = Field(default_factory=list, max_length=40)
    responsibilities: list[str] = Field(default_factory=list, max_length=20)
    coding_intensity: CodingIntensity = CodingIntensity.UNKNOWN
    ai_relevance: float = Field(0.0, ge=0, le=1)
    business_relevance: float = Field(0.0, ge=0, le=1)
    analytics_relevance: float = Field(0.0, ge=0, le=1)
    product_relevance: float = Field(0.0, ge=0, le=1)
    sponsorship_statements: list[str] = Field(default_factory=list, max_length=10)  # verbatim excerpts
    red_flags: list[str] = Field(default_factory=list, max_length=10)
    fit_explanation: str = Field("", max_length=1500)
    model: str = ""
    prompt_version: str = ""


class EmailClassification(_Model):
    category: EmailCategory
    confidence: float = Field(ge=0, le=1)
    summary: str = Field("", max_length=300)
    employer_guess: str | None = Field(default=None, max_length=120)  # untrusted; used only as a matching hint
    requires_attention: bool = False
    classifier: str = "rules"  # "rules" | "llm:<model>"
