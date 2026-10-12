# Architecture

job-agent is a single-user Python application that runs on your laptop. SQLite
is the source of truth; every other output (Excel, Markdown, dashboard, CLI) is
a view over it.

```
            ┌──────────────── CLI (Typer) ───────────────┐   ┌──── Dashboard (FastAPI, localhost) ───┐
            │ doctor status discover score jobs prepare   │   │ overview jobs applications approvals │
            │ apply approvals report daemon gmail ...     │   │ email follow-ups reports controls    │
            └───────────────┬────────────────────────────┘   └──────────────┬───────────────────────┘
                            │      service functions only (no business logic in UI)
   ┌────────────────────────┼───────────────────────────────────────────────────────────────────┐
   │ tasks.run_task  ── pause gate · DB task lock · agent_runs · classified errors              │
   │   ▲ scheduler (APScheduler): discover_and_score · email_sync · followups · report · health  │
   ├────────────────────────────────────────────────────────────────────────────────────────────┤
   │ discovery → scoring (+ llm analysis) → resume (documents) → applications (+ browser)        │
   │ email (Gmail read-only) → associations → evidence-based status · followups · reporting     │
   ├────────────────────────────────────────────────────────────────────────────────────────────┤
   │ database.repo: state machine · approvals (payload-hash, one-time) · audit · errors · locks  │
   │ security: redaction · permissions · untrusted-input wrapping · safe paths · passwords       │
   │ config (YAML, validated) · profile (verified facts) · secrets (env only)                    │
   └────────────────────────────────────────────────────────────────────────────────────────────┘
            SQLite (data/job_agent.db)      Ollama (127.0.0.1:11434)      files: reports/ documents/ logs/
```

## Key decisions

| Decision | Why |
|---|---|
| SQLite, WAL mode, explicit `BEGIN IMMEDIATE` transactions | Zero-ops, durable, survives restarts; immediate locks serialize concurrent runs instead of deadlocking. |
| One versioned schema (`database/sql/NNN_*.sql`) | Reproducible upgrades; migrations recorded in `schema_migrations`; newer-than-code databases are refused. |
| Deterministic code for every consequential decision | Permissions, approvals, state transitions, deduplication and "is this claim true?" are never delegated to the LLM. |
| LLM output is advisory and validated | The local model analyzes and drafts; Pydantic + deterministic validators decide what is kept. Hallucinated skills/metrics are dropped. |
| Approvals bound to a payload hash, consumed once | The thing you approve is exactly what is submitted; an approval cannot be replayed (prevents duplicate submissions). |
| Evidence required for outcome states | `submitted`, `confirmation_received`, `interview_requested`, ... need page/email evidence or your own report. |
| Public job-board APIs only | Greenhouse, Lever and Ashby publish read-only posting APIs for career sites. No scraping behind logins, no CAPTCHA bypass. |
| Read-only Gmail scope | The agent can't send, delete or modify mail even if a bug or a malicious email tried to make it. |
| Localhost-only services | The dashboard binds to 127.0.0.1 and Ollama must be on localhost; remote access goes through a private VPN (Tailscale). |

## Data flow of one cycle (`discover_and_score`)

1. **Discover**: each configured board is fetched politely (rate limited, bounded
   retries). Listings are normalized into `NormalizedJob`, prefiltered by title,
   filtered by location/arrangement/type/experience/age, deduplicated (stable IDs
   first, then canonical URLs/requisition IDs, then fingerprint + description
   similarity) and upserted. Changed listings are recorded in `job_changes`;
   listings that disappear after a complete fetch are marked closed.
2. **Analyze (optional)**: if Ollama and the configured model are available, the
   top-ranked jobs are analyzed by the local model (cached by input hash).
3. **Score**: an explainable 0–100 score from seven weighted factors plus
   separate eligibility flags (sponsorship/citizenship/clearance/export/graduation
   timing). Jobs above the shortlist threshold without blocker flags are shortlisted.
4. **Draft documents**: optionally, rules-based résumé/cover-letter drafts for the
   top new shortlisted jobs (verbatim verified facts; always reviewed by you).

Applications are prepared on demand (`job-agent apply`), produce an approval
request, and are only submitted by you (default) or by the browser after your
approval when `applications.browser_submission_enabled` is true.

## Failure model

Every failure is classified (`ErrorKind`): recoverable, permanent, authorization
required, human action required, unknown outcome. Tasks never crash the daemon;
failures are written to `errors` / `agent_runs` and shown by `job-agent status`,
the dashboard and the daily report. Unknown outcomes of consequential actions
(e.g. a submission that timed out) are never retried automatically.

## Module map

See `CLAUDE.md` for the source layout and conventions, and the per-area docs:
`sources.md`, `scoring.md`, `local-llm.md`, `documents.md`, `applications.md`,
`gmail-setup.md`, `followups.md`, `reports.md`, `dashboard.md`,
`remote-access.md`, `scheduling.md`, `security.md`, `setup.md`,
`troubleshooting.md`.
