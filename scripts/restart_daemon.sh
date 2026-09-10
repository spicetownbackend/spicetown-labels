#!/bin/bash
# scripts/restart_daemon.sh — safely restart the com.spicetown.labels LaunchDaemon.
#
# Why this exists: `sudo launchctl bootout system/<label>` followed
# immediately by `sudo launchctl bootstrap system <plist>` is a common
# recipe online, but pasting both commands together races launchd —
# bootout returns to the shell before launchd has actually finished tearing
# the old job down, so the very next bootstrap call hits it mid-teardown and
# fails with "Bootstrap failed: 5: Input/output error". This isn't a config
# problem; it's a timing race, and it's flaky rather than deterministic,
# which is why it sometimes "just works" and sometimes doesn't.
#
# This script closes the race by polling `launchctl print` until the job is
# actually gone (or a short timeout elapses) before bootstrapping it back in.
#
# Usage: sudo bash scripts/restart_daemon.sh
set -euo pipefail

LABEL="com.spicetown.labels"
PLIST="/Library/LaunchDaemons/com.spicetown.labels.plist"

if [[ $EUID -ne 0 ]]; then
    echo "Must run as root (sudo bash scripts/restart_daemon.sh)" >&2
    exit 1
fi

echo "Stopping ${LABEL}..."
launchctl bootout "system/${LABEL}" 2>/dev/null || true

echo "Waiting for launchd to finish tearing it down..."
for _ in $(seq 1 30); do
    if ! launchctl print "system/${LABEL}" >/dev/null 2>&1; then
        break
    fi
    sleep 0.5
done
# Give launchd's internal bookkeeping a moment to settle even after the job
# stops being visible to `print` — this small buffer is what actually closes
# the race in practice.
sleep 1

echo "Starting ${LABEL}..."
launchctl bootstrap system "${PLIST}"
launchctl kickstart -k "system/${LABEL}"

echo "Done. Status:"
launchctl print "system/${LABEL}" | grep -E "state|pid" || true
