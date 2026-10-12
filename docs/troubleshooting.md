# Troubleshooting

Start every investigation with:

```bash
job-agent doctor        # what is wrong and the next step for each problem
job-agent status        # recent runs and errors needing attention
```

Logs are in `logs/job_agent.log` (rotated; secrets and contact details
redacted). Add `-v` to any command to see info-level logs on the console.

## Installation

| Symptom | Fix |
|---|---|
| `job-agent` is not recognized (Windows) | Activate the venv: `.\.venv\Scripts\Activate.ps1`, or call `.\.venv\Scripts\job-agent.exe` directly. |
| `Activate.ps1 cannot be loaded because running scripts is disabled` | Run once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. |
| `py` / `python` not found | Install Python 3.13 with the python.org *Windows installer (64-bit)*; the `py` launcher comes with it. Then use `py -3.13`. |
| `pip install` fails building a package | Use Python **3.13** (`py -3.13 -m venv .venv`); very new Python releases may lack prebuilt wheels for some dependencies. Upgrade pip first: `.\.venv\Scripts\python -m pip install --upgrade pip`. |
| `no configuration found` | Run `job-agent init` from the project folder, or set `JOB_AGENT_HOME` to it. |
| `doctor` says "using example config" | `job-agent init` creates `config/config.yaml`; edit that file, not the example. |

## Configuration and profile

| Symptom | Fix |
|---|---|
| `invalid configuration:` followed by a list | Each line names the exact key and problem (unknown keys are rejected to catch typos). Fix and run `job-agent config-check`. |
| `config key ... looks like a secret` | Secrets belong in `.env`, never in YAML. Remove the key. |
| `invalid candidate profile` | Usually a YAML indentation error, a duplicate fact `id`, or a `metrics` value that does not appear in that fact's text. |
| Documents marked **needs_review** | Placeholders like `<Your Full Name>` remain in the profile, or the checker flagged something — `job-agent documents list <job-uid>` shows the issues. |

## Discovery

| Symptom | Fix |
|---|---|
| `board not found` (404) for a source | The employer's board id is wrong or they left that ATS. See `docs/sources.md` to find the right id; remove it from `config.yaml` otherwise. |
| `access denied` (401/403) | The board blocks API access. job-agent does not work around that; remove the source. |
| Few or no new jobs | `job-agent sources` shows per-source status. Check `search.target_titles`, `exclude_title_keywords`, `locations`, `max_years_experience` and `max_posting_age_days`; filtered jobs are kept with a reason (visible in the Excel "All Jobs" sheet). |
| A run says `deferred` | The per-run limit (`search.max_jobs_per_run`) was reached; the rest is picked up next run. |
| `run-once` is slow | The local AI analyzes up to `scoring.max_llm_jobs_per_run` jobs on your CPU. Use `job-agent run-once --no-llm` for a fast rules-only run. |

## Local AI (Ollama)

| Symptom | Fix |
|---|---|
| `Ollama not reachable` | Start the Ollama app (Windows/macOS) or `ollama serve` (Linux). Check `job-agent llm check`. |
| `model ... is not pulled` | `ollama pull qwen2.5:7b-instruct` (or the model in `llm.model`). |
| Timeouts / `LLMUnavailable` during runs | CPU inference is slow. Raise `llm.timeout_seconds` (e.g. 600), lower `scoring.max_llm_jobs_per_run`, or use `qwen2.5:3b-instruct`. Measure with `job-agent llm benchmark`. |
| Laptop becomes sluggish | The 7B model uses ~5–6 GB RAM and all CPU cores while analyzing. Close the browser-heavy work or use the 3B model. |
| `llm.base_url must point to localhost` | By design: the local-first policy forbids remote inference servers. |

## Documents and applications

| Symptom | Fix |
|---|---|
| A rewording was rejected | Expected when the AI changed a number, added a tool or claim. The verbatim verified fact is used instead; the reason is stored with the document. |
| `run job-agent prepare <job-uid> first` | `apply` needs approved-or-draft documents for that job. |
| `apply` stops with "security challenge" / "login required" | The site has a CAPTCHA, login or account step. Complete it manually using the prepared answers (`job-agent approvals show <id>`). |
| `apply` says "needs manual action" | Unsupported form (iframe, multi-step, custom widgets) or closed listing. The next action explains what to do. |
| Chromium does not start | `python -m playwright install chromium`. |
| `applications submit` refuses | Browser submission is off by default (`applications.browser_submission_enabled`). Submit yourself, then `job-agent applications mark-submitted <application-uid>`. |
| An application shows **unknown outcome** | A submission may or may not have gone through. Check your email / the employer portal first, then record the truth (`mark-submitted`) — never resubmit blindly. |

## Gmail

