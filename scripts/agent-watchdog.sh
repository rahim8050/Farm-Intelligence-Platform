#!/usr/bin/env bash
# Fallback watchdog: runs opencode agent via crontab when Celery is down.
# Installed by scripts/install-agent-watchdog.sh.
set -euo pipefail

cd /home/rahim/projects/Farm-Intelligence-Platform
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-$HOME/.local/share/fip-venv}"

# Simple pidfile lock to prevent concurrent runs
LOCK_FILE="/tmp/opencode-watchdog.pid"
if [ -f "$LOCK_FILE" ]; then
    OLD_PID=$(cat "$LOCK_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[watchdog] Already running (pid $OLD_PID) — skipping."
        exit 0
    fi
    echo "[watchdog] Stale lock found — removing."
fi
echo $$ > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

# Check if Celery worker is alive by capturing pong output directly
# (piping into grep triggers pipefail issues with celery inspect)
CELERY_ALIVE=false
CELERY_PONG=$("$UV_PROJECT_ENVIRONMENT/bin/celery" -A config inspect ping 2>/dev/null || true)
if echo "$CELERY_PONG" | grep -q "pong"; then
    CELERY_ALIVE=true
fi

if [ "$CELERY_ALIVE" = true ]; then
    echo "[watchdog] Celery worker is alive — nothing to do."
    exit 0
fi

echo "[watchdog] Celery worker unreachable — running agent directly."
"$UV_PROJECT_ENVIRONMENT/bin/python" "$HOME/projects/Farm-Intelligence-Platform/.opencode/run-agent.sh"
