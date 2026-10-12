# Follow-ups

job-agent reviews your applications and synced emails once a day (the
`followups` task, default 08:00) and lists things you might want to do: follow
up on an application, reply to a recruiter, prepare for an interview, finish an
assessment, submit before a deadline, or close a stale application.

**Nothing is ever sent.** Follow-ups are suggestions with an optional draft.
You copy the draft into your own mail client, send it yourself, then mark the
follow-up done.

```bash
job-agent followups              # open follow-ups (pending / drafted / approved)
job-agent followups --all        # every follow-up, any status
job-agent followups review       # run the rules now (safe to repeat)
job-agent followups show 7       # details + draft
job-agent followups approve 7    # mark "approved for manual sending" and print the draft to copy
job-agent followups done 7       # you did it (sent it / handled it)
job-agent followups dismiss 7    # not needed
```

The dashboard's Follow-ups page uses the same functions
(`job_agent.followups.service.set_followup_status`).

## Rules

Thresholds come from `config/config.yaml`:

```yaml
followups:
  application_no_response_days: 8
  recruiter_reply_days: 2
  stale_application_days: 30
  interview_reminder_hours: 24
  max_followups_per_application: 2
```

| Type | When it is created | Draft |
|---|---|---|
| `application_no_response` | Application is `submitted` or `confirmation_received`, and no linked employer email (other than an automatic confirmation) arrived since submission, for `application_no_response_days`. A later one is due the same number of days after you completed/dismissed the previous one. | Short, polite follow-up that names only the employer, job title and the date you applied. |
| `recruiter_reply` | A `recruiter_message` email still marked *needs attention* after `recruiter_reply_days`. | The email's suggested reply (placeholders such as `[your availability]`). |
| `interview_upcoming` | Application enters `interview_requested` or `interview_scheduled`. | None (a reminder: reply / prepare yourself). |
| `assessment_due` | Application enters `assessment_received`. | None (check the deadline in the email). |
| `deadline` | A shortlisted job without an application, or an application not yet submitted, whose `closing_at` is within the next 3 days. | None. |
| `stale_application` | No activity (status change, event or linked email) on an open application for `stale_application_days`. | None. The suggestion is to mark it closed; you decide. |

Guarantees:

- **Idempotent.** Each rule match has a deterministic `dedupe_key`, e.g.
  `application_no_response:<application_id>:<n>`,
  `recruiter_reply:email:<email_id>`,
  `interview_upcoming:<application_id>:<status>:<status_updated_at>`,
  `deadline:job:<job_id>:<closing_at>`. Running the review again never creates
  duplicates.
- **At most one open follow-up of a type per application** (open = pending,
  drafted, awaiting approval, approved).
- **`max_followups_per_application`** caps outreach follow-ups
  (`application_no_response` + `recruiter_reply`) per application; `0`
  disables them. Reminders (interview, assessment, deadline, stale) don't count
  against the cap.
- `applications.follow_up_due_at` is set to the earliest open follow-up's due
  time.

## Statuses

```
pending ─┬─> approved ─┬─> completed
drafted ─┤             └─> dismissed
         ├─> completed
         └─> dismissed
```

- `pending`: no draft; `drafted`: a draft is attached.
- `approved` means **approved by you for manual sending**. It doesn't send
  anything and doesn't create an approval request; it marks the draft as
  ready for you to send.
- `completed`: you did it. For an application this adds an application event
  (`followup_completed`, which also counts as activity). For a recruiter reply
  it clears the email's *needs attention* flag.
- `dismissed`: not needed. A dismissed recruiter reply also clears the email's
  flag.
- Only a human actor (`user`, `cli`, `dashboard`) can change a status. Every
  change is written to the audit log.

## Drafts

Templates use only facts from the database (employer, job title, the date you
applied), your name from the verified profile, and placeholders. They never
claim skills, numbers or commitments.

If `llm.enabled` is true and the local LLM module is available, the
no-response draft may come from the local model (`draft_followup`). It is
accepted only if it passes deterministic checks (no acceptance or commitment
phrases, no salary/negotiation, no sponsorship/visa/citizenship statements, no
times, phone numbers, email addresses or dollar amounts, max 1500 chars);
otherwise the template is used. With `permissions.draft_emails: false`, no
drafts are produced at all.

## Limitations

- Interview and assessment times/deadlines are not extracted from emails. The
  reminders say "check the email / your calendar", and
  `interview_reminder_hours` is **not used** yet (there is no interview time
  to count back from).
- Your own replies are not detected (sent mail is excluded from Gmail sync), so
  mark recruiter follow-ups done, or the email handled
  (`job-agent emails handled <id>`), after you reply.
- "No response" only knows about emails that were synced **and** linked to the
  application. An employer reply that wasn't linked (e.g. from an unknown
  address) still counts as no response; link it with
  `job-agent emails associate`.
- Sending is not implemented and `permissions.send_emails` can't be enabled.
