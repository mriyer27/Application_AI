#!/usr/bin/env bash
# macOS: install the job-agent LaunchAgent(s) for the current user.
#
# Renders scripts/macos/com.jobagent.*.plist (replacing @REPO_DIR@) into
# ~/Library/LaunchAgents/. It does NOT load anything unless you pass --load;
# otherwise it prints the launchctl commands. No sudo, no system-wide changes.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: scripts/macos/install-launchd.sh [options]

  --repo DIR          repository path (default: two levels above this script)
  --with-dashboard    also install com.jobagent.dashboard.plist
  --load              load (start) the agent(s) now with launchctl bootstrap
  --force             replace existing plists that differ (a timestamped .bak copy is kept)
  --dry-run           print the rendered plists; write nothing
  -h, --help          show this help
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOAD=0
WITH_DASHBOARD=0
FORCE=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo) [[ $# -ge 2 ]] || { echo "--repo needs a directory" >&2; exit 2; }
                REPO_DIR="$(cd "$2" && pwd)"; shift 2 ;;
        --with-dashboard) WITH_DASHBOARD=1; shift ;;
        --load) LOAD=1; shift ;;
        --force) FORCE=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

case "$REPO_DIR" in
    *$'\n'*) echo "error: the repository path must not contain newlines" >&2; exit 2 ;;
esac
if [[ ! -x "$REPO_DIR/.venv/bin/job-agent" ]]; then
    echo "warning: $REPO_DIR/.venv/bin/job-agent is missing; create the virtualenv first (docs/setup.md)." >&2
fi

# XML-escape the path, then escape it for the sed replacement.
XML_PATH="$(printf '%s' "$REPO_DIR" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')"
SED_REPLACEMENT="$(printf '%s' "$XML_PATH" | sed -e 's/[&|\\]/\\&/g')"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LABELS=(com.jobagent.daemon)
if [[ $WITH_DASHBOARD -eq 1 ]]; then
    LABELS+=(com.jobagent.dashboard)
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

for label in "${LABELS[@]}"; do
    rendered="$TMP_DIR/$label.plist"
    # replace the placeholder outside the leading XML comment only
    sed -e "/^<plist/,\$s|@REPO_DIR@|${SED_REPLACEMENT}|g" "$SCRIPT_DIR/$label.plist" > "$rendered"
    if command -v plutil >/dev/null 2>&1; then
        plutil -lint "$rendered" >/dev/null
    fi
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "----- $AGENTS_DIR/$label.plist -----"
        cat "$rendered"
        continue
    fi
    mkdir -p "$AGENTS_DIR" "$REPO_DIR/logs"
    target="$AGENTS_DIR/$label.plist"
    if [[ -e "$target" ]] && ! cmp -s "$rendered" "$target"; then
        if [[ $FORCE -ne 1 ]]; then
            echo "error: $target already exists and differs; re-run with --force to replace it" >&2
            exit 1
        fi
        backup="$target.bak.$(date +%Y%m%d%H%M%S)"
        cp -p "$target" "$backup"
        echo "kept a copy of the previous plist: $backup"
    fi
    install -m 0644 "$rendered" "$target"
    echo "installed $target"
done

if [[ $DRY_RUN -eq 1 ]]; then
    exit 0
fi

DOMAIN="gui/$(id -u)"
if [[ $LOAD -eq 1 ]]; then
    for label in "${LABELS[@]}"; do
        if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
            echo "$label is already loaded; restart it with: launchctl kickstart -k $DOMAIN/$label"
        else
            launchctl bootstrap "$DOMAIN" "$AGENTS_DIR/$label.plist"
            echo "loaded $label"
        fi
    done
else
    echo
    echo "Nothing was loaded. To start now and at every login, run:"
    for label in "${LABELS[@]}"; do
        echo "  launchctl bootstrap $DOMAIN $AGENTS_DIR/$label.plist"
    done
fi
cat <<EOF

Useful commands:
  launchctl print $DOMAIN/com.jobagent.daemon | head -20   # state, last exit code
  launchctl kickstart -k $DOMAIN/com.jobagent.daemon       # restart
  launchctl bootout $DOMAIN/com.jobagent.daemon            # graceful stop + unload until next bootstrap
  tail -f "$REPO_DIR/logs/job_agent.log"                   # detailed log
To remove: bootout as above, then delete $AGENTS_DIR/com.jobagent.*.plist
EOF
