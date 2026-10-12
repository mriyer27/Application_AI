#!/usr/bin/env bash
# Install the job-agent systemd *user* units (Linux) for the current user.
#
# Copies scripts/systemd/*.service into ~/.config/systemd/user/ after replacing
# the "%h/Application_AI" placeholder with this repository's path. It does NOT
# enable or start anything unless you pass --enable; otherwise it prints the
# systemctl commands to run. No sudo, no system-wide changes.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: scripts/install-systemd-user.sh [options]

  --repo DIR          repository path (default: the directory containing scripts/)
  --with-dashboard    also install job-agent-dashboard.service
  --enable            run: systemctl --user daemon-reload && systemctl --user enable --now <units>
  --force             replace existing unit files that differ (a timestamped .bak copy is kept)
  --dry-run           print the rendered unit files; write nothing
  -h, --help          show this help
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENABLE=0
WITH_DASHBOARD=0
FORCE=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo) [[ $# -ge 2 ]] || { echo "--repo needs a directory" >&2; exit 2; }
                REPO_DIR="$(cd "$2" && pwd)"; shift 2 ;;
        --with-dashboard) WITH_DASHBOARD=1; shift ;;
        --enable) ENABLE=1; shift ;;
        --force) FORCE=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

case "$REPO_DIR" in
    *$'\n'*|*'"'*|*'\'*)
        echo "error: the repository path must not contain newlines, double quotes or backslashes: $REPO_DIR" >&2
        exit 2 ;;
esac

EXE="$REPO_DIR/.venv/bin/job-agent"
if [[ ! -x "$EXE" ]]; then
    echo "warning: $EXE is missing or not executable; create the virtualenv first (docs/setup.md)." >&2
fi
if [[ ! -f "$REPO_DIR/config/config.yaml" ]]; then
    echo "warning: $REPO_DIR/config/config.yaml not found; run 'job-agent init' first." >&2
fi

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
# systemd treats % as a specifier prefix (escape as %%); then escape for the sed replacement.
SYSTEMD_PATH="${REPO_DIR//%/%%}"
SED_REPLACEMENT="$(printf '%s' "$SYSTEMD_PATH" | sed -e 's/[&|]/\\&/g')"

UNITS=(job-agent.service)
if [[ $WITH_DASHBOARD -eq 1 ]]; then
    UNITS+=(job-agent-dashboard.service)
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

for unit in "${UNITS[@]}"; do
    src="$SCRIPT_DIR/systemd/$unit"
    rendered="$TMP_DIR/$unit"
    # replace the placeholder on non-comment lines only
    sed -e "/^[[:space:]]*#/!s|%h/Application_AI|${SED_REPLACEMENT}|g" "$src" > "$rendered"
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "----- $UNIT_DIR/$unit -----"
        cat "$rendered"
        continue
    fi
    target="$UNIT_DIR/$unit"
    mkdir -p "$UNIT_DIR"
    if [[ -e "$target" ]] && ! cmp -s "$rendered" "$target"; then
        if [[ $FORCE -ne 1 ]]; then
            echo "error: $target already exists and differs; re-run with --force to replace it" >&2
            exit 1
        fi
        backup="$target.bak.$(date +%Y%m%d%H%M%S)"
        cp -p "$target" "$backup"
        echo "kept a copy of the previous unit: $backup"
    fi
    install -m 0644 "$rendered" "$target"
    echo "installed $target"
done

if [[ $DRY_RUN -eq 1 ]]; then
    exit 0
fi

if [[ $ENABLE -eq 1 ]]; then
    systemctl --user daemon-reload
    systemctl --user enable --now "${UNITS[@]}"
    systemctl --user --no-pager status "${UNITS[@]}" || true  # status exits non-zero while starting
else
    echo
    echo "Nothing was enabled. To start now and at every login, run:"
    echo "  systemctl --user daemon-reload"
    echo "  systemctl --user enable --now ${UNITS[*]}"
fi
cat <<EOF

Useful commands:
  systemctl --user status job-agent          # is it running?
  journalctl --user -u job-agent -f          # console output (detailed log: $REPO_DIR/logs/job_agent.log)
  systemctl --user stop job-agent            # graceful stop (a running task finishes first)
  loginctl enable-linger "\$USER"             # optional: keep running while logged out
  scripts/uninstall-systemd-user.sh          # remove the units again
EOF
