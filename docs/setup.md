# Setup

Target machine: a typical laptop with 16 GB RAM and no dedicated GPU (e.g. a
Ryzen 7 5800U-class Windows 11 laptop). No GPU, cloud server or paid API is
required.

## 1. Prerequisites

- Python **3.13** recommended (3.11–3.13 supported) — on Windows use the python.org *Windows installer (64-bit)*; very new releases may lack prebuilt packages for some dependencies
- Git
- Optional but recommended: [Ollama](https://ollama.com/download) for local AI (see `local-llm.md`)

## 2. Install

**Windows (PowerShell)** — put the project in your user folder, not in a
OneDrive-synced `Documents` folder:

```powershell
cd $HOME
git clone https://github.com/mriyer27/Application_AI.git
cd Application_AI
git checkout claude/youthful-clarke-j1nkah   # until this branch is merged into main
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[all]"
.\.venv\Scripts\python -m playwright install chromium   # only needed for application form preparation
.\.venv\Scripts\job-agent init
```

**macOS / Linux**

```bash
git clone https://github.com/mriyer27/Application_AI.git
cd Application_AI
git checkout claude/youthful-clarke-j1nkah   # until this branch is merged into main
python3.13 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[all]"
.venv/bin/python -m playwright install chromium
.venv/bin/job-agent init
```

`job-agent init` never overwrites anything. It creates:

| File | Purpose | In git? |
|---|---|---|
| `config/config.yaml` | your settings (titles, locations, salary, sources, schedules) | no |
| `profiles/candidate_profile.yaml` | your verified facts — the only source for documents | no |
| `.env` | secrets (session secret now; dashboard login later) — mode 0600 | no |
| `data/job_agent.db` | SQLite database (source of truth) | no |

Activate the virtual environment (`.venv\Scripts\Activate.ps1` or
`source .venv/bin/activate`) to type `job-agent` directly; otherwise use the full
path as above. Run commands from the project folder, or set `JOB_AGENT_HOME`.

## 3. Fill in your profile

Edit `profiles/candidate_profile.yaml`:

- Replace every `<placeholder>` (name, email, phone, location). Documents
  generated while placeholders remain are marked *needs review*.
- Only add facts you can stand behind. Each bullet is a `fact` with an `id`;
  list numbers that appear in the text under `metrics`.
- `work_authorization`: leave values `null` until verified (e.g. with your
  international student office). Unknown values are never guessed; every legal
  question is routed to you.
- `verified_answers`: answers you confirm for recurring questions (e.g. "How did
  you hear about us?"). Legal declarations here are still shown for approval.

Validate with `job-agent config-check`.

## 4. Configure the search

Edit `config/config.yaml`: `search.target_titles`, `exclude_title_keywords`,
`locations`, `remote_preference`, `employment_types` (add `internship` if
wanted), `max_years_experience`, `scoring.salary_target_base`, the employer
boards under `sources`, and `scheduler.tasks`. See `sources.md`, `scoring.md`
and `scheduling.md`. Invalid settings are rejected with a precise message.

## 5. Local AI (optional, recommended)

Install Ollama and pull the recommended model (≈4.7 GB):

```bash
ollama pull qwen2.5:7b-instruct
job-agent llm check
job-agent llm recommend
```

Without Ollama everything still works with rules-based scoring and verbatim
documents. See `local-llm.md`.

## 6. Health check

```bash
job-agent doctor
```

## 7. First run

```bash
job-agent run-once              # discover + score (+ analysis if Ollama is running)
job-agent jobs --top 20         # ranked list
job-agent job show <job-uid>    # full explanation
job-agent prepare <job-uid>     # tailored résumé + cover letter (review them!)
job-agent report                # Excel + Markdown summary in reports/
```

## 8. Dashboard and phone access

```bash
job-agent dashboard set-password
job-agent dashboard             # http://127.0.0.1:8765
```

For phone access over a private VPN see `remote-access.md`.

## 9. Gmail (optional)

Follow `gmail-setup.md`, then set `gmail.enabled: true` and
`scheduler.tasks.email_sync.enabled: true`.

## 10. Recurring automation

```bash
job-agent daemon                # foreground; Ctrl+C to stop
```

To start it automatically at login see `scheduling.md` (Windows Task Scheduler,
macOS launchd, Linux systemd). The agent only runs while the laptop is on and
awake.

## Backups

```bash
job-agent db backup             # consistent online copy into data/backups/
```

To restore: stop the daemon/dashboard, copy a backup over `data/job_agent.db`
(also delete any `job_agent.db-wal`/`-shm` files), run `job-agent doctor`.
Keep backups somewhere private — they contain your application history.
