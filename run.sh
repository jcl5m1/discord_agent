#!/usr/bin/env bash
# Start the Discord agent harness.
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python -m harness.bot
