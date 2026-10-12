# Dashboard

A small, authenticated web dashboard for checking on the agent and approving
things from your phone. It runs on your laptop, binds to `127.0.0.1` by
default, and works without JavaScript (the only script adds "Copy" buttons).

```bash
job-agent dashboard set-password   # once: username + password (12+ characters)
job-agent dashboard                # serves http://127.0.0.1:8765 (dashboard.host / dashboard.port)
```

`set-password` asks for a username and the password twice (hidden), stores an
scrypt hash as `JOB_AGENT_DASHBOARD_USERNAME` / `JOB_AGENT_DASHBOARD_PASSWORD_HASH`
in `<home>/.env` (mode 0600) and creates `JOB_AGENT_SESSION_SECRET` if it does
not exist yet. The password itself is never stored or printed. Restart the
dashboard after changing it.

`job-agent dashboard` refuses to start when the username, password hash or
session secret is missing, refuses wildcard binds (`0.0.0.0`, `::`), and prints
a loud warning when `dashboard.host` is not a loopback address. For phone
access read [remote-access.md](remote-access.md) first.

## Pages

| Page | What you see | What you can do |
|---|---|---|
| **Overview** | Automation paused/active, scheduler daemon running or not (heartbeat younger than 3 minutes), last successful run, next scheduled run, new jobs today, top matches, pending approvals, follow-ups due within 24 h, emails flagged as needing attention (last 14 days), unresolved errors, Gmail status, problems from the last health check | — |
| **Jobs** | Search (employer/title), filters (match category, status, source), sort (score, date, employer, status), 25 per page | Open a job |
| **Job detail** | Score and category, recommended next action, AI fit explanation (only when the local model produced one; otherwise it says "rules-based score only"), factor table, eligibility flags with the verbatim listing excerpt, sponsorship / work-authorization text from the listing, matching and missing qualifications, reasons to apply / not to apply, evidence from your verified profile, official posting and application links, documents, score and listing history, the full (escaped) description | Mark relevant / not relevant / clear; save notes |
| **Applications** | List with status, next action, follow-up date | Open an application |
| **Application detail** | Status, confirmation status and evidence, documents (download), prepared form answers (legal declarations and items needing review highlighted), approvals, full status timeline from `application_events`, related follow-ups and emails | Download documents |
| **Approvals** | Pending requests (expired ones are marked) and recently decided ones | Open the review page |
| **Approval review** | Legal declarations first, in a red box; validation errors, unanswered required fields and items needing review in warning boxes; then everything else in the request payload (employer, role, application URL, résumé, cover letter, answers, agreements, any other keys) | Approve or reject (requires ticking the confirmation box) |
| **Email** | Messages with category, classifier confidence, summary, "needs attention" and "suspicious instructions" flags, links to jobs/applications, suggested reply as plain text with a Copy button | Link an email to a job/application, optionally applying the status the email implies (requires confirmation) |
| **Follow-ups** | Open (or all) follow-ups with due date, suggested action and draft text with a Copy button | Mark approved / done / dismissed (requires confirmation) |
| **Reports** | Last-30-day bar charts (jobs discovered, applications submitted, job emails received; counted in SQL, drawn as inline SVG) and the report history | View a report, download its Excel / Markdown file |
| **Report detail** | Statistics and the Markdown summary rendered safely (everything escaped; only headings, lists, tables, code, bold/italic and http(s) links are produced) | Download |
| **Controls** | Automation state, the dashboard tasks with their last result, unresolved errors, the last 25 runs | Run a task; retry a failed run; pause/resume (requires confirmation); mark an error resolved (requires confirmation) |

`GET /api/status` returns a small JSON status object (paused, daemon running,
last success, next run, counts) for authenticated sessions; it answers 401
otherwise.

### Honest states

* Features whose module is not installed say **"not available yet"** instead
  of pretending to work (email association, follow-up actions, any task whose
  module is missing).
