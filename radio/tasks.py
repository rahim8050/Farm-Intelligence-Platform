"""Radio Celery tasks.

This module provides the periodic health-check task described in
``docs/architecture/radio/09_operational.md``. The task iterates all
active stations, probes each one, persists the result, and updates the
Prometheus metrics exported by ``radio.metrics``.

Worker: Celery worker.
Schedule: ``CELERY_BEAT_SCHEDULE['radio-health-check']`` (every 5 min).
Auth: Celery task isolation; no request context.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from radio.metrics import (
    radio_health_checks_last_run_timestamp,
    radio_station_health_failures_total,
    radio_station_health_latency_seconds,
    radio_station_health_successes_total,
    radio_stations_total,
)
from radio.models import Station
from radio.services import probe_all_active_stations

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
