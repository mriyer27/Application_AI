"""Draft text for follow-ups. Drafts are stored for the user to copy; nothing is sent.

Templates contain only facts from the database (employer, job title, the date
you applied) and placeholders. An optional local-LLM draft is used only if it
passes the same deterministic safety checks as suggested email replies: no
commitments or sensitive data, no work-authorization statements, and no
numbers (also with a changed qualifier), tools, leadership, degrees,
certifications, employers or job titles that are not in the verified profile
(``ctx["facts"]`` / ``ctx["profile_skills"]`` / ``ctx["profile"]`` when
provided; otherwise any such experience claim is rejected). The employer and
job title may be named, never claimed as your background. The accepted draft is stored normalized
(``security.textnorm.normalize_text``), exactly as it was checked.
"""

from __future__ import annotations

from typing import Any, Callable

from job_agent.email.replies import reply_is_safe
from job_agent.models import FollowupType
from job_agent.security.textnorm import normalize_text
from job_agent.security.untrusted import clean_untrusted

T = FollowupType


def _date_part(iso: str | None) -> str:
    return (iso or "")[:10] or "[date you applied]"


def template_draft(followup_type: FollowupType, ctx: dict[str, Any]) -> str | None:
    name = ctx.get("candidate_name") or "[your name]"
    employer = ctx.get("employer") or "[employer]"
    title = ctx.get("title") or "[role]"
    if followup_type == T.APPLICATION_NO_RESPONSE:
        return (
            f"Subject: Following up on my application for {title}\n\n"
            "Dear Hiring Team,\n\n"
            f"I applied for the {title} position at {employer} on {_date_part(ctx.get('submitted_at'))} and wanted "
            "to follow up on the status of my application. I remain interested in the role and would welcome the "
            "opportunity to discuss it further.\n\n"
            "Thank you for your time and consideration.\n\n"
            f"Best regards,\n{name}"
        )
    if followup_type == T.RECRUITER_REPLY:
        return ctx.get("suggested_reply") or None
    return None


def suggested_action(followup_type: FollowupType, ctx: dict[str, Any]) -> str:
    employer = ctx.get("employer") or "the employer"
    title = ctx.get("title") or "this role"
    if followup_type == T.APPLICATION_NO_RESPONSE:
        return (f"No response from {employer} about {title} for {ctx.get('days')} days. Review the draft, send it "
                "yourself if you want to follow up, then mark this done.")
    if followup_type == T.RECRUITER_REPLY:
        return (f"Reply to the recruiter message \"{(ctx.get('subject') or '')[:120]}\" (received "
                f"{_date_part(ctx.get('received_at'))}). Send your reply yourself, then mark this done.")
    if followup_type == T.INTERVIEW_UPCOMING:
        if ctx.get("status") == "interview_scheduled":
            return f"Interview scheduled with {employer} for {title}: check the time in your calendar and prepare."
        return (f"{employer} asked to interview you for {title}. Reply with your availability yourself; "
                "nothing is accepted automatically.")
    if followup_type == T.ASSESSMENT_DUE:
        return f"Assessment received for {title} at {employer}: check the deadline in the email and complete it."
    if followup_type == T.DEADLINE:
        return (f"{title} at {employer} closes {_date_part(ctx.get('closing_at'))}. Finish and submit before then "
                "(submission still needs your approval).")
    if followup_type == T.STALE_APPLICATION:
        return (f"No activity on {title} at {employer} for {ctx.get('days')} days. Consider marking the "
                "application closed (your decision).")
    return "Review this item."


def llm_followup_draft(draft_fn: Callable[..., str], ctx: dict[str, Any]) -> str | None:
    """Call the LLM drafter and keep the result only if it passes the safety checks."""
    context = f"Applied on {_date_part(ctx.get('submitted_at'))}; no response for {ctx.get('days')} days."
    employer, title = ctx.get("employer") or "", ctx.get("title") or ""
    text = draft_fn(employer=employer, job_title=title, context=context,
                    candidate_name=ctx.get("candidate_name") or "[your name]")
    cleaned = clean_untrusted(str(text or ""), max_chars=3000)
    facts = ctx.get("facts")
    safe = reply_is_safe(cleaned, facts=None if facts is None else list(facts), skills=ctx.get("profile_skills"),
                         context=(context, employer, title, ctx.get("candidate_name") or ""),
                         profile=ctx.get("profile"))
    return normalize_text(cleaned) if safe else None
