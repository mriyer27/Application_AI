#!/usr/bin/env bash
# Remove the job-agent systemd *user* units installed by install-systemd-user.sh.
# Stops them gracefully first (a running task finishes). Your data, config and
# logs are not touched.
set -euo pipefail

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
removed=0
for unit in job-agent.service job-agent-dashboard.service; do
    target="$UNIT_DIR/$unit"
    if [[ ! -e "$target" ]]; then
        echo "not installed: $unit"
        continue
    fi
    if ! systemctl --user disable --now "$unit"; then
        echo "warning: could not disable/stop $unit (continuing with removal)" >&2
    fi
    rm -f -- "$target"
    echo "removed $target"
    removed=1
done
if [[ $removed -eq 1 ]]; then
    systemctl --user daemon-reload
fi
