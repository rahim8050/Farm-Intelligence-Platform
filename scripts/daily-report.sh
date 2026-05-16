#!/bin/bash
# Daily report generator for weather-apis (Django backend)
# Usage: ./scripts/daily-report.sh [date]  (defaults to today)

set -euo pipefail

DATE="${1:-$(date +%Y-%m-%d)}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPORT_DIR="${REPO_DIR}/reports"
mkdir -p "${REPORT_DIR}"

REPORT_FILE="${REPORT_DIR}/daily-${DATE}.md"

echo "# Daily Report — weather-apis Django (${DATE})" > "${REPORT_FILE}"
echo "" >> "${REPORT_FILE}"
echo "Generated: $(date '+%Y-%m-%d %H:%M:%S %Z')" >> "${REPORT_FILE}"
echo "" >> "${REPORT_FILE}"

cd "${REPO_DIR}"

echo "## Commits" >> "${REPORT_FILE}"
echo "" >> "${REPORT_FILE}"
git log --since="${DATE}T00:00:00" --until="${DATE}T23:59:59" --pretty=format:"- %h %s (%an, %cr)" --no-merges >> "${REPORT_FILE}" 2>/dev/null || echo "_No commits_" >> "${REPORT_FILE}"
echo "" >> "${REPORT_FILE}"
echo "" >> "${REPORT_FILE}"

echo "## Pre-commit / Lint Results" >> "${REPORT_FILE}"
echo "" >> "${REPORT_FILE}"
echo '```' >> "${REPORT_FILE}"
pre-commit run --all-files >> "${REPORT_FILE}" 2>&1 || true
echo '```' >> "${REPORT_FILE}"
echo "" >> "${REPORT_FILE}"

echo "## Test Summary" >> "${REPORT_FILE}"
echo "" >> "${REPORT_FILE}"
echo '```' >> "${REPORT_FILE}"
.venv/bin/python manage.py test radio --verbosity=2 2>&1 | tail -10 >> "${REPORT_FILE}" || true
echo '```' >> "${REPORT_FILE}"
echo "" >> "${REPORT_FILE}"

echo "## File Changes" >> "${REPORT_FILE}"
echo "" >> "${REPORT_FILE}"
git diff --stat --since="${DATE}T00:00:00" --until="${DATE}T23:59:59" >> "${REPORT_FILE}" 2>/dev/null || echo "_No changes_" >> "${REPORT_FILE}"
echo "" >> "${REPORT_FILE}"

echo "Report saved to: ${REPORT_FILE}"
cat "${REPORT_FILE}"
