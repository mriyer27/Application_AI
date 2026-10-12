"""Deterministic, evidence-based application status updates from email.

An email can change an application's status only when *all* of these hold:
  * the category came from the deterministic rules with confidence >= 0.8
    (an LLM label never changes a status by itself),
  * the message is linked to an existing application with association
    confidence >= 0.8 and the match is unambiguous,
  * no prompt-injection hints were found in the message,
  * ``models.can_transition`` allows the move.

The transition goes through ``repo.transition_application`` (actor ``email``)
with an ``{"type": "email", ...}`` evidence record. Interviews are never
accepted, nothing is negotiated, nothing is replied to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from job_agent.database import Database, repo
from job_agent.models import EmailCategory, PipelineState, can_transition

C = EmailCategory
S = PipelineState

CATEGORY_TARGET: dict[EmailCategory, PipelineState] = {
    C.APPLICATION_CONFIRMATION: S.CONFIRMATION_RECEIVED,
    C.REJECTION: S.REJECTED,
    C.INTERVIEW_INVITATION: S.INTERVIEW_REQUESTED,
    C.SCHEDULING_REQUEST: S.INTERVIEW_REQUESTED,
    C.ASSESSMENT: S.ASSESSMENT_RECEIVED,
    C.OFFER: S.OFFER_RECEIVED,
}

#: A confirmation email may only resolve these states (incl. an unknown submission outcome).
CONFIRMATION_FROM: frozenset[PipelineState] = frozenset({S.SUBMISSION_ATTEMPTED, S.SUBMITTED, S.NEEDS_MANUAL_ACTION})

_NEXT_ACTION: dict[PipelineState, str] = {
    S.CONFIRMATION_RECEIVED: "Wait for a response; a follow-up will be suggested if there is none",
    S.REJECTED: "No action needed (rejected)",
    S.INTERVIEW_REQUESTED: "Reply to the interview request yourself (see the suggested reply)",
    S.ASSESSMENT_RECEIVED: "Complete the assessment before its deadline",
    S.OFFER_RECEIVED: "Review the offer yourself; the agent never accepts or negotiates",
}


@dataclass
class StatusOutcome:
    applied: PipelineState | None = None
    previous: PipelineState | None = None
    skipped_reason: str | None = None


def evidence_for(*, gmail_message_id: str, subject: str | None, received_at: str | None,
                 category: EmailCategory) -> dict[str, Any]:
    return {"type": "email", "gmail_message_id": gmail_message_id, "subject": (subject or "")[:300],
            "received_at": received_at, "category": category.value}


def check_transition(current: PipelineState, category: EmailCategory) -> tuple[PipelineState | None, str | None]:
    """Target state for this email category, or the reason it does not apply."""
    target = CATEGORY_TARGET.get(category)
    if target is None:
        return None, f"category {category.value} does not change application status"
    if current == target:
        return None, f"application already {target.value}"
    if target == S.CONFIRMATION_RECEIVED and current not in CONFIRMATION_FROM:
        return None, f"confirmation email does not apply to an application in {current.value}"
    if not can_transition(current, target):
        return None, f"{current.value} -> {target.value} is not an allowed transition"
    return target, None


def apply_email_status(db: Database, *, application_id: int, association_id: int, category: EmailCategory,
                       evidence: dict[str, Any], actor: str, email_id: int) -> StatusOutcome:
    """Apply (or record why not) the status implied by an email. Caller decides eligibility."""
    row = repo.get_application(db, application_id)
    if row is None:
        return StatusOutcome(skipped_reason="application no longer exists")
    current = PipelineState(row["status"])
    target, reason = check_transition(current, category)
    if target is None:
        if reason and not reason.startswith("application already") and category in CATEGORY_TARGET:
            repo.audit(db, actor=actor, action="email_status_update_skipped", entity_type="application",
                       entity_id=application_id, outcome="skipped",
                       details={"email_id": email_id, "category": category.value, "from": current.value,
                                "reason": reason})
        return StatusOutcome(previous=current, skipped_reason=reason)
    with db.transaction():
        previous = repo.transition_application(
            db, application_id, target, actor=actor, evidence=evidence,
            details={"email_id": email_id, "source": "gmail"}, next_action=_NEXT_ACTION.get(target),
            expected_current=current,
        )
        db.execute("UPDATE email_associations SET status_applied = ? WHERE id = ?", (target.value, association_id))
    return StatusOutcome(applied=target, previous=previous)
