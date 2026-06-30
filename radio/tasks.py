"""Radio Celery tasks.

This module provides the periodic health-check task described in
``docs/architecture/radio/09_operational.md`` plus two retention
purge tasks added per
``prompts/p4-staff-engineer-review.md`` #2.

Tasks:

- :func:`check_all_stations_health` iterates all active
  stations, probes each one, persists the result, and updates
  the Prometheus metrics exported by ``radio.metrics``.

- :func:`purge_old_history` deletes ``ListeningHistory`` rows
  older than ``RADIO_HISTORY_RETENTION_DAYS`` (default 90) so
  the trending table does not grow without bound.

- :func:`purge_old_health_checks` keeps only the
  ``RADIO_HEALTH_CHECK_KEEP_PER_STATION`` newest rows per
  station; older rows are deleted. This preserves the
  ``last_reachable_at`` audit trail while keeping the table
  small.

Worker: Celery worker.
Schedule:

- ``CELERY_BEAT_SCHEDULE['radio-health-check']`` every 5 min.
- ``CELERY_BEAT_SCHEDULE['radio-purge-old-history']`` daily at
  05:15 UTC.
- ``CELERY_BEAT_SCHEDULE['radio-purge-old-health-checks']``
  daily at 05:45 UTC.

Auth: Celery task isolation; no request context.
"""

from __future__ import annotations

import logging
import subprocess
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db import DatabaseError, OperationalError
from django.utils import timezone

from radio.metrics import (
    radio_health_checks_last_run_timestamp,
    radio_station_health_failures_total,
    radio_station_health_latency_seconds,
    radio_station_health_successes_total,
    radio_stations_total,
)
from radio.models import ListeningHistory, Station, StationHealthCheck
from radio.services import (
    probe_all_active_stations,
    refresh_now_playing,
    rollup_station_analytics,
)

logger = logging.getLogger("radio")


def _health_check_timeout_seconds() -> float:
    """Read the per-probe timeout from settings with a sane default."""
    return float(getattr(settings, "RADIO_HEALTH_CHECK_TIMEOUT_SECONDS", 5.0))


@shared_task(
    bind=True,
    name="radio.health.check_all_stations",
    max_retries=0,
    time_limit=300,
    soft_time_limit=270,
)
def check_all_stations_health(self: Any) -> dict[str, Any]:
    """Probe every active station and update health metrics.

    Scheduled via Celery Beat every 5 minutes. The task:

    1. Iterates ``Station.objects.filter(is_active=True)``.
    2. Calls ``radio.services.probe_all_active_stations``.
    3. Increments per-station success / failure counters and observes
       probe latency in the ``radio_station_health_latency_seconds``
       histogram.
    4. Updates the ``radio_stations_total`` and
       ``radio_health_checks_last_run_timestamp`` gauges.

    Returns:
        Dict with ``checked``, ``reachable``, and ``unreachable``
        counts.
    """
    timeout = _health_check_timeout_seconds()
    start = time.monotonic()
    try:
        results = probe_all_active_stations(timeout_seconds=timeout)
    except Exception:
        logger.exception("radio_health_check_failed")
        raise

    reachable = sum(1 for r in results if r.is_reachable)
    unreachable = sum(1 for r in results if not r.is_reachable)
    duration = time.monotonic() - start

    for r in results:
        try:
            station = Station.objects.get(id=r.station_id)
            provider_slug = station.provider.slug
        except Station.DoesNotExist:
            provider_slug = "unknown"
        labels = {"station_id": r.station_id, "provider_slug": provider_slug}
        if r.is_reachable:
            radio_station_health_successes_total.labels(**labels).inc()
        else:
            radio_station_health_failures_total.labels(**labels).inc()
        latency_seconds = (
            r.response_time_ms / 1000.0
            if r.response_time_ms is not None
            else 0.0
        )
        radio_station_health_latency_seconds.labels(
            station_id=r.station_id,
            outcome="success" if r.is_reachable else "failure",
        ).observe(latency_seconds)

    radio_stations_total.set(Station.objects.filter(is_active=True).count())
    radio_health_checks_last_run_timestamp.set(timezone.now().timestamp())

    logger.info(
        "radio_health_check_completed checked=%d reachable=%d "
        "unreachable=%d duration_seconds=%.3f",
        len(results),
        reachable,
        unreachable,
        duration,
    )
    return {
        "checked": len(results),
        "reachable": reachable,
        "unreachable": unreachable,
        "duration_seconds": round(duration, 3),
    }


# --- Retention purge tasks -----------------------------------------------
# Per prompts/p4-staff-engineer-review.md #2 the listening-history
# and health-check tables grow unbounded. The purge tasks are
# scheduled daily by Celery Beat and obey environment-driven
# retention windows.


@shared_task(
    name="radio.tasks.purge_old_history",
    autoretry_for=(OperationalError, DatabaseError),
    retry_backoff=True,
    max_retries=2,
    time_limit=600,
)
def purge_old_history() -> dict[str, int]:
    """Delete ``ListeningHistory`` rows older than
    ``RADIO_HISTORY_RETENTION_DAYS`` (default 90).

    Returns ``{"deleted": <int>, "retention_days": <int>}``.
    A ``retention_days`` of 0 (or negative) is the opt-out
    switch and short-circuits with ``deleted=0``.
    """
    retention_days = int(getattr(settings, "RADIO_HISTORY_RETENTION_DAYS", 90))
    if retention_days <= 0:
        return {"deleted": 0, "retention_days": retention_days}
    cutoff = timezone.now() - timedelta(days=retention_days)
    deleted, _ = ListeningHistory.objects.filter(
        started_at__lt=cutoff
    ).delete()
    logger.info(
        "purge_old_history: deleted=%d cutoff=%s retention_days=%d",
        deleted,
        cutoff.isoformat(),
        retention_days,
    )
    return {"deleted": deleted, "retention_days": retention_days}


