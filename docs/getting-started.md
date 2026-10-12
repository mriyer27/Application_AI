# Getting started: running job-agent on your laptop and phone

The complete, step-by-step guide. It assumes a **Windows 11** laptop (16 GB
RAM, no dedicated GPU is fine) and an iPhone or Android phone; macOS/Linux
differences are in boxes. Budget about 45 minutes for the first setup — most
of it is downloads.

> **What you get:** the agent runs on your laptop, searches public employer
> job boards twice a day, ranks jobs against your verified profile, drafts
> truthful résumés and cover letters, prepares applications for your review,
> reads (never sends) Gmail, and writes a daily Excel report. Your phone shows a
> private dashboard where you review and approve. **Nothing is submitted or
> sent without your explicit approval.**

> **Honest status:** everything was built and tested on Linux (≈1,000
> automated tests plus live runs against real job boards and a real local
> model). The Windows-specific pieces (PowerShell scripts, Task Scheduler,
> file locking) were written for Windows and syntax-checked but have **not yet
> been run on a Windows PC**. If a step fails, `docs/troubleshooting.md` covers
> the common problems.

---

## Part A — One-time laptop setup

### A1. Install the prerequisites

1. **Python 3.13** — <https://www.python.org/downloads/windows/> → under
   *Python 3.13.x* choose **Windows installer (64-bit)**. In the installer tick
   **"Add python.exe to PATH"**. (Avoid the newest 3.14/3.15 releases for now:
   some dependencies may not have prebuilt Windows packages yet.)
2. **Git** — <https://git-scm.com/download/win> (defaults are fine).
3. **Ollama** (local AI, free, runs offline) — <https://ollama.com/download/windows>.
   It starts automatically at login and listens only on your laptop
   (`127.0.0.1:11434`).

Open **PowerShell** (Start → type "PowerShell") and check:

```powershell
py -3.13 --version   # Python 3.13.x
git --version
ollama --version
```

Allow PowerShell to run the virtual-environment activation script (once,
required on Windows 11):

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

> macOS: `brew install python@3.13 git` and the Ollama app from ollama.com.
> Linux: python3.13 + git from your package manager; `curl -fsSL https://ollama.com/install.sh | sh`.

### A2. Download the project and install it

Put the project directly in your user folder — **not** in `Documents`, which
OneDrive often syncs to the cloud (that would upload your database and
documents and can lock files):

```powershell
cd $HOME
git clone https://github.com/mriyer27/Application_AI.git
cd Application_AI
git checkout claude/youthful-clarke-j1nkah   # the code lives on this branch until it is merged into main
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[all]"
.\.venv\Scripts\python -m playwright install chromium
```

Activate the environment so you can type `job-agent` directly (in every new
PowerShell window):

```powershell
cd $HOME\Application_AI
.\.venv\Scripts\Activate.ps1
job-agent --help
```

> macOS/Linux: `python3.13 -m venv .venv && source .venv/bin/activate && pip install -e ".[all]" && python -m playwright install chromium`.

### A3. Create your local files

```powershell
job-agent init
```

This creates (never overwrites, never committed to git):

- `config\config.yaml` — your search settings
- `profiles\candidate_profile.yaml` — your verified facts
- `.env` — secrets (login hash, session key)
- `data\job_agent.db` — the database

**Your profile.** If you received a prepared `candidate_profile.yaml`
privately, copy it over `profiles\candidate_profile.yaml`. Otherwise fill in
the example that `init` copied. Then open it in Notepad or VS Code and:

1. Replace **every** value in `<angle brackets>` (name, email, phone,
   location, any title you still need to confirm). `job-agent doctor` lists
   any placeholders that remain; documents generated while they remain are
   marked *needs review*.
2. Keep only facts you can stand behind. Each experience bullet is a `fact`
   with an `id`; list any numbers that appear in its text under `metrics`.
   Keep `keywords` faithful to the fact — they count as evidence when jobs are
   scored.
3. **Work authorization** (`work_authorization:`) — leave values `null` unless
   you have verified them (e.g. with your school's international student
   office). `null` means every legal or eligibility question is sent to you;
   nothing is ever guessed.
4. Optional: answers you confirm for recurring questions:

   ```yaml
   verified_answers:
     - id: hear_about_us
       question_patterns: ["how did you hear"]
       answer: "Company careers page"
       is_legal_declaration: false
   ```

   Legal questions (sponsorship, authorization, citizenship, …) are only ever
   filled from an entry with `is_legal_declaration: true`, and are still shown
   to you for approval.

Check it:

```powershell
job-agent config-check
```

### A4. Download the local AI model

