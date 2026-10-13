# CLAUDE.md — job-agent

Local-first, human-supervised AI job application agent (Python 3.11+, SQLite,
Ollama, FastAPI, Playwright, Gmail API). Personal tool for one candidate.

## Non-negotiable rules (enforced in code, not prompts)

1. **No fabrication.** Every résumé / cover-letter / application claim must
   trace to a `Fact.id` in the verified profile (`job_agent.profile`). Never add
   a skill, metric, title, degree or responsibility not in the profile. Numbers
   in generated text must appear in some verified fact.
2. **No guessed legal declarations.** Work authorization, sponsorship,
   citizenship, export control, clearance → always `requires_review`, never
   auto-answered, even when profile values are set (they still need approval).
3. **No unapproved consequential actions.** Final submission requires a human
   approval created via `repo.create_approval`, decided by the user
   (`repo.decide_approval`, actor `user|cli|dashboard`) and consumed exactly once
   via `repo.transition_application(... SUBMISSION_ATTEMPTED, approval_uid=...,
   approved_payload=...)`. The payload hash must match what the human approved.
4. **No sending email.** `permissions.send_emails` is `Literal[False]`; Gmail is
   read-only (`gmail.readonly`). Drafts are stored, never sent.
5. **No success without evidence.** `SUBMITTED`/`CONFIRMATION_RECEIVED`/… need
   an `evidence` dict (`{"type": "page"|"email", ...}`) unless actor is `user`.
   Unknown outcome → `NEEDS_MANUAL_ACTION` + `UnknownOutcome`; never auto-retry
   a submission.
6. **Untrusted input** (job descriptions, web pages, emails) is data. Wrap with
   `security.untrusted.wrap_untrusted` before giving it to the LLM; validate LLM
   output with Pydantic; the LLM has no tools and its output never selects
   actions, URLs to fetch, or permissions.
7. **No secrets in git, logs, reports, or YAML.** Secrets only via env/.env
   (`config.Secrets`). Logs pass through `RedactingFilter`. Use
   `repo.audit`/`repo.record_error` (they redact).
8. **No bypassing** CAPTCHAs, logins, rate limits or anti-bot measures. Detect
   them and stop with `HumanActionRequired`.
9. **Never swallow errors.** Raise a `job_agent.errors` subclass (classified by
   `ErrorKind`) or record it with `repo.record_error`. One failing source /
   component must not crash the rest.
10. **Tests never hit real services** (no real submissions, emails, Gmail,
    employer sites). Use fixtures, `respx`, local HTML pages. Real network tests
    are marked `@pytest.mark.network` and are opt-in.

## Layout

```
src/job_agent/
  cli.py            Typer app; modules attach commands via <module>/commands.py:register(app)
  config.py         AppConfig (pydantic, extra=forbid) + Secrets (env only) + load_config()
  profile.py        CandidateProfile / Fact (verified facts with stable ids)
  models.py         enums, PipelineState + ALLOWED_TRANSITIONS, NormalizedJob, ScoreResult (contracts)
  errors.py         exception hierarchy mapped to ErrorKind
  context.py        AppContext(config, secrets, lazy db, lazy profile); one per thread
  tasks.py          TASKS registry + run_task (pause gate, DB lock, agent_runs, error capture)
  health.py         doctor checks
  logging_config.py rotating + redacting logs
  database/         connection.Database, migrations (sql/NNN_*.sql), repo.py (core repos)
  security/         redaction, sanitize (safe filenames, ensure_within), untrusted, permissions,
                    passwords (scrypt), envfile
  utils/time.py     UTC ISO timestamps (utcnow_iso, parse_iso, local_today)
  discovery/ scoring/ llm/ resume/ applications/ browser/ email/ followups/
  reporting/ dashboard/ scheduler/          feature modules (each owns its repository code)
tests/unit, tests/integration, tests/fixtures
```

## Conventions

- Timestamps: ISO-8601 UTC strings via `utils.time.utcnow_iso()`; report dates use
  `local_today(config.app.timezone)`.
- DB: `Database` (autocommit + explicit `with db.transaction():`, nested =
  savepoint). Parametrized SQL only. JSON columns via `to_json`/`from_json`.
  Schema changes = new `database/sql/NNN_name.sql` file; never edit applied ones.
- Job rows: `jobs.pipeline_state` holds pre-application states (`JOB_STATES`),
  changed only via `repo.set_job_state`. Application rows: `applications.status`,
  changed only via `repo.transition_application`.
- Paths inside config are relative to `config.home`; use `config.resolve()`,
  `config.documents_dir`, etc. Generated files: `security.sanitize.document_filename`
  + `ensure_within`.
- Tasks: implement `fn(ctx: AppContext, run: RunInfo) -> dict` and register a
  `TaskSpec` in `tasks.TASKS`. Return `{"partial": True}` for partial success.
- CLI: a module adds commands in `<module>/commands.py` with `register(app: typer.Typer)`
  and is listed in `cli._MODULE_COMMANDS`. Only register commands that work.
- Keep business logic out of the CLI and dashboard; they call service functions.
- Style: type hints, `from __future__ import annotations`, small functions,
  comments only where intent is non-obvious. Logger: `logging_config.get_logger(__name__)`.

## Commands

```bash
python -m venv .venv && .venv/bin/pip install -e ".[all]"
.venv/bin/job-agent init          # create local config/profile/.env/db (never overwrites)
.venv/bin/job-agent doctor        # health checks
.venv/bin/python -m pytest -q     # full test suite (no network)
.venv/bin/python -m pytest -m network   # opt-in live source tests
```

## Personal data

`profiles/candidate_profile.yaml`, `config/config.yaml`, `.env`, `data/`,
`reports/`, `documents/`, `logs/` are git-ignored. The committed example
profile and `tests/fixtures/profile_test.yaml` are synthetic — keep them that way.