@shared_task(
    name="radio.tasks.purge_old_health_checks",
    autoretry_for=(OperationalError, DatabaseError),
    retry_backoff=True,
    max_retries=2,
    time_limit=600,
)
def purge_old_health_checks() -> dict[str, int]:
    """Keep only the ``RADIO_HEALTH_CHECK_KEEP_PER_STATION``
    newest rows per station; older rows are deleted.

    This preserves the "is the station currently reachable?"
    audit trail (driven by the most recent row) without
    letting the table accumulate one row per probe per
    station per day forever.

    Returns ``{"deleted": <int>, "keep_per_station": <int>}``.
    A ``keep_per_station`` of 0 (or negative) is the opt-out
    switch and short-circuits with ``deleted=0``.
    """
    keep = int(getattr(settings, "RADIO_HEALTH_CHECK_KEEP_PER_STATION", 20))
    if keep <= 0:
        return {"deleted": 0, "keep_per_station": keep}
    deleted = 0
    # For each station, find the Nth-newest ``checked_at`` and
    # delete every row older than that. The subquery is
    # necessary because Django does not expose a portable
    # ``ROW_NUMBER() OVER (PARTITION BY ...)`` helper.
    for station in Station.objects.all().only("id"):
        cutoff_row = (
            StationHealthCheck.objects.filter(station_id=station.id)
            .order_by("-checked_at")
            .values_list("checked_at", flat=True)[keep - 1 : keep]
        )
        if not cutoff_row:
            continue
        cutoff_ts = cutoff_row[0]
        n, _ = StationHealthCheck.objects.filter(
            station_id=station.id, checked_at__lt=cutoff_ts
        ).delete()
        deleted += n
    logger.info(
        "purge_old_health_checks: deleted=%d keep_per_station=%d",
        deleted,
        keep,
    )
    return {"deleted": deleted, "keep_per_station": keep}


# --- Phase 7 tasks ------------------------------------------------------


@shared_task(
    name="radio.tasks.rollup_station_analytics",
    autoretry_for=(OperationalError, DatabaseError),
    retry_backoff=True,
    max_retries=2,
    time_limit=600,
)
def rollup_station_analytics_task(
    lookback_days: int = 2,
) -> dict[str, int]:
    """Roll up :class:`ListeningHistory` into per-station daily
    :class:`StationAnalytics` rows.

    Scheduled to run just after midnight UTC (with a 2-day
    ``lookback_days`` so the just-finished day is re-aggregated
    after midnight) and again at noon UTC to catch late-arriving
    history rows.
    """
    return rollup_station_analytics(lookback_days=lookback_days)


@shared_task(
    name="radio.tasks.refresh_now_playing",
    autoretry_for=(OperationalError, DatabaseError),
    retry_backoff=True,
    max_retries=2,
    time_limit=300,
)
def refresh_now_playing_task(station_id: str | None = None) -> dict[str, int]:
    """Poll each active station's ``metadata_url`` and update its
    :class:`NowPlaying` row.

    Best-effort: a single failed fetch is logged and skipped; one
    broken station does not block the rest of the poll.
    """
    return refresh_now_playing(station_id=station_id)


# --- Opencode agent automation --------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
AGENT_SCRIPT = str(BASE_DIR / ".opencode" / "run-agent.sh")


@shared_task(
    bind=True,
    queue="agent_execution",
    time_limit=3600,
    soft_time_limit=3540,
    max_retries=0,
)
def run_opencode_agent_task(self: Any) -> dict[str, Any]:
    """Execute ``.opencode/run-agent.sh`` via subprocess.

    Designed for single-concurrency execution (queue ``agent_execution``
    must have ``-c 1``) to avoid git/S3 conflicts.

    Logs stdout/stderr, records exit code, and sends an email summary
    when SMTP is configured.

    Fallback: when Celery is down, ``scripts/agent-watchdog.sh``
    (installed via crontab by ``scripts/install-agent-watchdog.sh``)
    detects the missing worker ``pong`` and runs the agent directly.
    """
    logger.info("Starting opencode agent: %s", AGENT_SCRIPT)
    before = subprocess.run(
        ["/usr/bin/git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        cwd=BASE_DIR,
    ).stdout.strip()

    proc = subprocess.run(
        [AGENT_SCRIPT],
        capture_output=True,
        text=True,
        cwd=BASE_DIR,
        timeout=3500,
    )
    exit_code = proc.returncode
    stdout_log = proc.stdout
    stderr_log = proc.stderr

    after = subprocess.run(
        ["/usr/bin/git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        cwd=BASE_DIR,
    ).stdout.strip()

    report = (
        f"Opencode agent finished.\n"
        f"Exit code: {exit_code}\n"
        f"Before: {before}\n"
        f"After:  {after}\n\n"
        f"=== STDOUT ===\n{stdout_log}\n\n"
        f"=== STDERR ===\n{stderr_log}"
    )
    logger.info(
        "Opencode agent exit_code=%s before=%s after=%s",
        exit_code,
        before,
        after,
    )

    # Email notification
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None)
    if from_email:
        try:
            send_mail(
                subject="[Automation] Opencode agent finished",
                message=report,
                from_email=from_email,
                recipient_list=["rahimranxx8050@gmail.com"],
                fail_silently=False,
            )
        except Exception as exc:
            logger.warning("Failed to send agent report email: %s", exc)

    if exit_code != 0:
        raise RuntimeError(f"Opencode agent failed with exit code {exit_code}")

    return {"exit_code": exit_code, "before": before, "after": after}