```powershell
ollama pull qwen2.5:7b-instruct     # ~4.7 GB, the recommended default
job-agent llm check
job-agent llm recommend             # what fits your hardware and why
job-agent llm benchmark             # measures real speed on your laptop
```

On a CPU-only laptop expect **a minute or more per job analysis** with the 7B
model; only the top ~10 jobs per run are analyzed, in the background. If it is
too slow, `ollama pull qwen2.5:3b-instruct` and set `llm.model:
qwen2.5:3b-instruct` in `config\config.yaml` (faster, less accurate). Without
Ollama everything still works with rules-based scoring.

### A5. Tune your search (optional)

Open `config\config.yaml`. The most useful settings:

| Setting | What it does |
|---|---|
| `search.target_titles` | Roles you want (preset to the 18 target career paths) |
| `search.exclude_title_keywords` | Skips senior/staff/engineering titles |
| `search.locations`, `remote_preference` | Where you'll work. "United States" also matches "City, ST" listings; remote roles limited to other countries are excluded |
| `search.employment_types` | Add `internship` to include internships |
| `scoring.salary_target_base` | Base-salary target (90000) — a ranking factor only |
| `sources.greenhouse.boards` / `lever.companies` / `ashby.boards` | Which employers' public job boards to search — see `docs/sources.md` to add more |
| `scheduler.tasks` | When things run (times are in `app.timezone`) |

Run `job-agent config-check` after editing; mistakes are reported precisely.

### A6. Health check

```powershell
job-agent doctor
```

`OK` rows are ready. `WARN` rows tell you exactly what to do next
(placeholders left in your profile, dashboard password not set, Gmail not
connected, ...).

---

## Part B — Using it day to day (laptop)

### B1. First run by hand

```powershell
job-agent run-once                  # discover + score (+ local AI on top matches) + draft documents
job-agent run-once --no-llm         # same, fast, rules only
job-agent jobs --top 20             # ranked list with job IDs like J-1a2b3c4d5e
job-agent job show J-1a2b3c4d5e     # why it fits, what's missing, eligibility notes
```

### B2. Prepare an application

```powershell
job-agent prepare J-1a2b3c4d5e      # tailored résumé + cover letter (DOCX/PDF/MD)
```

Files land in `documents\J-1a2b3c4d5e\`, named like
`Employer_Role_Resume_2026-10-12.pdf`. Every bullet is printed with the
verified fact it came from. **Read them, then approve or reject them:**

```powershell
job-agent documents list J-1a2b3c4d5e
job-agent documents approve <document-id>
```

Then let the agent fill the employer's form — it **never clicks Submit**:

```powershell
job-agent apply J-1a2b3c4d5e        # add --headed to watch the browser
job-agent approvals                 # what's waiting for you
job-agent approvals show <approval-id>
```

The approval view lists the documents, every answer, **legal/eligibility
questions (always left for you)**, unanswered fields and anything uncertain.
Answer the flagged fields:

```powershell
job-agent approvals answer <application-id> <field-key> "your answer"
```

Each answer replaces the approval request and **prints a new approval ID** —
use the newest one (or run `job-agent approvals` again).

Approve, then submit:

- **Default (recommended):** the agent does *not* submit. After you approve,
  open the application URL, submit it yourself using the prepared answers, then
  record it:

  ```powershell
  job-agent approvals approve <approval-id>
  job-agent applications mark-submitted <application-id>
  ```

- **Optional:** once you trust the prepared forms, set
  `applications.browser_submission_enabled: true` in `config\config.yaml`; then
  `job-agent applications submit <approval-id>` submits *that one approved
  application*, once, and records it as submitted **only** when a
  confirmation page is detected. It refuses while a document isn't approved or
  a field still needs you. Anything ambiguous becomes "needs manual action"
  and is never retried automatically.

If a site shows a CAPTCHA or needs a login, the agent stops and tells you to
continue manually — it never bypasses them.

If an email or a mistake set the wrong status, correct it yourself:

```powershell
job-agent applications set-status <application-id> submitted --note "that rejection was for another role"
```

### B3. Reports

```powershell
job-agent report                    # reports\Job_Search_Report_YYYY-MM-DD.xlsx + Daily_Summary_YYYY-MM-DD.md
job-agent status                    # quick overview in the terminal
```

### B4. Gmail (optional, read-only)

Follow `docs/gmail-setup.md` (about 15 minutes in Google Cloud Console; free).
Then set `gmail.enabled: true` in `config\config.yaml` and run:

```powershell
job-agent gmail auth                # opens your browser for Google sign-in (the agent never sees your password)
job-agent gmail sync
job-agent emails --attention
```

To check mail automatically every 30 minutes, also set
`scheduler.tasks.email_sync.enabled: true`. Google's "Testing" mode expires
the authorization after 7 days — when `doctor` says so, run
`job-agent gmail auth` again.

### B5. Turn on automation

Try it in the foreground first:

```powershell
job-agent daemon                    # Ctrl+C to stop
```

Then stop it (Ctrl+C) and make the daemon **and the dashboard** start
automatically when you log in (Windows Task Scheduler):

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows\install-task.ps1 -WithDashboard -StartNow
```

