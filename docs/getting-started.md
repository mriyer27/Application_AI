# Getting started: running job-agent on your laptop and phone

This is the complete, step-by-step guide. It assumes **Windows 11** (HP Pavilion
Aero 13). macOS/Linux differences are noted in boxes. Budget about 45 minutes
for the first setup (most of it is downloads).

> **What you get:** the agent runs on your laptop, searches public job boards
> twice a day, ranks jobs against your verified profile, drafts truthful résumés
> and cover letters, prepares applications for your review, reads (never sends)
> Gmail, and writes a daily Excel report. Your phone shows a private dashboard
> where you can review and approve. Nothing is submitted or sent without your
> explicit approval.

---

## Part A — One-time laptop setup

### A1. Install the prerequisites

1. **Python 3.12 or 3.13** — <https://www.python.org/downloads/windows/>.
   In the installer tick **"Add python.exe to PATH"**.
2. **Git** — <https://git-scm.com/download/win> (defaults are fine).
3. **Ollama** (local AI, free, runs offline) — <https://ollama.com/download/windows>.
   It starts automatically at login and listens only on your laptop
   (`127.0.0.1:11434`).

Open **PowerShell** (Start → type "PowerShell") and check:

```powershell
py --version        # Python 3.12.x or 3.13.x
git --version
ollama --version
```

> macOS: `brew install python git` and the Ollama app from ollama.com.
> Linux: your package manager for python3/git; `curl -fsSL https://ollama.com/install.sh | sh`.

### A2. Download the project and install it

```powershell
cd $HOME\Documents
git clone https://github.com/mriyer27/Application_AI.git
cd Application_AI
git checkout claude/youthful-clarke-j1nkah      # until this branch is merged into main
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[all]"
.\.venv\Scripts\python -m playwright install chromium
```

If PowerShell refuses to run scripts later, run once:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

Activate the environment so you can type `job-agent` directly (do this in every
new PowerShell window):

```powershell
cd $HOME\Documents\Application_AI
.\.venv\Scripts\Activate.ps1
job-agent --help
```

> macOS/Linux: `python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[all]" && python -m playwright install chromium`.

### A3. Create your local files

```powershell
job-agent init
```

This creates (never overwrites, never committed to git):

- `config\config.yaml` — your search settings
- `profiles\candidate_profile.yaml` — your verified facts
- `.env` — secrets (login hash, session key)
- `data\job_agent.db` — the database

**Use the profile I prepared for you:** copy the `candidate_profile.yaml` file I
sent you in chat over `profiles\candidate_profile.yaml`. It already contains
your verified education, experience and skills. Then open it in Notepad (or VS
Code) and:

1. Replace `<Your Full Name>`, `<you@example.com>`, `<+1 555 555 5555>`,
   `<City, ST>` with your real details.
2. Replace `<Confirm exact title held (IT / Software Development)>` with the
   exact title you held.
3. **Work authorization** (`work_authorization:` section) — leave values as
   `null` unless you have verified them (e.g. with your school's
   international student office). `null` means every legal question is sent to you;
   nothing is ever guessed.
4. Optional: add `verified_answers` for recurring questions, e.g.

   ```yaml
   verified_answers:
     - id: hear_about_us
       question_patterns: ["how did you hear"]
       answer: "Company careers page"
       is_legal_declaration: false
   ```

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

On a Ryzen 7 5800U with 16 GB RAM expect a few tokens per second — fine for
analyzing ~10 top jobs per run in the background. If it feels too slow, use the
faster model: `ollama pull qwen2.5:3b-instruct` and set `llm.model:
qwen2.5:3b-instruct` in `config\config.yaml`. Without Ollama everything still
works with rules-based scoring.

### A5. Tune your search (optional)

Open `config\config.yaml`. The most useful settings:

| Setting | What it does |
|---|---|
| `search.target_titles` | Roles you want (already set to your 18 target paths) |
| `search.exclude_title_keywords` | Skips senior/staff/engineering titles |
| `search.locations`, `remote_preference` | Where you'll work |
| `search.employment_types` | Add `internship` to include internships |
| `scoring.salary_target_base` | Your base-salary target (90000) — a ranking factor only |
| `sources.greenhouse.boards` / `lever.companies` / `ashby.boards` | Which employers' public job boards to search — see `docs/sources.md` to add more |
| `scheduler.tasks` | When things run (times are in `app.timezone`) |

Run `job-agent config-check` after editing; mistakes are reported precisely.

### A6. Health check

```powershell
job-agent doctor
```

Green/`OK` rows are ready. `WARN` rows tell you exactly what to do next
(placeholders left in your profile, dashboard password not set, Gmail not
connected, ...).

---

## Part B — Using it day to day (laptop)

### B1. First run by hand

```powershell
job-agent run-once                  # discover + score (+ local AI on top matches)
job-agent jobs --top 20             # ranked list with job IDs like J-1a2b3c4d5e
job-agent job show J-1a2b3c4d5e     # why it fits, what's missing, eligibility notes
```

### B2. Prepare an application

```powershell
job-agent prepare J-1a2b3c4d5e      # tailored résumé + cover letter (DOCX/PDF/MD)
```

