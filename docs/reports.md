# Daily reports

job-agent writes one Excel workbook and one short Markdown summary per day.
Both are read-only views of the SQLite database. Generating a report never
changes jobs, applications, approvals or follow-ups. It only records the report
in the `reports` history table and adds a `report_generated` audit event.

## Generating reports

```bash
job-agent report                    # today (in app.timezone)
job-agent report --date 2026-10-10  # a past local date (regenerates that date only)
job-agent reports                   # report history: dates, generation count, files
job-agent tasks run report          # same as `report`, run as a recorded task
```

The scheduler runs the `report` task once a day (default `30 21 * * *` in
`app.timezone`, see `scheduler.tasks.report`). You can't generate a report for
a future date.

## Files and locations

Both files go in `app.reports_dir` (default `reports/` under your project home):

| File | Contents |
|---|---|
| `Job_Search_Report_YYYY-MM-DD.xlsx` | The full workbook (7 sheets, below) |
| `Daily_Summary_YYYY-MM-DD.md` | A short summary you can read on a phone |

`YYYY-MM-DD` is the *local* report date in `app.timezone`. The `reports` table
stores only the file names, relative to `reports_dir`. Downloads resolve them
strictly inside that directory.

### Idempotency and atomic writes

- Each file is written to a temporary file in the same directory and then moved
  into place with `os.replace`, so a reader never sees a half-written file. On
  POSIX the files are created with mode `0600` (readable only by you).
- Regenerating a date replaces that date's two files and nothing else. Other
  dates' files are never opened or modified, and old reports are never deleted
  automatically.
- The `reports` row for the date is upserted: `generation_count` goes up by one,
  `last_generated_at` is updated, and `first_generated_at` keeps its original
  value. `stats` holds the counts from the latest generation as JSON.
- On Windows, if the workbook is open in Excel, the replace fails. The run then
  fails with "Close the report ... and generate it again". Nothing is
  half-written.

## How "today" is computed

The report date is a calendar day in `app.timezone`. Its start and end (local
midnight to local midnight, DST-aware, so some days have 23 or 25 hours) are
converted to UTC, and stored UTC timestamps are compared against that window. A
job discovered at 23:30 New York time counts for that New York date, even
though it is already the next day in UTC.

## Metric definitions

These are implemented in `reporting/queries.py` (`dashboard_counts`).

| Metric | Exactly what is counted |
|---|---|
| Total jobs discovered | `jobs` with `duplicate_of_job_id IS NULL` and `discovered_at` before the end of the report day |
| New jobs today | The same, with `discovered_at` inside the report day |
| Jobs scored | Those jobs that have a current score (`job_scores.is_current = 1`) |
| Shortlisted | Non-duplicate jobs whose `pipeline_state` is currently `shortlisted` |
| Applications (total) | Applications created before the end of the report day |
| Applications prepared today | `prepared_at` inside the day, or a `form_prepared` event or a status change to `awaiting_approval` inside the day. **Preparing is not submitting.** |
| Applications awaiting approval | Applications whose status is currently `awaiting_approval` |
| Approval requests pending | `approval_requests` with status `pending` that have not expired (any type) |
| **Submitted (evidence-based)** | See below |
| Submitted today | Evidence-based submissions whose `submitted_at` (or, if missing, `status_updated_at`) is inside the day |
| Confirmations | Evidence-based submissions with `confirmation_status` `page_evidence`/`email_confirmed`, or with a status-change event to `confirmation_received` |
| Interviews / Assessments / Rejections / Offers | Applications that ever reached `interview_requested`/`interview_scheduled` / `assessment_received` / `rejected` / `offer_received` before the end of the day. This comes from `application_events`; the current status is used for rows without events. |
| Pending follow-ups | Follow-ups whose status is not `completed` or `dismissed` |
| Follow-ups due | Pending follow-ups with `due_at` before the end of the report day (overdue included) |
| Errors requiring attention | `errors` with `resolved = 0` and `requires_human_action = 1` |
| Errors / documents / emails today | Rows whose `occurred_at` / `created_at` / `received_at` (or `processed_at`) is inside the day |

### When an application counts as "submitted"

An application counts only if **both** of these are true:

1. Its status is `submitted` or a later outcome (`confirmation_received`,
   `assessment_received`, `interview_requested`, `interview_scheduled`,
   `rejected`, `offer_received`), or it is `withdrawn`/`closed` with a recorded
   `submitted_at`.
2. There is evidence. Either `confirmation_status` is `page_evidence`
   (confirmation page), `email_confirmed` (confirmation email) or
   `user_reported` (you recorded "I applied"), or a status-change event into
   `submitted`/`confirmation_received` carries an evidence payload or was
   recorded by you (actor `user`).

These never count as submitted: prepared forms, `awaiting_approval`, approved
but unused approvals, `submission_attempted` and `needs_manual_action` (unknown
outcome), or a row whose status says `submitted` but has no evidence. The
Applications sheet shows such a row as "Counted as Submitted: No — no evidence
recorded".

### Snapshot semantics

Counts that depend on timestamps are exact for any report date. Counts of
*open items* (awaiting approval, pending approvals, pending follow-ups, errors
requiring attention, shortlisted) and status columns show the database **at
generation time**. Regenerating an old date does not rebuild what was pending
back then.

## Workbook sheets

