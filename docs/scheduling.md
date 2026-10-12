# Scheduling and operations

The scheduler daemon (`job-agent daemon`) runs the predefined tasks on a
schedule while your laptop is on and awake. It can only run tasks from the
built-in task list (`job-agent tasks list`); it never submits an application or
sends an email by itself. Everything consequential still waits for your
approval.

**This is not a 24/7 service.** Nothing runs while the laptop is asleep, off,
or logged out (unless you set that up on Linux). Runs that were missed are
caught up once when the laptop is back — see [Missed runs](#missed-runs-sleep-shutdown-catch-up).

## Default schedule

Times are in `app.timezone` (default `America/New_York`), not the laptop's
time zone. Daylight-saving changes are handled.

| Task | Default | What it does |
|---|---|---|
| `health` | every 60 min | system health checks (database, Ollama, Gmail token, disk) |
| `discover_and_score` | 07:15 and 16:15 | fetch new jobs, local-AI analysis of top matches, scoring |
| `email_sync` | every 30 min, **disabled** | read and categorize recent Gmail (read-only); enable after `gmail-setup.md` |
| `followups` | 08:00 | review applications and messages, draft follow-up suggestions |
| `report` | 21:30 | today's Excel report and Markdown summary |

See them, with the next run times, the daemon state and each task's last result:

```bash
job-agent schedule              # add -n 5 for five upcoming runs per task
```

## Changing the schedule

Edit `scheduler` in `config/config.yaml`, then restart the daemon (it reads
the config once at start; `job-agent schedule` says when a restart is needed):

```yaml
scheduler:
  enabled: true                 # false: the daemon refuses to start
  tasks:
    discover_and_score: {cron: "15 7,16 * * 1-5"}   # weekdays only
    email_sync: {interval_minutes: 30, enabled: true}
    followups: {cron: "0 8 * * *"}
    report: {cron: "30 21 * * *"}
    health: {interval_minutes: 60}
  lock_timeout_minutes: 90      # a task lock older than this is considered stale
  max_retries: 2                # retries after a *recoverable* failure
  retry_backoff_seconds: 120    # first retry wait; doubles each time
  misfire_grace_minutes: 60     # a run up to this late still runs
```

* Each entry has exactly one of `cron` (5 fields: minute hour day-of-month month
  day-of-week) or `interval_minutes` (5–1440).
* Cron uses standard crontab meaning, including day-of-week: `0` and `7` are
  Sunday, `1-5` is Monday–Friday, names (`mon`, `sat,sun`) work too.
* Known limitation: if you restrict **both** day-of-month and day-of-week, a run
  happens only on days matching both (classic cron runs on days matching either).
* `enabled: false` keeps the entry but does not schedule it.
* An unknown task name or an invalid cron expression is not scheduled; the
  daemon logs a warning, shows it in the start-up report, and records an error
  (with the fix) that appears in `job-agent status`, the dashboard and the report.
* Interval tasks count from daemon start (first run one interval after start,
  unless a catch-up run happens first).

## Running the daemon

```bash
job-agent daemon                # foreground; Ctrl+C stops it gracefully
job-agent daemon --once         # run each enabled task once now, then exit
```

On start the daemon prints a report: initialization, database integrity,
whether automation is paused, every scheduled task with its next run time,
anything not scheduled (and why), planned catch-up runs, pending approvals and
unresolved errors. It then runs until stopped. Detailed logs go to
`logs/job_agent.log` (secrets and personal data redacted).

`--once` runs the enabled tasks in the order health → discover_and_score →
email_sync (if enabled) → followups → report (then any others), with the same
retries and pause rules, and exits with code 1 if any task failed. It is useful
for testing and for OS schedulers that start the program periodically instead
of keeping a daemon running. Ctrl+C once finishes the current task and stops;
Ctrl+C twice aborts it (recorded as interrupted).

Exit codes: `0` ok, `1` a task failed (`--once`) or another error, `2`
configuration problem, `3` another daemon is already running.

## Overlap prevention

* **One daemon at a time.** The daemon holds an operating-system lock on
  `data/daemon.lock` (released automatically if the process dies) and records
  its pid/host in the database. A second `job-agent daemon` (or `--once`) exits
  with code 3 and a message saying which process holds it.
* **One run of a task at a time.** Every run takes a database lock for that
  task, shared by the daemon, `job-agent tasks run` and the dashboard. If a run
  is already in progress, the new one is recorded as `skipped` — not an error
  and not retried. Within the daemon a task also never overlaps itself
  (`max_instances: 1`), and runs that piled up while it was busy are merged into
  one (`coalesce`).
* Locks left by a process that no longer exists are released at the next start;
  otherwise a lock expires after `lock_timeout_minutes`.

## Retries

A failed run is retried automatically only when the error is **recoverable**
(network blip, timeout, server error, rate limit) and the task is marked safe to
retry. With the defaults that means at most 2 retries, after 120 s and then
240 s (`retry_backoff_seconds * 2^(n-1)`). Every attempt is a separate row in
`agent_runs` with its attempt number.

Never retried automatically: skipped runs (paused, or already running),
permanent errors, missing authorization (e.g. Gmail needs `job-agent gmail
auth`), anything that needs your action, and unknown outcomes. Those are
recorded with a "next step" for you and the task simply runs again at its next
scheduled time. Stopping the daemon cancels a pending retry wait immediately.

## Missed runs (sleep, shutdown, catch-up)

What happens when a scheduled time passes while nothing can run:

* **Late by less than `misfire_grace_minutes` (60):** the run happens as soon as
  the laptop wakes (the daemon re-checks the clock at least once a minute), once
  even if several were missed.
* **Missed by more, while the daemon kept running (laptop asleep):** the daemon
  runs that task once shortly after waking.
* **Missed while the daemon was not running (laptop off, logged out, daemon
  stopped):** on start, every task whose last successful run is older than its
  most recent scheduled time (cron) or older than its interval runs once, about
  5 s after start, one after another in the order health → discover_and_score →
  email_sync → followups → report. It is skipped if the regular run is due within
  15 minutes anyway.

Each missed run is caught up **at most once** — also across restarts and crash
loops — even if the catch-up run fails (the task then waits for its next
regular time). Catch-up runs at start are recorded with trigger `startup`. If
automation is paused, catch-up runs are skipped like any other scheduled run.

## Pause and resume

```bash
job-agent pause --reason "interviews this week"
job-agent resume
```

While paused the daemon keeps running (heartbeat, status) but every scheduled
run, retry and catch-up is recorded as `skipped`. Manual commands
(`job-agent tasks run …`, `discover`, `report`, …) still work. Pausing does not
interrupt a task that is already running.

## Stopping, restarts and recovery

* **Graceful stop** (Ctrl+C, `SIGTERM`, `systemctl --user stop`, `launchctl
  bootout`, Ctrl+Break on Windows): no new runs start, pending retries are
  cancelled, a task that is running finishes (this can take minutes during
  local-LLM analysis), then the daemon records `daemon_stopped_at` and releases
  its lock. `job-agent schedule` shows `stopped`.
* **Hard kill** (Task Manager "End task", Task Scheduler "End", `kill -9`, power
  loss, logoff/shutdown on Windows): the run in progress stays marked `running`
  until the next start, which marks it `interrupted` with an *unknown outcome*
  and releases its lock. It is not retried automatically; the task runs again at
  its next scheduled time or catch-up. The scheduled tasks are meant to be safe
  to re-run (discovery deduplicates jobs, the report is regenerated per day,
  emails are keyed by message id), and none of them submits applications or
  sends email.
* At every start the daemon also expires approvals past their deadline and
  refuses to run on a database that fails its integrity check (restore a backup,
  see `setup.md`).
* **Status:** the daemon writes a heartbeat every 60 s. `job-agent doctor`, the
  dashboard and `job-agent schedule` treat a heartbeat younger than 180 s as
  running; an older one is `stale` (killed, crashed, or the laptop is asleep).

## Start automatically

Run the daemon from your user account, not as administrator/root. All scripts
are in `scripts/`, change nothing until you run them, and print what they did.

### Windows 11 (Task Scheduler)

In PowerShell, from the repository folder:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\install-task.ps1 -StartNow
# optional: also the dashboard
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\install-task.ps1 -WithDashboard -Force -StartNow
```

This registers `\job-agent\job-agent daemon` for your user: runs
`.venv\Scripts\job-agent.exe daemon` one minute after you log on, without a
console window (a window may flash briefly; use `-ShowWindow` to keep a visible
console instead), with no run-time limit, on battery too, restart on failure (3
times, 5 minutes apart), "run as soon as possible after a missed start", and
never two copies. It runs only while you are logged on and needs no password or
admin rights.

Check it: `job-agent schedule`, or Task Scheduler → Task Scheduler Library →
job-agent (Last Run Result). Remove it:
`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\uninstall-task.ps1`.

Windows specifics, honestly:

* Ending the task, logging off or shutting down kills the process (no graceful
  stop); see "Hard kill" above. To stop between tasks, run `job-agent pause`
  first and wait until `job-agent schedule` shows no lock held.
* Task Scheduler's "restart on failure" applies when Task Scheduler considers
  the task failed; it may not react to every error exit. The daemon is built not
  to exit on task errors, and the logon trigger starts it again at your next
  logon.
* These PowerShell scripts were written for Windows PowerShell 5.1 / PowerShell
  7 but could not be executed in the (Linux) build environment; if a step fails,
  the same settings can be made in the Task Scheduler UI (Create Task → Triggers:
  At log on; Actions: start `job-agent.exe` with argument `daemon` in the
  repository folder; Settings: "Run task as soon as possible after a scheduled
  start is missed", "If the task fails, restart every 5 minutes", uncheck "Stop
  the task if it runs longer than"; Conditions: uncheck "Start the task only if
  the computer is on AC power").

### macOS (launchd)

```bash
scripts/macos/install-launchd.sh --load            # add --with-dashboard for the dashboard
launchctl print gui/$(id -u)/com.jobagent.daemon | head -20
launchctl bootout gui/$(id -u)/com.jobagent.daemon # graceful stop + unload
```

The LaunchAgent starts the daemon at login and restarts it after a crash (not
after a clean stop), at most once a minute. On unload launchd sends `SIGTERM`
and waits up to 15 minutes for a running task. Output goes to
`logs/launchd-daemon.*.log`. If you started a daemon by hand first, the
LaunchAgent copy exits with code 3 and launchd retries every minute until yours
stops. To remove: `bootout`, then delete
`~/Library/LaunchAgents/com.jobagent.*.plist`. (Not executed in the build
environment; the plists are validated XML.)

### Linux (systemd user service)

```bash
scripts/install-systemd-user.sh --enable           # add --with-dashboard for the dashboard
systemctl --user status job-agent
journalctl --user -u job-agent -f
systemctl --user stop job-agent                    # graceful (TimeoutStopSec=15min)
scripts/uninstall-systemd-user.sh
```

Without `--enable` the script only installs the unit files and prints the
`systemctl --user` commands. The unit restarts the daemon on failure (not for
configuration errors or "already running"), at a lower CPU priority. User
services run only while you are logged in unless you run
`loginctl enable-linger "$USER"`. Optional extra environment variables go in
`~/.config/job-agent/environment`; secrets are read from the repository's `.env`
by job-agent itself.

## Backups

```bash
scripts/backup-db.sh                 # = job-agent db backup -> data/backups/
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\backup-db.ps1
```

Both are safe while the daemon runs (SQLite online backup) and never delete old
backups. The script headers show how to run them daily (cron / Task Scheduler).
Backups contain your application history; keep them private.

## Operational limits (read this)

* **Asleep, off, offline = nothing runs.** Scheduled times that pass meanwhile
  are caught up once afterwards (see above). If the network is down, runs fail
  as recoverable, are retried twice, and otherwise wait for the next schedule.
* **Ollama must be running** for AI analysis and AI wording. The Ollama app
  starts at login on Windows and macOS by default; on Linux enable its service
  (`systemctl enable --now ollama`). Without it the agent keeps working with
  rules-based scoring and reports Ollama in `job-agent doctor`.
* **Gmail in "Testing" mode:** Google expires refresh tokens of OAuth apps in
  testing status after 7 days. `email_sync` then fails with "authorization
  required" (never retried) and `job-agent doctor` / the dashboard tell you to run
  `job-agent gmail auth` again. Publishing the OAuth app avoids the weekly
  re-authorization (see `gmail-setup.md`).
* **Power settings (laptop plugged in):**
  * Windows 11: Settings → System → Power & battery → Screen, sleep & hibernate
    timeouts → set "When plugged in, put my device to sleep after" to *Never* (or
    a long time). Control Panel → Power Options → "Choose what closing the lid
    does" → *Do nothing* when plugged in, if you keep it closed. Laptops with
    Modern Standby (common on thin laptops like the HP Pavilion Aero 13) may keep
    the network partly alive while "asleep", but do not count on scheduled runs
    happening then.
  * macOS: System Settings → Battery → Options → "Prevent automatic sleeping on
    power adapter when the display is off", or run `caffeinate -s` while plugged
    in.
  * Linux: your desktop's power settings, or `systemd-inhibit` for long runs.
* Local-LLM analysis on a laptop CPU is slow; `scoring.max_llm_jobs_per_run`
  bounds each discovery run, and a graceful stop waits for it to finish.
* The daemon reads the config only at start; restart it after changing
  `config/config.yaml` or the profile.
