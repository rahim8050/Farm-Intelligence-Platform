#!/usr/bin/env bash
set -euo pipefail

export PATH="/home/rahim/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"

cd /home/rahim/projects/Farm-Intelligence-Platform

# Stash any pre-existing dirt so agent starts clean
git stash --include-untracked --quiet || true

AGENT="bugfix-radio"
TIMEOUT_SECONDS=$((60 * 60))
PROMPT="Fix the 6 bugs listed in prompts/radio-app-bug-audit.md (High + Medium severity). Before fixing each bug, first check if it has already been resolved by reading the relevant lines of code — if the issue pattern no longer exists, skip it and move on. This ensures idempotent runs.

Read prompts/radio-app-bug-audit.md first for full descriptions. The bugs to check and fix:

1) Race condition in FavoriteListCreateView.post — radio/views.py around line 865. Check if Station.objects.get() is still called after serializer validation. If so, replace with filter().first() + NotFound fallback.

2) Hardcoded email in run_opencode_agent_task — radio/tasks.py around line 352. Check if \"rahimranxx8050@gmail.com\" still appears. If so, replace with settings.AGENT_NOTIFICATION_EMAIL (add the setting to config/settings.py with a default of empty string, and conditionally skip email if unset).

3) 200 vs 204 in FavoriteDeleteView.delete — radio/views.py around line 923. Check if success_response is used (returns 200) but the docstring says 204. If so, add status_code=status.HTTP_204_NO_CONTENT and remove the response body (204 has no body).

4) Missing request_id in StationStreamView error — radio/views.py lines 390-401. Check if the hand-crafted Response dict is missing request_id. If so, replace with a call to error_response() from config.api.responses, or add request_id to the dict using the project's request_id helper.

5) total_duration_seconds never populated — radio/services.py around line 775. Check if StationAnalytics rollup sets total_duration_seconds in the defaults dict. If not, add Sum('duration_seconds') to the annotation and include it in the defaults.

6) Broken pagination in ListeningHistoryRecentView — radio/views.py lines 1045-1057. Check if the queryset is pre-sliced with [:limit]. If so, remove the slice and let paginated_response handle the limit via its page_size parameter.

Verification: after all fixes, run:
   uv run ruff check radio/
   uv run python manage.py test radio.tests.test_favorites radio.tests.test_health radio.tests.test_services --verbosity=1

IMPORTANT RULES:
- Read each file before editing to see exact formatting and indent style.
- Fix each bug at most once. If you already fixed Bug 1 but the run errors out before completing, check each bug site first before re-fixing.
- Do NOT modify files outside radio/ (except config/settings.py for Bug 2).
- Keep the same code style as the surrounding code."

LOG_FILE="/tmp/opencode-${AGENT}-$(date +%Y%m%d-%H%M).log"
REPORT_FILE="/tmp/opencode-report-${AGENT}-$(date +%Y%m%d-%H%M).txt"

echo "[$(date)] Starting ${AGENT} agent (timeout: ${TIMEOUT_SECONDS}s)..." | tee "$LOG_FILE"

BEFORE=$(git rev-parse HEAD)

set +e
{
  timeout "$TIMEOUT_SECONDS" \
    opencode run \
      --agent "$AGENT" \
      --dangerously-skip-permissions \
      "$PROMPT"
} 2>&1 | stdbuf -oL tee -a "$LOG_FILE"
AGENT_EXIT=${PIPESTATUS[0]}
set -e

TIMED_OUT=false
if [ "$AGENT_EXIT" -eq 124 ]; then
  TIMED_OUT=true
  echo "[$(date)] WARNING: ${AGENT} agent timed out after ${TIMEOUT_SECONDS}s." | tee -a "$LOG_FILE"
elif [ "$AGENT_EXIT" -ne 0 ]; then
  echo "[$(date)] WARNING: ${AGENT} agent exited with code ${AGENT_EXIT}." | tee -a "$LOG_FILE"
fi

AFTER=$(git rev-parse HEAD)
CHANGED_FILES=$(git diff --name-only "$BEFORE" 2>/dev/null || git status --short)
DIFF_STAT=$(git diff --stat "$BEFORE" 2>/dev/null || echo "(no changes)")

{
  echo "${AGENT} — Agent Report"
  echo "========================"
  echo "Date: $(date)"
  echo "Exit code: $AGENT_EXIT"
  if [ "$TIMED_OUT" = true ]; then
    echo "Status: TIMED OUT after ${TIMEOUT_SECONDS}s"
  fi
  echo ""
  echo "Files changed:"
  echo "$CHANGED_FILES"
  echo ""
  echo "Diff stat:"
  echo "$DIFF_STAT"
  echo ""
  echo "Log: $LOG_FILE"
} > "$REPORT_FILE"

cd /home/rahim/projects/Farm-Intelligence-Platform
~/.local/share/fip-venv/bin/python -c "
import os, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
os.environ['DJANGO_ALLOWED_HOSTS'] = '*'
import django; django.setup()
from django.conf import settings
from django.core.mail import send_mail

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

timed_out = '$TIMED_OUT' == 'true'
subject_prefix = '[TIMEOUT] ' if timed_out else ''
try:
    sent = send_mail(
        subject=subject_prefix + AGENT + ' agent finished',
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