All times are local (`app.timezone`) and shown as `yyyy-mm-dd hh:mm`; dates
are shown as `yyyy-mm-dd`. Header rows are frozen, every table has an
auto-filter, column widths are capped and long text wraps.

1. **Dashboard**: the report date, every metric above with a one-line
   definition, and three charts: applications by status (bar), score
   distribution in 10-point buckets (bar), and jobs discovered per day for the
   last 14 days (line). If a sheet hit its row cap, a "Rows not shown" line
   appears here.
2. **All Jobs**: job ID, employer, role, location, work arrangement, salary
   min/max (`$#,##0`; other currencies labelled), salary period, salary note
   (base / total compensation / basis not stated / currency not stated),
   posting date, discovery date, fit score (`0.0`, colour scale), match
   category (coloured), missing qualifications, eligibility concerns
   (`code [severity]: message`), source, job URL, application URL, status
   (coloured) and last checked. Sorted by score, at most 5,000 rows.
3. **Applications**: application ID, employer, job title, application date
   with its type ("Submitted" / "Created (not submitted)"), status, whether it
   counts as submitted, submission evidence, résumé and cover-letter file
   names, confirmation status, next follow-up date, last activity, next action,
   notes and job URL. At most 2,000 rows.
4. **Daily Summary**: one filterable table (Section / Item / Details / Link)
   with these sections: new opportunities, top matches
   (`reporting.top_matches`, with up to 3 reasons each), generated documents,
   applications prepared, actual submissions (evidence-based only),
   confirmations, recruiter messages, interviews, assessments, rejections and
   offers, deadlines (overdue or within 7 days), approvals pending and
   failures. Empty sections show "None".
5. **Follow-ups**: employer, role, type, last activity, follow-up date,
   suggested action, draft (never sent automatically), approval status and
   completion status. Open follow-ups come first.
6. **Activity Log**: agent runs, errors and audit events of the report day,
   newest first, at most 2,000 rows. Columns: local timestamp, type,
   task/actor, job/application, attempted action, outcome, error (redacted),
   human action required, details (redacted).
7. **Configuration**: operating mode, app version, config hash, whether the
   example config is in use, the report time zone, "Secrets: not included",
   and every non-secret setting from `config.safe_dict()` as dotted key/value
   rows.

## Markdown summary

The summary uses headings and bullets, no tables. It contains:

- headline counts for today and the totals
- **Needs your action**: pending approvals, `needs_review` jobs with
  eligibility flags, a Gmail authorization problem (`gmail_auth_error`), errors
  that need you, and follow-ups that are due
- top matches (at most 10) with score, category and 2–3 reasons
- documents generated today
- applications prepared vs. actually submitted (evidence-based), and
  confirmations
- recruiter messages, interviews, assessments, rejections and offers
- failures, and next steps

## Security

- **No secrets.** Report code never reads `ctx.secrets` or environment
  variables. The Configuration sheet comes only from `config.safe_dict()`,
  which can't contain secrets because the config loader rejects secret-looking
  keys. Error messages, audit details and email subjects/summaries pass through
  `redact_text` before they are written.
- **Formula injection.** Any cell text from the data that starts with `=`,
  `+`, `-`, `@`, a tab, CR/LF (or their full-width forms) gets a leading
  apostrophe and is stored as text. A title like `=HYPERLINK(...)` stays text,
  also after a CSV export. Characters that are illegal in XLSX are removed, and
  text is cut at Excel's cell limit.
- **Hyperlinks.** Only absolute `http://`/`https://` URLs without whitespace,
  quotes or angle brackets become clickable. Anything else (`javascript:`,
  `file:`, `ftp:`, relative paths) is plain text.
- **Markdown.** `<` and `>` become entities, so there is no raw HTML.
  Backslash, backticks, `*`, word-boundary `_` and `[`/`]` are
  backslash-escaped, and `](` is broken, so there are no code spans, emphasis,
  links or images. A leading `#`/`+`/`-`/`=`/`1.` is escaped and newlines are
  collapsed. Only safe http(s) URLs are written, as `[Open posting](url)`.

## Reading reports on your phone

Report files stay on your computer. Nothing is uploaded or emailed. To read
them on a phone, either:

- open them from the dashboard over your private VPN (`docs/remote-access.md`), or
- point `app.reports_dir` at a folder that a sync client you already use
  (OneDrive, Google Drive, Dropbox, iCloud Drive) mirrors.

job-agent does not set up or manage sync. Synced reports contain your job
search data, so only use an account you trust. Excel on a phone shows charts
and conditional formatting; plain file previews may show only values.

## Limitations

- Snapshot semantics (above).
- Row caps: All Jobs 5,000; Applications, Follow-ups and Activity Log 2,000
  each. Hitting a cap is flagged. New opportunities in the Daily Summary are
  capped at 50.
- Data is read with separate queries, not one database snapshot, so a task
  finishing during generation can make two numbers differ by one.
- `reporting.keep_days` is not enforced. Remove old files yourself if you
  want; history rows stay and `job-agent reports` marks missing files.
- Salaries appear exactly as listed. A missing currency gets the `$` format but
  is marked "currency not stated".
- An interview email that is not linked to an application appears under
  messages, not in the Interviews count.
- Charts render in Excel, LibreOffice and Excel mobile. Some lightweight
  viewers don't render charts.
