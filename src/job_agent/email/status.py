"""Deterministic, evidence-based application status updates from email.

An email changes an application's status *automatically* (actor ``email``)
only when ``email.service`` found all of these to hold:
  * the category came from the deterministic rules with confidence >= 0.8
    (an LLM label never changes a status by itself),
  * Gmail verified the sender (DKIM / DMARC pass aligned with the From domain),
  * the message is not bulk / promotional mail and has no prompt-injection hints,
  * the message is linked to an existing application with association
    confidence >= 0.8 and the match is unambiguous (no other role named),
  * ``check_transition`` below allows the move for that actor.

``check_transition`` mirrors every rule ``repo.transition_application``
enforces, so a disallowed move becomes a recorded *skip reason* instead of an
exception. In particular an automatic update never:
  * resolves ``NEEDS_MANUAL_ACTION`` (an unknown submission outcome, or an
    application that was never submitted) -- you confirm those yourself with
    ``job-agent emails associate ... --apply-status`` or
    ``job-agent applications set-status``,
  * touches an application with no record of a submission,
  * makes a correction between outcomes that only you may make.

The transition goes through ``repo.transition_application`` with an
``{"type": "email", "gmail_message_id": ...}`` evidence record. Interviews are
never accepted, nothing is negotiated, nothing is replied to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from job_agent.database import Database, repo
from job_agent.models import (
    USER_CORRECTIONS,
    USER_ONLY_WITHOUT_EVIDENCE,
    EmailCategory,
    PipelineState,
    can_transition,
)

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

#: States a confirmation email may move to CONFIRMATION_RECEIVED automatically.
AUTO_CONFIRMATION_FROM: frozenset[PipelineState] = frozenset({S.SUBMISSION_ATTEMPTED, S.SUBMITTED})
#: ... and when you confirm it yourself (incl. an unknown outcome you have checked).
CONFIRMATION_FROM: frozenset[PipelineState] = AUTO_CONFIRMATION_FROM | {S.NEEDS_MANUAL_ACTION}

#: Actor whose decisions count as the user's in ``repo.transition_application``.
USER_ACTOR = "user"

CONFIRM_YOURSELF = ("confirm it yourself: job-agent emails associate <email id> <application uid> --apply-status, "
                    "or job-agent applications set-status")

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


def check_transition(current: PipelineState, category: EmailCategory, *, actor: str = "email",
                     submission_attempts: int = 0, submitted_at: str | None = None,
                     ) -> tuple[PipelineState | None, str | None]:
    """Target state for this email category, or the reason it does not apply.

    Never raises: every rule ``repo.transition_application`` would reject with
    ``InvalidTransition`` is reported here as a reason instead.
    """
    target = CATEGORY_TARGET.get(category)
    if target is None:
        return None, f"category {category.value} does not change application status"
    if current == target:
        return None, f"application already {target.value}"
    by_user = actor == USER_ACTOR
    if not by_user:
        if current == S.NEEDS_MANUAL_ACTION:
            what = "an unknown submission outcome" if submission_attempts > 0 else "an application never submitted"
            return None, f"email never resolves {what} automatically; {CONFIRM_YOURSELF}"
        if submission_attempts <= 0 and not submitted_at:
            return None, f"no record that this application was submitted; {CONFIRM_YOURSELF}"
    allowed_from = CONFIRMATION_FROM if by_user else AUTO_CONFIRMATION_FROM
    if target == S.CONFIRMATION_RECEIVED and current not in allowed_from:
        return None, f"confirmation email does not apply to an application in {current.value}"
    if not can_transition(current, target):
        return None, f"{current.value} -> {target.value} is not an allowed transition"
    if not by_user and (current, target) in USER_ONLY_WITHOUT_EVIDENCE:
        return None, f"{current.value} -> {target.value} may only be recorded by you"
    if not by_user and (current, target) in USER_CORRECTIONS:
        return None, (f"{current.value} -> {target.value} is a correction only you can make "
                      "(job-agent applications set-status)")
    return target, None


def _corrected_by_user(db: Database, application_id: int) -> bool:
    """Did you ever override an outcome by hand (e.g. rejected -> interview_requested)?

    After that, an email (possibly the same misread thread) must not silently undo your correction.
    """
    rows = db.query_all("SELECT from_status, to_status FROM application_events WHERE application_id = ? "
                        "AND event_type = 'status_change' AND actor = ? AND from_status IS NOT NULL "
                        "AND to_status IS NOT NULL", (application_id, USER_ACTOR))
    return any((PipelineState(r["from_status"]), PipelineState(r["to_status"])) in USER_CORRECTIONS for r in rows)


def apply_email_status(db: Database, *, application_id: int, association_id: int, category: EmailCategory,
                       evidence: dict[str, Any], actor: str, email_id: int) -> StatusOutcome:
    """Apply (or record why not) the status implied by an email. Caller decides eligibility."""
    row = repo.get_application(db, application_id)
    if row is None:
        return StatusOutcome(skipped_reason="application no longer exists")
    current = PipelineState(row["status"])
    target, reason = check_transition(current, category, actor=actor,
                                      submission_attempts=int(row["submission_attempts"] or 0),
                                      submitted_at=row["submitted_at"])
    if target is not None and actor != USER_ACTOR and _corrected_by_user(db, application_id):
        target, reason = None, ("you corrected this application's status yourself, so emails only suggest "
                                f"changes; {CONFIRM_YOURSELF}")
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
