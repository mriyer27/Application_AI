"""Concise, phone-friendly Markdown summary (``Daily_Summary_YYYY-MM-DD.md``).

All database text is untrusted: it is escaped with :func:`text.md_escape` (HTML
entity-escaped, Markdown punctuation backslash-escaped, newlines collapsed) and
error/email text is redacted first. Only plain http(s) URLs are emitted, as
``[Open posting](url)`` links. Lists use bullets (no tables) so the file reads well on a phone.
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from job_agent.reporting import queries as q
from job_agent.reporting import text as t
from job_agent.reporting.data import ReportData
from job_agent.reporting.workbook import reasons, submission_evidence

#: Items shown per list before "...and N more".
LIST_CAP = 10
#: Top matches shown in the Markdown (the workbook lists ``reporting.top_matches``).
MARKDOWN_TOP_MATCHES = 10


def _e(value: Any, max_len: int = 160) -> str:
    return t.md_escape(value, max_len=max_len)


def _job(row: dict[str, Any]) -> str:
    employer = _e(row.get("employer") or "Unknown employer", 80)
    title = _e(row.get("title") or "Unknown role", 120)
    return f"{employer} — {title}"


def _sub(items: Sequence[Any], render: Callable[[Any], str], *, total: int | None = None,
         indent: str = "  ") -> list[str]:
    out = [f"{indent}- {render(item)}" for item in items[:LIST_CAP]]
    remaining = (total if total is not None else len(items)) - min(len(items), LIST_CAP)
    if remaining > 0:
        out.append(f"{indent}- …and {remaining} more (see the Excel report)")
    return out


def _needs_action(data: ReportData) -> list[str]:
    tz = data.tz_name
    lines: list[str] = []
    if data.approvals:
        lines.append(f"- **{len(data.approvals)} approval(s) pending** — nothing is submitted until you approve")
        lines += _sub(data.approvals, lambda a: (
            f"{_e(a.get('summary'), 140)} ({_e(t.humanize(a.get('request_type')), 40)}"
            + (f", expires {t.local_text(a.get('expires_at'), tz)}" if a.get("expires_at") else "") + ")"))
    if data.review_jobs:
        lines.append(f"- **{data.review_jobs_total} job(s) need an eligibility review**")
        lines += _sub(data.review_jobs, lambda j: (
            f"{_job(j)}: " + _e(t.flags_text(j.get("eligibility_flags"), sep="; "), 240)),
            total=data.review_jobs_total)
    if data.gmail_auth_error:
        lines.append(f"- **Gmail authorization problem:** {_e(t.redacted(data.gmail_auth_error), 200)} — "
                     "re-authorize with `job-agent gmail auth`")
    if data.action_errors:
        lines.append(f"- **{data.action_errors_total} error(s) need your attention**")
        lines += _sub(data.action_errors, lambda e: (
            f"{_e(e.get('component'), 60)}: {_e(t.redacted(e.get('message')), 160)}"
            + (f" → {_e(t.redacted(e.get('human_action')), 160)}" if e.get("human_action") else "")),
            total=data.action_errors_total)
    if data.followups_due:
        lines.append(f"- **{len(data.followups_due)} follow-up(s) due** (drafts are never sent automatically)")
        lines += _sub(data.followups_due, lambda f: (
            (f"{_job(f)}: " if (f.get("employer") or f.get("title")) else "")
            + f"{_e(f.get('suggested_action'), 140)} (due {t.local_text(f.get('due_at'), tz)})"))
    return lines or ["- Nothing needs your action right now."]


def _top_matches(data: ReportData) -> list[str]:
    matches = data.top_matches[:MARKDOWN_TOP_MATCHES]
    if not matches:
        return ["- No scored matches yet."]
    lines: list[str] = []
    for i, j in enumerate(matches, start=1):
        bits = [t.category_label(j.get("category"))]
        if j.get("location"):
            bits.append(_e(j.get("location"), 60))
        status = j.get("application_status")
        if status:
            bits.append(f"application: {t.status_label(status)}")
        lines.append(f"{i}. **{float(j['overall_score']):.1f}** · {_job(j)} ({' · '.join(bits)})")
        for reason in reasons(j, 3):
            lines.append(f"   - {_e(reason, 160)}")
        link = t.md_url(j.get("job_url"))
        if link:
            lines.append(f"   - {link}")
    if len(data.top_matches) > len(matches):
        lines.append(f"- …{len(data.top_matches) - len(matches)} more in the Excel report (Daily Summary sheet)")
    return lines


def _applications(data: ReportData) -> list[str]:
    c = data.counts
    tz = data.tz_name
    prepared_open = [a for a in data.prepared if not a.get("evidenced_submitted")]
    lines = [f"- Prepared today: {len(data.prepared)} (not yet submitted: {len(prepared_open)})"]
    lines += _sub(data.prepared, lambda a: f"{_job(a)} — {t.status_label(a.get('status'))}")
    lines.append(f"- **Actually submitted today (evidence-based): {len(data.submissions)}**")
    lines += _sub(data.submissions, lambda a: (
        f"{_job(a)} — {t.local_text(a.get('submitted_at') or a.get('status_updated_at'), tz)}, "
        f"{_e(submission_evidence(a), 60)}"))
    lines.append(f"- Confirmations today: {len(data.confirmations)}")
    lines += _sub(data.confirmations, lambda a: f"{_job(a)} — {t.evidence_label(a.get('confirmation_status'))}")
    lines.append(f"- Awaiting your approval now: {c.get('awaiting_approval', 0)}; submitted in total: "
                 f"{c.get('submitted', 0)}")
    return lines


def _email_line(e: dict[str, Any]) -> str:
    text = _e(t.redacted(e.get("subject") or "(no subject)", max_len=140), 140)
    text += f" — {_e(e.get('from_domain') or 'unknown sender', 60)}"
    if e.get("employer") or e.get("title"):
        text += f" (re: {_job(e)})"
    if e.get("injection_suspected"):
        text += " (WARNING: possible prompt-injection content)"
    return text


def _milestone_line(m: dict[str, Any]) -> str:
    return f"{_job(m)} — {t.status_label(m.get('milestone'))}"


def _messages(data: ReportData) -> list[str]:
    lines: list[str] = []
    groups = (
        ("Recruiter messages", data.emails_in(q.RECRUITER_EMAIL_CATEGORIES), []),
        ("Interviews", data.emails_in(q.INTERVIEW_EMAIL_CATEGORIES), data.interview_milestones),
        ("Assessments", data.emails_in(q.ASSESSMENT_EMAIL_CATEGORIES), data.assessment_milestones),
        ("Rejections", data.emails_in(("rejection",)), data.rejection_milestones),
        ("Offers", data.emails_in(("offer",)), data.offer_milestones),
    )
    for label, emails, stones in groups:
        if not emails and not stones:
            continue
        lines.append(f"- {label}: {len(stones) + len(emails)}")
        lines += _sub(stones, _milestone_line)
        lines += _sub(emails, _email_line)
    return lines or ["- No recruiter messages, interviews or assessments today."]


def _failures(data: ReportData) -> list[str]:
    if not data.failures:
        return ["- No failures recorded today."]
    tz = data.tz_name
    return _sub(data.failures, lambda f: (
        f"{t.local_text(f.get('at'), tz)} {_e(f.get('component'), 60)}: {_e(t.redacted(f.get('message')), 180)}"
        + (" (resolved)" if f.get("resolved") else "")), indent="")


def _next_steps(data: ReportData) -> list[str]:
    c = data.counts
    steps: list[str] = []
    if data.approvals:
        steps.append("Review pending approvals (`job-agent approvals` or the dashboard); approve only what you "
                     "have checked.")
    if data.gmail_auth_error:
        steps.append("Re-authorize Gmail (`job-agent gmail auth`) so confirmations and replies are tracked.")
    if data.action_errors:
        steps.append("Resolve the errors listed above (`job-agent status` shows details).")
    if data.followups_due:
        steps.append("Handle due follow-ups; send any draft yourself only if you agree with it.")
    if data.review_jobs:
        steps.append("Check eligibility flags (sponsorship, citizenship, clearance) before applying to flagged jobs.")
    unapplied = [j for j in data.top_matches[:MARKDOWN_TOP_MATCHES] if not j.get("application_status")]
    if unapplied:
        steps.append(f"Consider preparing applications for {len(unapplied)} top match(es) without one yet.")
    if c.get("awaiting_approval"):
        steps.append(f"{c['awaiting_approval']} application(s) are prepared but NOT submitted until you approve.")
    if data.deadlines:
        steps.append(f"{len(data.deadlines)} deadline(s) overdue or within 7 days — see the Excel report.")
    return [f"- {s}" for s in steps] or ["- Nothing urgent. The agent keeps monitoring on its schedule."]


def render_markdown(data: ReportData) -> str:
    c = data.counts
    d = data.report_date
    lines = [
        f"# Job search summary — {d.isoformat()} ({d.strftime('%a')})",
        "",
        f"*Generated {data.generated_at.strftime('%Y-%m-%d %H:%M')} ({data.tz_name}) · job-agent {data.app_version}*",
        "",
        f"**Today:** {c.get('new_jobs_today', 0)} new jobs · {c.get('applications_prepared_today', 0)} prepared · "
        f"{c.get('submitted_today', 0)} actually submitted · {c.get('emails_today', 0)} emails",
        "",
        f"**Totals:** {c.get('total_jobs', 0)} jobs · {c.get('jobs_scored', 0)} scored · "
        f"{c.get('shortlisted', 0)} shortlisted · {c.get('submitted', 0)} submitted · "
        f"{c.get('confirmations', 0)} confirmed · {c.get('interviews', 0)} interviews · "
        f"{c.get('assessments', 0)} assessments · {c.get('rejections', 0)} rejections · {c.get('offers', 0)} offers",
        "",
        "## Needs your action",
        *_needs_action(data),
        "",
        "## Top matches",
        *_top_matches(data),
        "",
        "## Documents generated",
        *(_sub(data.documents, lambda doc: (
            f"{_e(doc.get('file_name'), 120)} ({_e(t.humanize(doc.get('doc_type')), 20)}, "
            f"{_e(t.humanize(doc.get('status')), 20)})"), indent="") or ["- None today."]),
        "",
        "## Applications",
        *_applications(data),
        "",
        "## Messages, interviews and assessments",
        *_messages(data),
        "",
        "## Failures",
        *_failures(data),
        "",
        "## Next steps",
        *_next_steps(data),
        "",
        "*\"Submitted\" counts only applications with a confirmation page, a confirmation email or your own "
        "report. Prepared forms and pending approvals are never counted as submitted.*",
        "",
    ]
    return "\n".join(lines)
