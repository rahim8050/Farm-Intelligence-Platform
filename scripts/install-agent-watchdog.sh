#!/usr/bin/env bash
# Install the Celery watchdog crontab entry.
# Runs agent-watchdog.sh at :15 past every hour (matches Celery Beat times).
set -euo pipefail

cd "$(dirname "$0")/.."
WATCHDOG="$(pwd)/scripts/agent-watchdog.sh"
chmod +x "$WATCHDOG"

CRON_LINE="15 10,16 * * * $WATCHDOG >> /tmp/opencode-watchdog.log 2>&1"

# Check if already installed
if crontab -l 2>/dev/null | grep -q "$WATCHDOG"; then
    echo "[install] Watchdog crontab already present."
else
    (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
    echo "[install] Watchdog crontab installed: $CRON_LINE"
fi

echo "[install] Done — current crontab:"
crontab -l
