#!/usr/bin/env bash
# Remove the Celery watchdog crontab entry.
set -euo pipefail

WATCHDOG="$(cd "$(dirname "$0")/.." && pwd)/scripts/agent-watchdog.sh"

crontab -l 2>/dev/null | grep -v "$WATCHDOG" | crontab -
echo "[remove] Watchdog crontab removed (if it existed)."