Files land in `documents\J-1a2b3c4d5e\`, named like
`Employer_Role_Resume_2026-10-12.pdf`. Every bullet is printed with the
verified fact it came from. **Read them before using them.** Approve or reject:

```powershell
job-agent documents list J-1a2b3c4d5e
job-agent documents approve <document-id>
```

Then let the agent fill the employer's form (it never clicks Submit):

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

Approve, then submit:

- **Default (recommended to start):** the agent does *not* submit. After you
  approve, open the application URL, submit it yourself using the prepared
  answers, then record it:

  ```powershell
  job-agent approvals approve <approval-id>
  job-agent applications mark-submitted <application-id>
  ```

- **Optional:** after you trust the prepared forms, set
  `applications.browser_submission_enabled: true` in `config\config.yaml`; then
  `job-agent applications submit <approval-id>` submits *that one approved
  application*, once, and records it as submitted **only** if a confirmation page
  is detected. Anything ambiguous is marked "needs manual action" and is never
  retried automatically.

If a site shows a CAPTCHA or needs a login, the agent stops and tells you to
continue manually — it never bypasses them.

### B3. Reports

```powershell
job-agent report                    # reports\Job_Search_Report_YYYY-MM-DD.xlsx + Daily_Summary_YYYY-MM-DD.md
job-agent status                    # quick overview in the terminal
```

### B4. Gmail (optional, read-only)

Follow `docs/gmail-setup.md` (about 15 minutes in Google Cloud Console; free).
Then:

```powershell
job-agent gmail auth                # opens your browser for Google sign-in (never asks for your password itself)
job-agent gmail sync
job-agent emails --attention
```

Set `gmail.enabled: true` and `scheduler.tasks.email_sync.enabled: true` in
`config\config.yaml` to check mail every 30 minutes. Google's "Testing" mode
expires the authorization after 7 days — when `doctor` says so, run
`job-agent gmail auth` again.

### B5. Turn on automation

Try it in the foreground first:

```powershell
job-agent daemon                    # Ctrl+C to stop
```

Then make it start automatically when you log in (Windows Task Scheduler):

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows\install-task.ps1
```

Pause and resume at any time (manual commands keep working while paused):

```powershell
job-agent pause --reason "interview week"
job-agent resume
```

> macOS: `scripts/macos/` (launchd). Linux: `scripts/install-systemd-user.sh`.
> Details: `docs/scheduling.md`.

**Honest limits:** nothing runs while the laptop is asleep, shut down or offline.
Missed daily tasks run once when the laptop wakes and the daemon is running.
To maximize uptime, plug in and set *Settings → System → Power → Screen and
sleep → "When plugged in, put my device to sleep after" → Never* (optional).

---

## Part C — Phone access

The dashboard runs on your laptop. Your phone reaches it through **Tailscale**,
a free private network between your own devices — the dashboard is never
exposed to the public internet.

### C1. Set a dashboard password (laptop)

```powershell
job-agent dashboard set-password    # username + a password of 12+ characters
job-agent dashboard                 # open http://127.0.0.1:8765 on the laptop to test
```

Leave it running (or install it as a startup task — see below).

### C2. Install Tailscale

1. Laptop: <https://tailscale.com/download/windows> → sign in.
2. Phone: install **Tailscale** from the App Store / Google Play → sign in with
   the **same account**. Both devices now appear at
   <https://login.tailscale.com/admin/machines>.
3. In the admin console, open **DNS** and make sure **MagicDNS** and **HTTPS
   Certificates** are enabled.

### C3. Publish the dashboard privately (laptop)

In a PowerShell window:

```powershell
tailscale serve --bg 8765
tailscale serve status              # shows your private URL, e.g. https://hp-aero.tail1234.ts.net
```

Edit `config\config.yaml`:

```yaml
dashboard:
  host: 127.0.0.1                   # unchanged: still loopback only
  secure_cookies: true              # Tailscale serves HTTPS
  allowed_hosts: ["hp-aero.tail1234.ts.net"]   # your name from `tailscale serve status`
```

Restart `job-agent dashboard`.

### C4. Open it on your phone

1. Turn on Tailscale on the phone (VPN toggle).
2. Open the URL from `tailscale serve status` in Safari/Chrome.
3. Log in. Add it to your home screen (Share → *Add to Home Screen*).

From the phone you can see new matches, read fit explanations, approve or
reject prepared applications, review emails and follow-ups, open the daily
summary, start a discovery run, generate a report and pause/resume the agent.

**Do not** use `tailscale funnel`, ngrok, router port-forwarding, or
`host: 0.0.0.0` — those expose your job-search data to the internet. More in
`docs/remote-access.md`.

---

## Part D — Keeping it healthy

| Task | Command |
|---|---|
| Health check | `job-agent doctor` |
| What happened recently | `job-agent status` |
| Back up the database (weekly) | `job-agent db backup` → `data\backups\` |
| Update the code | `git pull` then `pip install -e ".[all]"` |
| Pause everything | `job-agent pause` |
| Logs | `logs\job_agent.log` (secrets and contact details are redacted) |

Keep `data\`, `documents\`, `reports\`, `profiles\` and `.env` private — they
are excluded from git. Troubleshooting: `docs/troubleshooting.md`.
