# Application_AI — local-first job application agent

A personal, human-supervised AI job-search assistant that runs on your laptop.
It discovers jobs from public employer job boards, scores them against your
verified profile, prepares truthful tailored documents, tracks applications,
and reports daily — **and never submits an application or sends an email
without your explicit approval.**

> Status: under active development. See *Implementation status* below for
> exactly what works today. Nothing listed as "planned" should be assumed to work.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[all]"
.venv/bin/job-agent init      # creates config/config.yaml, profiles/candidate_profile.yaml, .env, data/job_agent.db
.venv/bin/job-agent doctor    # health checks
```

Then edit `profiles/candidate_profile.yaml` (your verified facts only) and
`config/config.yaml` (titles, locations, salary, schedules). Neither file is
committed to git.

## Implementation status

| Area | Status |
|---|---|
| Packaging, config validation, profile model, SQLite schema + migrations, logging with redaction, CLI (`init`, `doctor`, `status`, `pause`, `resume`, `config-check`, `db backup/info`, `tasks`) | Implemented (Phase 1) |
| Discovery, scoring, local LLM, documents, Excel reports, browser preparation, Gmail, follow-ups, dashboard, scheduler | In progress |
