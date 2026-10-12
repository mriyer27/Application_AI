# Application_AI — a local-first, human-supervised job application agent

A personal AI job-search assistant that runs **on your own laptop**. It finds
relevant jobs on public employer job boards, ranks them against your verified
profile with an explainable score, drafts truthful tailored résumés and cover
letters, prepares application forms for your review, reads (never sends)
Gmail, tracks every application, writes a daily Excel report, and gives you a
private dashboard you can open from your phone.

> **It never submits an application or sends an email without your explicit
> approval, never invents experience, and never guesses legal or
> work-authorization answers.**

**New here? Follow [docs/getting-started.md](docs/getting-started.md)** — the
complete step-by-step guide for your Windows laptop and your phone.

## What it does

| Area | What actually happens |
|---|---|
| **Discovery** | Fetches listings from the public, documented job-board APIs of **Greenhouse, Lever and Ashby** for the employers you list (29 preconfigured). Polite rate limiting, retries, no scraping behind logins, no CAPTCHA bypass. Filters by title, location (US-aware), arrangement, type, years, age; deduplicates across sources; tracks reposts, changes and closures. |
| **Scoring** | 0–100 score from 7 configurable weighted factors (career interest, skills, experience level, AI/business/analytics/product relevance, location, compensation, employer). Every qualification marked "matched" cites the verified fact behind it. Sponsorship / citizenship / clearance / export-control / graduation-timing statements are flagged **separately** with the verbatim excerpt — never as a legal determination. |
| **Local AI** | Optional [Ollama](https://ollama.com) on `127.0.0.1` (no cloud API). Analyzes the top jobs; its output is validated so it can't add requirements that aren't in the listing. Without Ollama everything still works with rules. |
| **Documents** | ATS-friendly résumé and cover letter (DOCX, PDF, Markdown preview) per job, built **only** from your verified facts. Optional AI rewording is accepted only if a deterministic checker finds no new numbers, tools, leadership claims or names. Every bullet is traceable to a fact id. |
| **Applications** | Opens the employer's official application page (Playwright/Chromium), fills only verified fields, uploads your documents, **never clicks Submit**, and creates an approval request listing every answer, legal question, unanswered field and agreement. Legal/eligibility, EEO and agreement fields are always left for you. You submit yourself (default) or, if you enable it, the browser submits that one approved application once and records it only on a confirmation page. |
| **Gmail** | Official Gmail API, OAuth 2.0, **read-only scope**. Categorizes confirmations, recruiter messages, interviews, assessments, rejections and offers; links them to applications; updates status only on credible evidence; drafts replies you send yourself. Message bodies are not stored. |
| **Follow-ups** | Suggests follow-ups for silent applications, recruiter messages, interviews, assessments, deadlines and stale applications, with drafts. Never sends anything. |
| **Reports** | Daily `Job_Search_Report_YYYY-MM-DD.xlsx` (Dashboard with charts, All Jobs, Applications, Daily Summary, Follow-ups, Activity Log, Configuration) and a phone-friendly `Daily_Summary_YYYY-MM-DD.md`. |
| **Dashboard** | Responsive web app on `127.0.0.1:8765`: overview, jobs with explanations, applications and timelines, approvals, email, follow-ups, reports, safe controls. Login, CSRF protection, rate limiting, strict security headers. Phone access through a private Tailscale network. |
| **Automation** | Scheduler daemon (discovery twice daily, follow-ups, report, health checks, optional email sync) with overlap prevention, bounded retries, catch-up after sleep, pause/resume, and auto-start scripts for Windows, macOS and Linux. |
| **Safety & records** | SQLite is the source of truth (versioned migrations, backups). Every consequential action is in an audit log. Secrets live only in `.env`; logs and reports are redacted. Approvals are bound to the exact content you reviewed and can be used once. |

## Quick start

```bash
python -m venv .venv
.venv/bin/pip install -e ".[all]"          # Windows: .\.venv\Scripts\pip install -e ".[all]"
.venv/bin/job-agent init                   # creates your local config, profile, .env, database
.venv/bin/job-agent doctor                 # health checks with next steps
.venv/bin/job-agent run-once               # discover + score (+ local AI) + draft documents
.venv/bin/job-agent jobs --top 20
```

Full instructions, including Ollama, Gmail, the dashboard, phone access and
auto-start: **[docs/getting-started.md](docs/getting-started.md)**.

## Commands

| Command | Purpose |
|---|---|
| `job-agent init` / `doctor` / `status` / `config-check` | set up, health checks, overview, validate config & profile |
| `job-agent run-once` | one full cycle now (same as the scheduled `discover_and_score`) |
| `job-agent discover` / `sources` / `sources check` | fetch jobs; source status; reachability |
| `job-agent score` / `jobs --top N` / `job show|mark|note <job-uid>` | score, rank, explain, annotate |
| `job-agent prepare <job-uid>` / `documents list|approve|reject` | tailored documents and review |
| `job-agent apply <job-uid>` | prepare the application form (never submits) |
| `job-agent approvals [show|approve|reject|answer]` | review what's waiting for you |
| `job-agent applications [list|show|mark-submitted|submit|withdraw|note]` | track applications |
| `job-agent gmail auth|status|sync|revoke` / `emails` / `followups` | read-only email and follow-ups |
| `job-agent report [--date]` / `reports` | Excel + Markdown reports |
| `job-agent llm check|recommend|benchmark|analyze` | local AI |
| `job-agent dashboard` / `dashboard set-password` | phone/desktop dashboard |
| `job-agent daemon [--once]` / `schedule` / `pause` / `resume` | automation |
| `job-agent db backup|info` / `tasks list|run` | maintenance |

Every command has `--help`.

## Configuration

- `config/config.yaml` — titles, locations, salary target, employers' boards,
  weights, thresholds, schedules, permissions. Validated strictly; no code edits needed.
- `profiles/candidate_profile.yaml` — your verified facts (education,
  experience bullets with ids, skills, preferences, work authorization as
  `null` = unknown, verified answers).
- `.env` — secrets only (dashboard login hash, session secret). Never committed.

Examples with placeholders: `config/config.example.yaml`,
`profiles/candidate_profile.example.yaml`, `.env.example`.

## Documentation

| Topic | File |
|---|---|
| Step-by-step laptop + phone guide | [docs/getting-started.md](docs/getting-started.md) |
| Installation details, backups | [docs/setup.md](docs/setup.md) |
| Architecture and key decisions | [docs/architecture.md](docs/architecture.md) |
| Security & privacy, revoking credentials | [docs/security.md](docs/security.md) |
| Job sources | [docs/sources.md](docs/sources.md) |
| Scoring | [docs/scoring.md](docs/scoring.md) |
| Local AI (Ollama, models for your hardware) | [docs/local-llm.md](docs/local-llm.md) |
| Résumés and cover letters | [docs/documents.md](docs/documents.md) |
| Applications and approvals | [docs/applications.md](docs/applications.md) |
| Gmail setup | [docs/gmail-setup.md](docs/gmail-setup.md) |
| Follow-ups | [docs/followups.md](docs/followups.md) |
| Reports | [docs/reports.md](docs/reports.md) |
| Dashboard | [docs/dashboard.md](docs/dashboard.md) |
| Phone / remote access | [docs/remote-access.md](docs/remote-access.md) |
| Scheduling and auto-start | [docs/scheduling.md](docs/scheduling.md) |
| Troubleshooting | [docs/troubleshooting.md](docs/troubleshooting.md) |

## Honest limitations

- **Job sources:** only employers that use Greenhouse, Lever or Ashby public
  boards. LinkedIn, Indeed, Handshake, Workday and iCIMS are not supported (no
  permitted public API / login walls). Lever/Ashby employer names come from the
  board id.
- **Heuristics:** filtering, skill matching, eligibility flags, field
  detection and email classification are rule-based (English). They can miss
  unusual wording. Eligibility flags are not legal advice.
- **Local AI on a CPU is slow:** measured in the dev container (4 vCPUs) the
  recommended `qwen2.5:7b-instruct` took ~4.5 minutes per job analysis; a
  Ryzen 7 laptop should be faster but expect minutes, so only the top ~10
  jobs per run are analyzed. Smaller models are faster but less accurate.
  Measure with `job-agent llm benchmark`. AI prose (summaries) is not
  fact-checked; requirements and sponsorship excerpts are.
- **Application forms:** tested only against local fixture pages that mimic
  Greenhouse/Lever forms. Real pages vary; iframes, multi-step wizards,
  custom widgets and pages with CAPTCHA scripts need manual completion.
  Browser submission is **off by default**.
- **Gmail:** in Google's "Testing" mode the authorization expires every 7 days
  (`job-agent gmail auth` again). Interview times and deadlines are not
  extracted from emails.
- **Not 24/7:** nothing runs while the laptop is asleep, off or offline;
  missed daily tasks run once when the daemon is running again.
- **Single user, single machine.** The dashboard keeps sessions in memory.

## Development

```bash
.venv/bin/python -m pytest -q                 # 960+ tests, no network
.venv/bin/python -m pytest -m network         # opt-in live job-board tests
.venv/bin/python -m pytest -m ollama          # opt-in live local-model tests
```

Conventions, architecture and the non-negotiable safety rules for
contributors (and AI coding assistants) are in [CLAUDE.md](CLAUDE.md).
Personal data (`profiles/`, `config/config.yaml`, `.env`, `data/`,
`documents/`, `reports/`, `logs/`) is git-ignored.
