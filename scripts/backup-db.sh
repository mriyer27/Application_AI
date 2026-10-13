#!/usr/bin/env bash
# Consistent online backup of the job-agent SQLite database (safe while the
# daemon/dashboard run). Thin wrapper around "job-agent db backup"; arguments are
# passed through, e.g.:
#   scripts/backup-db.sh                          # -> data/backups/job_agent_<UTC timestamp>.db
#   scripts/backup-db.sh --dest ~/Private/job_agent.db
# Old backups are never deleted by this script. Backups contain your application
# history: keep them private (not in git, not in a public cloud folder).
#
# Daily at 22:05 via cron (crontab -e):
#   5 22 * * * /path/to/Application_AI/scripts/backup-db.sh >/dev/null
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${JOB_AGENT_HOME:-$(cd "$SCRIPT_DIR/.." && pwd)}"
EXE="$REPO_DIR/.venv/bin/job-agent"
if [[ ! -x "$EXE" ]]; then
    echo "error: $EXE not found; create the virtualenv first (docs/setup.md)" >&2
    exit 2
fi
cd "$REPO_DIR"
JOB_AGENT_HOME="$REPO_DIR" exec "$EXE" db backup "$@"