Only one daemon can run at a time (a second one exits with code 3), so don't
also run `job-agent daemon` by hand while the task is running. The task
restarts the daemon within 30 minutes if it ever stops. Remove it with
`scripts\windows\uninstall-task.ps1`.

Pause and resume at any time (manual commands keep working while paused):

```powershell
job-agent pause --reason "interview week"
job-agent resume
job-agent schedule                  # next runs and daemon state
```

After changing `config\config.yaml`, restart the tasks so they pick it up:

```powershell
Stop-ScheduledTask  -TaskPath '\job-agent\' -TaskName 'job-agent daemon'
Start-ScheduledTask -TaskPath '\job-agent\' -TaskName 'job-agent daemon'
# same for 'job-agent dashboard'
```

> macOS: `scripts/macos/install-launchd.sh`. Linux: `scripts/install-systemd-user.sh`.
> Details: `docs/scheduling.md`.

**Honest limits:** nothing runs while the laptop is asleep, shut down or
offline. Missed daily tasks run once when the laptop is awake and the daemon
is running again. To maximize uptime while plugged in: *Settings → System →
Power & battery → Screen, sleep & hibernate timeouts → "When plugged in, put
my device to sleep after" → Never* (optional).

---

## Part C — Phone access

The dashboard runs on your laptop. Your phone reaches it through
**Tailscale**, a free private network between your own devices — the
dashboard is never exposed to the public internet.

### C1. Set a dashboard password (laptop)

```powershell
job-agent dashboard set-password    # username + a password of 12+ characters
job-agent dashboard                 # open http://127.0.0.1:8765 on the laptop to test, then Ctrl+C
```

(If you installed the startup task with `-WithDashboard`, it runs the
dashboard for you; restart it after changing settings — see B5.)

### C2. Install Tailscale

1. Laptop: <https://tailscale.com/download/windows> → sign in.
2. Phone: install **Tailscale** from the App Store / Google Play → sign in with
   the **same account**. Both devices now appear at
   <https://login.tailscale.com/admin/machines>.
3. In the admin console open **DNS** and make sure **MagicDNS** and **HTTPS
   Certificates** are enabled.

### C3. Publish the dashboard privately (laptop)

```powershell
tailscale serve --bg 8765
tailscale serve status              # shows your private URL, e.g. https://my-laptop.tail1234.ts.net
```

Edit `config\config.yaml` (use the host name from that URL):

```yaml
dashboard:
  host: 127.0.0.1                   # unchanged: still loopback only
  secure_cookies: true              # Tailscale serves HTTPS
  allowed_hosts: ["my-laptop.tail1234.ts.net"]
```

Restart the dashboard (Ctrl+C and `job-agent dashboard` again, or restart the
task as in B5). `tailscale serve --bg` keeps working after reboots.

### C4. Open it on your phone

1. Turn on Tailscale on the phone (VPN toggle).
2. Open the URL from `tailscale serve status` in Safari / Chrome and log in.
3. Add it to your home screen — iPhone: *Share → Add to Home Screen*;
   Android (Chrome): *⋮ → Add to Home screen*. (The home-screen app may ask
   you to log in once more.)

From the phone you can see new matches and their explanations, approve or
reject prepared applications, review emails and follow-ups, open the daily
summary, start a discovery run, generate a report and pause/resume the agent.

**Do not** use `tailscale funnel`, ngrok, router port-forwarding or
`host: 0.0.0.0` — those expose your job-search data to the internet. More in
`docs/remote-access.md`.

---

## Part D — Keeping it healthy

| Task | Command |
|---|---|
| Health check | `job-agent doctor` |
| What happened recently | `job-agent status` |
| Back up the database (weekly) | `job-agent db backup` → `data\backups\` |
| Update the code | `git pull`, then `.\.venv\Scripts\python -m pip install -e ".[all]"`, then restart the tasks (B5). Once the branch is merged, switch with `git checkout main`. |
| Pause everything | `job-agent pause` |
| Logs | `logs\job_agent.log` (secrets and contact details are redacted) |

Keep `data\`, `documents\`, `reports\`, `profiles\` and `.env` private — they
are excluded from git; never paste them into an issue or chat.
Troubleshooting: `docs/troubleshooting.md`.