| Symptom | Fix |
|---|---|
| `OAuth client file missing` | Follow `docs/gmail-setup.md`; save the client JSON as `data/secrets/gmail_credentials.json`. |
| `access_denied` / "app not verified" during sign-in | Add your Google account as a **test user** on the OAuth consent screen; click *Continue* past the unverified-app warning (it is your own app). |
| `authorization problem` / RefreshError after a week | Testing-mode tokens expire after 7 days. Run `job-agent gmail auth` again. |
| Emails not linked to applications | Recruiters writing from personal or agency addresses can't be matched automatically. Link them: `job-agent emails associate <email-id> <application-uid>`. |

## Dashboard and phone

| Symptom | Fix |
|---|---|
| `dashboard login not configured` | `job-agent dashboard set-password`. |
| 400 "Bad Request: unexpected Host header" on the phone | Add your Tailscale name (from `tailscale serve status`) to `dashboard.allowed_hosts` and restart the dashboard. |
| Login works on the laptop but not via Tailscale (cookie not kept) | Set `dashboard.secure_cookies: true` when using `tailscale serve` (HTTPS). Use `false` for plain `http://127.0.0.1`. |
| 429 Too Many Requests at login | Rate limit after repeated failures; wait a minute. |
| Phone can't reach it at all | Laptop asleep/off, Tailscale disconnected on either device, or the dashboard isn't running. |
| Everyone logged out | The dashboard was restarted (sessions are in memory) or the session secret changed. |

## Scheduler

| Symptom | Fix |
|---|---|
| `another daemon is already running` (exit 3) | Only one daemon per project. Stop the other one (Task Scheduler / `systemctl --user stop job-agent`). |
| Tasks show `skipped` | Automation is paused (`job-agent resume`) or the same task was already running. |
| A run stayed `running` | The process was killed. The next daemon start marks it `interrupted`; nothing consequential is retried automatically. |
| Schedules ignore my config change | The daemon reads config at start — restart it. `job-agent schedule` says "The config changed since the daemon started; restart it to apply." On Windows: `Stop-ScheduledTask -TaskPath '\job-agent\' -TaskName 'job-agent daemon'` then `Start-ScheduledTask ...`. |
| Nothing ran overnight | The laptop was asleep. Missed daily tasks run once after the daemon is running again. See `docs/scheduling.md` for power settings. |

## Database

| Symptom | Fix |
|---|---|
| `database is locked` | Another process holds a long write. Usually transient; retry. Don't open the DB in another tool while the agent runs. |
| `integrity check` fails / corruption | Stop the daemon and dashboard, restore the newest backup from `data/backups/` over `data/job_agent.db` (delete `job_agent.db-wal` and `-shm`), run `job-agent doctor`. |
| `schema version ... is newer than this code supports` | You ran an older job-agent against a newer database. Update the code (`git pull`, `pip install -e ".[all]"`). |
| Excel report won't regenerate on Windows | The workbook is open in Excel; close it and run `job-agent report` again. |

## Still stuck

Run `job-agent doctor` and `job-agent status`, check the end of
`logs/job_agent.log`, and look at the Activity Log sheet of today's report.
Never paste `.env`, `data/secrets/*` or your database into an issue or chat.

## Windows specifics

| Symptom | Fix |
|---|---|
| Files locked / "access denied" / strange sync conflicts | The project is inside a OneDrive-synced folder (often `Documents`). Move it to `C:\Users\<you>\Application_AI`. Windows Security "Controlled folder access" can also block writes — allow `python.exe`. |
| Task Scheduler shows *Last Run Result* `0x3` | A daemon was already running (e.g. you started `job-agent daemon` by hand). Only one may run; stop the manual one. |
| Dashboard not reachable after a reboot | Install the startup task with `-WithDashboard` (`scripts\windows\install-task.ps1 -WithDashboard -Force`). |
| `unknown timezone 'America/New_York'` | Time-zone data is missing: `.\.venv\Scripts\python -m pip install tzdata`. |
| `pip` tries to compile packages / needs a C compiler | You are on a very new Python. Recreate the venv with `py -3.13 -m venv .venv`. |

## More dashboard cases

| Symptom | Fix |
|---|---|
| `gmail sync` says Gmail is disabled | A manual sync also needs `gmail.enabled: true`; `scheduler.tasks.email_sync.enabled` only controls the 30-minute schedule. |
| Port 8765 already in use | Set `dashboard.port` (e.g. 8766) and re-run `tailscale serve --bg 8766`. |
| `allowed_hosts` entry rejected by `config-check` | Use the bare host name, e.g. `my-laptop.tail1234.ts.net` (a full `https://…` URL is reduced to its host automatically; paths or user names are rejected). |
| `doctor` says Chromium does not start | `.\.venv\Scripts\python -m playwright install chromium`. |
