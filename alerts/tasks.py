"""Celery tasks for the `alerts` app.

Two periodic scans:

- :func:`scan_ndvi_declines` runs every
  ``ALERTS_NDVI_DECLINE_SCAN_INTERVAL_SECONDS`` and fires an
  ``NDVI_DECLINE`` alert for every farm whose latest state is
  ``DECLINE`` and that has not been alerted in the last 24h.
- :func:`scan_low_ndvi_observations` does the same for
  ``NDVI_LOW`` alerts (mean < ``ALERTS_NDVI_LOW_THRESHOLD``).

The de-duplication key is the latest :class:`AudioAlert.created_at`
for the ``(user, farm, alert_type)`` triple; a second alert is not
sent within the 24h window. This is intentionally simple (a
timestamp check, not a separate table) because the alert stream is
low-volume.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from celery import shared_task
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from alerts.models import AudioAlert, AudioAlertType

logger = logging.getLogger("alerts.tasks")

_DEDUPE_WINDOW = timedelta(hours=24)


@shared_task(name="alerts.tasks.scan_ndvi_declines", ignore_result=True)
def scan_ndvi_declines() -> dict[str, int]:
    """Find farms in ``DECLINE`` state and fire one alert per farm.

    Returns a small dict for visibility in Celery results.
    """
    from farms.models import Farm
    from ndvi.farm_state import build_farm_state

    cutoff = timezone.now() - _DEDUPE_WINDOW
    dispatched = 0
    farms_scanned = 0
    close_old_connections_safe()
    for farm in Farm.objects.filter(is_active=True).iterator():
        farms_scanned += 1
        try:
            result = build_farm_state(farm=farm)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "build_farm_state failed for farm %s: %s",
                farm.id,
                exc.__class__.__name__,
            )
            continue
        if result.state != "decline":
            continue
        if _recent_alert_exists(
            user_id=farm.owner_id,
            farm_id=farm.id,
            alert_type=AudioAlertType.NDVI_DECLINE,
            cutoff=cutoff,
        ):
            continue
        try:
            from alerts.triggers import on_ndvi_decline

            on_ndvi_decline(
                farm_id=farm.id,
                owner_id=farm.owner_id,
                message=(
                    f"NDVI decline detected on farm "
                    f"{farm.name}. Please review your fields."
                ),
            )
            dispatched += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "decline alert dispatch failed for farm %s: %s",
                farm.id,
                exc.__class__.__name__,
            )
    return {"farms_scanned": farms_scanned, "dispatched": dispatched}


@shared_task(
    name="alerts.tasks.scan_low_ndvi_observations", ignore_result=True
)
def scan_low_ndvi_observations() -> dict[str, int]:
    """Find farms whose latest NDVI mean < ``ALERTS_NDVI_LOW_THRESHOLD``.

    Threshold and dedupe window are read from settings; the
    de-duplication is per (user, farm, alert_type).
    """
    from farms.models import Farm
    from ndvi.models import NdviObservation

    threshold = float(getattr(settings, "ALERTS_NDVI_LOW_THRESHOLD", 0.2))
    cutoff = timezone.now() - _DEDUPE_WINDOW
    dispatched = 0
    farms_scanned = 0
    close_old_connections_safe()
    farms = Farm.objects.filter(is_active=True)
    for farm in farms.iterator():
        farms_scanned += 1
        latest = (
            NdviObservation.objects.valid()
            .filter(farm=farm)
            .order_by("-observation_date")
            .first()
        )
        if latest is None or latest.mean is None:
            continue
        if latest.mean >= threshold:
            continue
        if _recent_alert_exists(
            user_id=farm.owner_id,
            farm_id=farm.id,
            alert_type=AudioAlertType.NDVI_LOW,
            cutoff=cutoff,
        ):
            continue
        try:
            from alerts.triggers import on_ndvi_low

            on_ndvi_low(
                farm_id=farm.id,
                owner_id=farm.owner_id,
                message=(
                    f"Low NDVI on farm {farm.name}: "
                    f"mean {latest.mean:.2f} (threshold {threshold:.2f})."
                ),
                source_object_id=str(latest.id),
            )
            dispatched += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "low-ndvi alert dispatch failed for farm %s: %s",
                farm.id,
                exc.__class__.__name__,
            )
    return {
        "farms_scanned": farms_scanned,
        "dispatched": dispatched,
        "threshold": int(threshold * 1000),
    }


def _recent_alert_exists(
    *, user_id: int, farm_id: int, alert_type: str, cutoff: Any
) -> bool:
    return (
        AudioAlert.objects.filter(
            user_id=user_id,
            farm_id=farm_id,
            alert_type=alert_type,
            created_at__gte=cutoff,
        )
        .filter(Q(is_acknowledged=False) | Q(is_acknowledged=True))
        .exists()
    )


def close_old_connections_safe() -> None:
    from django.db import close_old_connections

    close_old_connections()
