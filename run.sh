#!/usr/bin/env bash
# Start the Discord agent harness and its watchdog.
# The watchdog runs as a separate process so it can alert in Discord even when
# the harness itself has crashed; it exits when the harness does.
set -euo pipefail
cd "$(dirname "$0")"
.venv/bin/python -m harness.watchdog &
watchdog_pid=$!
trap 'kill "$watchdog_pid" 2>/dev/null || true' EXIT
.venv/bin/python -m harness.bot