* Gmail shows **"Gmail not connected"** unless `agent_state.gmail_last_success_at`
  is newer than the last recorded `gmail_auth_error` (and "Gmail integration
  disabled" when `gmail.enabled` is false).
* The next scheduled run is annotated "daemon not running: will not fire" when
  there is no recent daemon heartbeat.
* An application in `submitted`/`submission_attempted` without evidence says
  so; the dashboard never infers success.

## Controls

* **Run a task**: buttons for `discover_and_score`, `report`, `health`,
  `followups` and `email_sync` when they are registered in `tasks.TASKS` with
  `dashboard_allowed=True`. Only names from that registry are accepted (anything
  else is a 400 and nothing runs). Tasks run in one background worker thread with
  their own database connection, trigger `dashboard`, and ignore the pause flag
  (like a manual CLI run). The database task lock prevents overlap with the
  scheduler or the CLI; a task that is already running is refused with 409.
* **Retry** appears only for a `failed` run whose task is `retry_safe` and
  `dashboard_allowed`; it re-runs the task with `attempt + 1`.
* **Pause / resume** use `repo.set_paused(..., actor="dashboard")`.
* **Resolve error** uses `repo.resolve_error(..., actor="dashboard")`.

There is deliberately no endpoint that runs shell commands or arbitrary code
(a test greps the package for `subprocess`, `os.system`, `eval(` and `exec(`).

## How actions are carried out

| Action | Implementation |
|---|---|
| Approve / reject | `applications.service.decide(ctx, uid, approve=, actor="dashboard", note=, expected_payload_hash=)` when that module provides it (it also moves the application state); otherwise `repo.decide_approval(...)`. The hidden `expected_payload_hash` is always passed, so a request that changed after you opened it is refused ("review it again"). Approving never submits anything by itself. |
| Relevance / notes | Updates `jobs.user_relevance` / `jobs.user_notes` in a transaction and writes an audit event (`job_relevance_set`, `job_notes_updated`; note text is not copied into the audit log). `jobs.updated_at` is left unchanged. |
| Email association | `email.service.associate_email(ctx, email_id, job_ref=, application_ref=, apply_status=)`; references are passed as `int` when numeric, otherwise as the uid string. |
| Follow-up status | `followups.service.set_followup_status(ctx, id, FollowupStatus, actor="dashboard")` for approved / completed / dismissed. |
| Tasks | `tasks.run_task(ctx, name, trigger="dashboard", ignore_pause=True)` |

Logins (success and failure, with the client address) are audited as
`dashboard_login`.

## Security model

* **Authentication**: one user. The password is verified against the scrypt
  hash with `security.passwords.verify_password`; the username is compared in
  constant time, and the password hash is checked even when the username is
  wrong.
* **Sessions**: an `itsdangerous`-signed cookie (`ja_session`) carrying a
  random session id and CSRF token; `HttpOnly`, `SameSite=Strict`, `Secure`
  when `dashboard.secure_cookies: true`, max age
  `dashboard.session_max_age_minutes` (default 12 h). The session id must also
  exist in the server's in-memory registry, so **Sign out** revokes it
  immediately and **restarting the dashboard signs everyone out**. A new id is
  issued at every login.
* **CSRF**: every POST (including sign-in and sign-out) needs a hidden
  `csrf_token` field matching the session (sign-in uses a signed double-submit
  token); otherwise 403.
* **Confirmation**: approve/reject, pause/resume, resolve error, follow-up
  status changes and email-driven status updates require a ticked
  `confirm` checkbox that is validated on the server (400 without it).
* **Login rate limiting**: in-memory sliding 60-second window,
  `dashboard.login_attempts_per_minute` attempts per client address and four
  times that globally; over the limit the response is 429 with `Retry-After: 60`
  and the password is not even checked. `X-Forwarded-For` is ignored (it can
  be forged), so behind `tailscale serve` every request has the same client
  address and the limit effectively applies to all of your devices together.
* **Host header allowlist** (DNS-rebinding protection): `127.0.0.1`,
  `localhost`, `::1`, `dashboard.host` and `dashboard.allowed_hosts`, with or
  without a port; anything else gets 400.
* **Headers on every response**: `Content-Security-Policy: default-src 'self';
  img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors
  'none'; base-uri 'none'; form-action 'self'`, `X-Content-Type-Options:
  nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, plus
  `Cache-Control: no-store` on everything except `/static/`. There are no inline
  scripts or styles.
* **Escaping**: Jinja2 autoescaping everywhere; job titles, descriptions,
  emails and report text are always rendered as text. Links are produced only for
  `http(s)` URLs, with `rel="noopener noreferrer" target="_blank"`; anything
  else (e.g. `javascript:`) is shown as inert text.
* **Files**: `/static` serves only the package's `dashboard/static` directory and
  only asset types (`.css`, `.js`, images, fonts), never dotfiles. Reports and
  documents are downloaded only through authenticated routes that look up the
  file by database id (`reports.id`, `document_versions.id`) and resolve it with
  `ensure_within` inside `reports_dir` / `documents_dir`; anything else is 404.
* **No API docs**: FastAPI's `/docs`, `/redoc` and `/openapi.json` are disabled.

## Limitations

* Single user, single process. Sessions and rate-limit counters live in memory:
  a restart signs you out and resets the counters; do not run several uvicorn
  workers.
* No "withdraw application" button: there is no application-withdrawal
  contract for the dashboard yet. Use the CLI / applications module.
* No live updates; reload the page (no JavaScript polling).
* Trends group by local date using today's UTC offset, so around a daylight-
  saving change a few events can land on the neighbouring day.
* Stopping the dashboard while a dashboard-started task runs waits for that
  task to finish.
* The dashboard is only reachable while the laptop is awake and
  `job-agent dashboard` is running (see [remote-access.md](remote-access.md)).
