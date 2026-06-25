#!/usr/bin/env bash
set -euo pipefail

export PATH="/home/rahim/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"

cd /home/rahim/projects/Farm-Intelligence-Platform

STATE_FILE=".opencode/.phase"
FIRST=0
LAST=4

if [ $# -ge 1 ]; then
  # Explicit override — reset
  PHASE="$1"
  echo "$PHASE" > "$STATE_FILE"
elif [ -f "$STATE_FILE" ]; then
  PHASE=$(cat "$STATE_FILE")
  # Validate
  if ! [[ "$PHASE" =~ ^[0-9]+$ ]] || [ "$PHASE" -lt $FIRST ] || [ "$PHASE" -gt $LAST ]; then
    PHASE=$FIRST
  fi
else
  PHASE=$FIRST
  echo "$PHASE" > "$STATE_FILE"
fi

AGENT="ndmi-phase${PHASE}"

# Phase-specific prompts
PROMPT_0="Implement all Phase 0 tasks for NDMI as defined in .opencode/agents/ndmi-phase0.md. Start with 0.1 (formula registry) and work through 0.8 (URL prefix). Run ruff and tests after each task."
PROMPT_1="Implement all Phase 1 tasks for NDMI as defined in .opencode/agents/ndmi-phase1.md. Start with 1.1 (SpectralComputeEngine) and work through 1.6 (remove if-branches). Run ruff and tests after each task."
PROMPT_2="Implement all Phase 2 tasks for NDMI as defined in .opencode/agents/ndmi-phase2.md. Start with 2.1 (Celery Beat tasks) and work through 2.8 (quality thresholds). Run ruff and tests after each task."
PROMPT_3="Implement all Phase 3 tasks for NDMI as defined in .opencode/agents/ndmi-phase3.md. Start with 3.1 (multi-level caching) and work through 3.6 (provenance tracking). Run ruff and tests after each task."
PROMPT_4="Implement all Phase 4 tasks for NDMI as defined in .opencode/agents/ndmi-phase4.md. Only implement tasks whose trigger conditions are met. Run ruff and tests after each task."

PROMPT_VAR="PROMPT_${PHASE}"
PROMPT="${!PROMPT_VAR}"

LOG_FILE="/tmp/opencode-ndmi-phase${PHASE}-$(date +%Y%m%d-%H%M).log"
REPORT_FILE="/tmp/opencode-ndmi-report-phase${PHASE}-$(date +%Y%m%d-%H%M).txt"

echo "[$(date)] Starting NDMI Phase ${PHASE} agent..." | tee "$LOG_FILE"

# Capture git state before
BEFORE=$(git rev-parse HEAD)

# Run agent (allow failure — we capture exit code for reporting)
set +e
{
  opencode run \
    --agent "$AGENT" \
    --dangerously-skip-permissions \
    "$PROMPT"
} 2>&1 | tee -a "$LOG_FILE"
AGENT_EXIT=${PIPESTATUS[0]}
set -e

# Auto-advance to next phase on success (cap at LAST)
if [ "$AGENT_EXIT" -eq 0 ] && [ "$PHASE" -lt "$LAST" ]; then
  NEXT=$((PHASE + 1))
  echo "$NEXT" > "$STATE_FILE"
  echo "[$(date)] Phase ${PHASE} done → advancing to phase ${NEXT} for next run." | tee -a "$LOG_FILE"
fi

# Check what changed
AFTER=$(git rev-parse HEAD)
CHANGED_FILES=$(git diff --name-only "$BEFORE" 2>/dev/null || git status --short)
DIFF_STAT=$(git diff --stat "$BEFORE" 2>/dev/null || echo "(no changes)")

# Build summary
{
  echo "NDMI Phase ${PHASE} — Agent Report"
  echo "================================="
  echo "Date: $(date)"
  echo "Exit code: $AGENT_EXIT"
  echo ""
  echo "Files changed:"
  echo "$CHANGED_FILES"
  echo ""
  echo "Diff stat:"
  echo "$DIFF_STAT"
  echo ""
  echo "Log: $LOG_FILE"
} > "$REPORT_FILE"

# Send email notification with diagnostic output
cd /home/rahim/projects/Farm-Intelligence-Platform
.venv/bin/python3 -c "
import os, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
os.environ['DJANGO_ALLOWED_HOSTS'] = '*'
import django; django.setup()
from django.conf import settings
from django.core.mail import send_mail

# Check if email is actually configured
backend = getattr(settings, 'EMAIL_BACKEND', '')
if 'console' in backend or 'locmem' in backend:
    print('[email] Using non-sending backend: {} — skipping.'.format(backend))
    sys.exit(0)

has_host = bool(getattr(settings, 'EMAIL_HOST', ''))
has_from = bool(getattr(settings, 'DEFAULT_FROM_EMAIL', ''))
if not has_host or not has_from:
    print('[email] SMTP not configured (EMAIL_HOST={!r}, DEFAULT_FROM_EMAIL={!r}) — skipping.'.format(
        getattr(settings, 'EMAIL_HOST', None),
        getattr(settings, 'DEFAULT_FROM_EMAIL', None),
    ))
    sys.exit(0)

with open('$REPORT_FILE') as f:
    body = f.read()

try:
    sent = send_mail(
        subject='NDMI Phase ${PHASE} agent finished',
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=['rahimranxx8050@gmail.com'],
        fail_silently=False,
    )
    if sent:
        print('[email] Sent OK ({} recipient(s))'.format(sent))
    else:
        print('[email] send_mail returned 0 — not sent (check EMAIL_BACKEND)')
except Exception as exc:
    print('[email] Failed: {}'.format(exc))
" 2>&1 | tee -a "$LOG_FILE"

echo "[$(date)] Done." | tee -a "$LOG_FILE"
