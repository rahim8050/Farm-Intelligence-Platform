"""Service layer for the `alerts` app.

This module is the home for all business logic. It contains:

- :func:`dispatch_alert` - the single entry point that creates an
  :class:`AudioAlert` row, generates the audio file (via
  :mod:`alerts.tts`), and pushes a WebSocket event to the user's
  Channels group.
- :func:`acknowledge_alert` - mark one alert as read.
- :func:`list_alerts_for_user` - paginated list of the user's alerts.
- :func:`has_subscription` - quick check used by the periodic scan
  tasks to filter farms before building alert payloads.
- :func:`emit_audio_alert_event` - the WebSocket push primitive.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import close_old_connections
from django.utils import timezone

from alerts.models import (
    AudioAlert,
    AudioAlertSubscription,
    AudioAlertTriggerSource,
    AudioAlertType,
)
from alerts.tts import synthesize

logger = logging.getLogger("alerts.services")


@dataclass(frozen=True)
class AlertDispatchResult:
    """Return value of :func:`dispatch_alert`."""

    alert_id: UUID
    delivered: bool


def group_name_for_user(user_id: int) -> str:
    """Return the Channels group name for a user's audio alerts."""
    prefix = getattr(settings, "ALERTS_WEBHOOK_GROUP_PREFIX", "alerts_user_")
    return f"{prefix}{user_id}"


def has_subscription(*, user_id: int, farm_id: int, alert_type: str) -> bool:
    """True if the user has a subscription for this farm/type.

    The ``alert_types`` JSON field is filtered in Python because the
    test backend (SQLite) does not support ``__contains`` on JSON
    fields, and because the per-farm subscription set is small
    (typically one row per user-farm pair).
    """
    sub = (
        AudioAlertSubscription.objects.filter(user_id=user_id, farm_id=farm_id)
        .values_list("alert_types", flat=True)
        .first()
    )
    if not sub:
        return False
    return alert_type in sub


def subscribed_users_for_farm(
    *, farm_id: int, alert_type: str
) -> list[AudioAlertSubscription]:
    """Users subscribed to ``alert_type`` for ``farm_id``.

    Returns a list (not a QuerySet) because the ``alert_types`` JSON
    field is filtered in Python (see :func:`has_subscription`).
    """
    rows = list(
        AudioAlertSubscription.objects.filter(farm_id=farm_id).select_related(
            "user"
        )
    )
    return [s for s in rows if alert_type in (s.alert_types or [])]


def emit_audio_alert_event(user_id: int, payload: dict[str, Any]) -> int:
    """Push ``payload`` to the user's Channels group.

    Returns 1 on a successful send, 0 if no channel layer is configured
    (e.g. during tests with ``LocMemChannelLayer`` disabled).
    """
    layer = get_channel_layer()
    if layer is None:
        return 0
    group = group_name_for_user(user_id)
    async_to_sync(layer.group_send)(  # type: ignore[arg-type]
        group, {"type": "audio.alert", "payload": payload}
    )
    return 1


def _save_audio_bytes(alert: AudioAlert, audio_bytes: bytes) -> None:
    if not audio_bytes:
        return
    name = f"{alert.id}.wav"
    alert.audio_file.save(name, ContentFile(audio_bytes), save=False)


def dispatch_alert(
    *,
    user_id: int,
    farm_id: int | None,
    alert_type: str,
    trigger_source: str,
    title: str,
    message: str,
    source_object_id: str = "",
) -> AlertDispatchResult:
    """Create, synthesise, persist, and push a single audio alert.

    The DB write and the WebSocket push are best-effort: a failure in
    the push path never blocks the row from being saved, and vice
    versa. The caller (a Celery task) is responsible for any retries.
    """
    if alert_type not in AudioAlertType.values:
        raise ValueError(f"unknown alert_type: {alert_type!r}")
    if trigger_source not in AudioAlertTriggerSource.values:
        raise ValueError(f"unknown trigger_source: {trigger_source!r}")
    close_old_connections()
    alert = AudioAlert.objects.create(
        user_id=user_id,
        farm_id=farm_id,
        alert_type=alert_type,
        trigger_source=trigger_source,
        title=title[:200],
        message=message,
        source_object_id=source_object_id[:64],
    )
    try:
        result = synthesize(message)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "TTS failed for alert %s: %s", alert.id, exc.__class__.__name__
        )
        result = None
    if result is not None and result.audio_bytes:
        _save_audio_bytes(alert, result.audio_bytes)
        alert.duration_ms = result.duration_ms
        alert.mime_type = result.mime_type
        alert.save(update_fields=["audio_file", "duration_ms", "mime_type"])
    delivered = False
    try:
        sent = emit_audio_alert_event(
            user_id,
            {
                "alert_id": str(alert.id),
                "alert_type": alert.alert_type,
                "title": alert.title,
                "message": alert.message,
                "farm_id": alert.farm_id,
                "audio_url": _absolute_audio_url(alert),
                "duration_ms": alert.duration_ms,
                "mime_type": alert.mime_type,
                "created_at": alert.created_at.isoformat(),
                "schema_version": "1.0",
            },
        )
        if sent:
            alert.is_delivered = True
            alert.delivered_at = timezone.now()
            alert.save(update_fields=["is_delivered", "delivered_at"])
            delivered = True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "WebSocket push failed for alert %s: %s",
            alert.id,
            exc.__class__.__name__,
        )
    return AlertDispatchResult(alert_id=alert.id, delivered=delivered)


def acknowledge_alert(*, user_id: int, alert_id: UUID) -> bool:
    """Mark one of the user's alerts as acknowledged. Idempotent."""
    close_old_connections()
    updated = AudioAlert.objects.filter(
        user_id=user_id, id=alert_id, is_acknowledged=False
    ).update(is_acknowledged=True, acknowledged_at=timezone.now())
    return bool(updated)


def list_alerts_for_user(
    *,
    user_id: int,
    only_unacknowledged: bool = False,
    limit: int = 100,
) -> list[AudioAlert]:
    """Return the user's alerts (newest first)."""
    qs = AudioAlert.objects.filter(user_id=user_id)
    if only_unacknowledged:
        qs = qs.filter(is_acknowledged=False)
    return list(qs.order_by("-created_at")[: max(1, min(limit, 500))])


def _absolute_audio_url(alert: AudioAlert) -> str:
    """Best-effort absolute URL for the audio file (empty if no file)."""
    if not alert.audio_file:
        return ""
    try:
        return alert.audio_file.url
    except ValueError:
        return ""
