"""Celery tasks for the `alerts` app.

Four tasks:

- :func:`scan_ndvi_declines` runs every
  ``ALERTS_NDVI_DECLINE_SCAN_INTERVAL_SECONDS`` and fires an
  ``NDVI_DECLINE`` alert for every farm whose latest state is
  ``DECLINE`` and that has not been alerted in the last 24h.
- :func:`scan_low_ndvi_observations` does the same for
  ``NDVI_LOW`` alerts (mean < ``ALERTS_NDVI_LOW_THRESHOLD``).
- :func:`render_alert_audio` is the asynchronous half of
  :func:`alerts.services.dispatch_alert`; it runs the TTS
  backend, saves the audio file, and pushes a second WebSocket
  event with the populated ``audio_url``.
- :func:`dispatch_one_alert` is the per-recipient task used by
  :func:`alerts.triggers.on_admin_broadcast` to fan out admin
  broadcasts via a ``celery.group`` (one task per recipient)
  so a single slow TTS render does not block the others.

The de-duplication key for the scans is the latest
:class:`AudioAlert.created_at` for the ``(user, farm, alert_type)``
triple; a second alert is not sent within the 24h window. This is
intentionally simple (a timestamp check, not a separate table)
because the alert stream is low-volume.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from uuid import UUID

from celery import shared_task
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import DatabaseError, OperationalError
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


@shared_task(
    name="alerts.tasks.render_alert_audio",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
    ignore_result=True,
)
def render_alert_audio(alert_id: str) -> dict[str, Any] | None:
    """Render the audio file for ``alert_id`` and re-push the WS event.

    Split out of :func:`alerts.services.dispatch_alert` (see
    ``prompts/p4-staff-engineer-review.md`` #6) so the HTTP / trigger
    path stays sub-second and a slow TTS backend never blocks the
    user-facing row insert. The task is idempotent: if the alert
    already has an audio file it short-circuits to a no-op (Celery
    at-least-once delivery means the same alert id can be enqueued
    twice).
    """
    from alerts import metrics, services
    from alerts.tts import synthesize

    close_old_connections_safe()
    try:
        alert = AudioAlert.objects.select_related("user").get(
            id=UUID(alert_id)
        )
    except AudioAlert.DoesNotExist:
        logger.warning("render_alert_audio: alert %s not found", alert_id)
        return None
    if alert.audio_file:
        # Already rendered (idempotent re-delivery).
        return {"alert_id": alert_id, "status": "already_rendered"}
    engine = (getattr(settings, "TTS_ENGINE", "espeak") or "espeak").lower()
    started = timezone.now()
    try:
        result = synthesize(alert.message)
    except Exception as exc:  # noqa: BLE001
        metrics.render_failures(engine=engine, reason=exc.__class__.__name__)
        logger.warning(
            "render_alert_audio: TTS failed for %s: %s",
            alert_id,
            exc.__class__.__name__,
        )
        raise
    if result.audio_bytes:
        alert.audio_file.save(
            f"{alert.id}.wav", ContentFile(result.audio_bytes), save=False
        )
    alert.duration_ms = result.duration_ms
    alert.mime_type = result.mime_type
    alert.save(update_fields=["audio_file", "duration_ms", "mime_type"])
    # Second push: deliver the populated audio_url.
    payload = services.build_push_payload(alert)
    services.emit_audio_alert_event(alert.user_id, payload)
    logger.info(
        "render_alert_audio: alert=%s engine=%s bytes=%d duration_ms=%d "
        "elapsed_ms=%d",
        alert.id,
        engine,
        len(result.audio_bytes),
        result.duration_ms,
        int((timezone.now() - started).total_seconds() * 1000),
    )
    return {
        "alert_id": alert_id,
        "status": "rendered",
        "engine": engine,
        "bytes": len(result.audio_bytes),
    }


@shared_task(
    name="alerts.tasks.dispatch_one_alert",
    autoretry_for=(
        OperationalError,
        DatabaseError,
        ConnectionError,
        TimeoutError,
    ),
    dont_autoretry_for=(ValueError,),
    retry_backoff=True,
    retry_backoff_max=30,
    retry_jitter=True,
    max_retries=2,
    ignore_result=True,
)
def dispatch_one_alert(
    *,
    user_id: int,
    farm_id: int | None,
    alert_type: str,
    trigger_source: str,
    title: str,
    message: str,
    source_object_id: str = "",
) -> dict[str, Any]:
    """Dispatch a single audio alert for one user.

    Used by :func:`alerts.triggers.on_admin_broadcast` (and
    available to any other trigger that wants to fan out a
    large list of recipients) to push one Celery task per user
    via ``celery.group``. Each task self-heals on transient
    infrastructure failures (``OperationalError``,
    ``ConnectionError``, ``TimeoutError``) via ``autoretry_for``;
    ``ValueError`` (unknown ``alert_type`` / ``trigger_source``)
    is explicitly excluded via ``dont_autoretry_for`` because
    it indicates a programming error.
    """
    from alerts.services import dispatch_alert

    close_old_connections_safe()
    result = dispatch_alert(
        user_id=user_id,
        farm_id=farm_id,
        alert_type=alert_type,
        trigger_source=trigger_source,
        title=title,
        message=message,
        source_object_id=source_object_id,
    )
    return {
        "user_id": user_id,
        "alert_id": str(result.alert_id),
        "delivered": result.delivered,
    }
