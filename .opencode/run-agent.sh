#!/usr/bin/env bash
set -euo pipefail

export PATH="/home/rahim/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"

cd /home/rahim/projects/Farm-Intelligence-Platform

# Stash any pre-existing dirt so agent starts clean
git stash --include-untracked --quiet || true

AGENT="spectral-metrics"
PROMPT="Migrate Prometheus metrics from per-index metric families (ndvi_*, ndwi_*, ndmi_*) to the unified spectral_index_* pattern. The TODOs and definitions are in ndvi/metrics.py (lines 292-298). The old metric families need their consumers replaced with the unified versions that carry an index=\"NDVI\" label.

What to do:

1) Read ndvi/metrics.py to understand the old vs new metric signatures (labels differ — the new ones add an index label).

2) For each old metric that has consumers, replace with the spectral_* equivalent:
   - ndvi_upstream_requests_total → spectral_upstream_requests_total (add index=\"NDVI\")
   - ndvi_upstream_latency_seconds → spectral_upstream_latency_seconds (add index=\"NDVI\")
   - ndvi_jobs_total → spectral_jobs_total (add index=\"NDVI\")
   - ndvi_task_runtime_seconds → spectral_task_runtime_seconds (add index=\"NDVI\") — only remaining at ndvi/tasks.py:758
   - ndvi_farms_stale_total → spectral_farms_stale_total (add index=\"NDVI\") — ndvi/views.py:1024,1037
   - ndmi_cache_hit_ratio → add a new spectral_cache_hit_total counter in metrics.py with index and level labels, then update ndvi/cache.py consumers

3) For dead metrics defined but never incremented (ndvi_backfill_rows_total, ndmi_observations_ingested_total, ndmi_observations_null_total, ndmi_compute_duration_seconds, ndmi_job_duration_seconds), remove the definitions and update test assertions in ndvi/tests/test_ndmi_phase2.py that only check they exist.

4) Update Grafana dashboards under monitoring/grafana/dashboards/ that query old metric names. Replace each PromQL query with the equivalent spectral_* query (the metric name changes, but labels carry the same cardinality info).

5) Update ndvi/README.md metrics table to reference spectral_* names instead of old names.

6) Run these verification commands:
   - uv run ruff check ndvi/
   - uv run mypy ndvi/
   - uv run pytest ndvi/tests/test_no_regression.py -x -q
   - uv run pytest ndvi/tests/test_ndmi_phase2.py -x -q
   - uv run pytest ndvi/tests/test_ndwi.py -x -q
   - uv run pytest ndvi/tests/test_ndmi_views.py -x -q

IMPORTANT SAFETY RULES:
- Do NOT remove the old metric definitions in metrics.py yet — only replace the consumers. Removing definitions would break Prometheus metric registration.
- The old metric definitions stay but become unused — add a comment \"# DEPRECATED — use spectral_* instead\" above each.
- Keep all existing label values (engine, status, etc.) — just add the new index label where needed.
- Do NOT modify labelnames of the unified metrics (they already exist with the right labels).
- For spectral_upstream_requests_total and spectral_upstream_latency_seconds which are defined but have zero consumers — this is the migration that activates them.
- Read each file before editing it to see exact formatting/indentation."
TIMEOUT_SECONDS=$((60 * 60))

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
        subject=subject_prefix + 'Sentinel-1 docs agent finished',
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
