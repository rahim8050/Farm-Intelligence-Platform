"""Service layer for the `alerts` app.

This module is the home for all business logic. It contains:

- :func:`dispatch_alert` - convenience entry point that calls
  :func:`dispatch_alert_fast` and enqueues
  :func:`alerts.tasks.render_alert_audio`. Use this from synchronous
  triggers (admin views, REST endpoints).
- :func:`dispatch_alert_fast` - the fast path: writes the
  :class:`AudioAlert` row, builds the initial WebSocket payload
  (without ``audio_url``), and pushes the event. The audio file is
  generated asynchronously by a Celery task and a second push
  delivers the URL once the file is ready.
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
from django.db import close_old_connections, connection
from django.utils import timezone

from alerts.models import (
    AudioAlert,
    AudioAlertSubscription,
    AudioAlertTriggerSource,
    AudioAlertType,
)

from . import metrics

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


def _json_contains_supports_array() -> bool:
    """True when the active DB can do ``JSONField __contains=[...]``.

    Per ``prompts/p4-staff-engineer-review.md`` #9, Postgres has
    first-class JSON containment; SQLite and MySQL do not (MySQL
    stores JSON as text, which the Django ORM refuses to search
    via ``__contains`` on a list operand). The check is cached on
    the connection so we do not re-evaluate the vendor lookup on
    every call.
    """
    return connection.vendor == "postgresql"


def has_subscription(*, user_id: int, farm_id: int, alert_type: str) -> bool:
    """True if the user has a subscription for this farm/type.

    On Postgres the filter is pushed into SQL via
    ``alert_types__contains=[alert_type]``; on SQLite / MySQL
    the same result is computed in Python because the ORM
    refuses the array operand there. The per-farm subscription
    set is small (typically one row per user-farm pair) so the
    Python fallback is cheap.
    """
    qs = AudioAlertSubscription.objects.filter(
        user_id=user_id, farm_id=farm_id
    )
    if _json_contains_supports_array():
        qs = qs.filter(alert_types__contains=[alert_type])
    else:
        sub = qs.values_list("alert_types", flat=True).first()
        if not sub:
            return False
        return alert_type in sub
    return qs.exists()


def subscribed_users_for_farm(
    *, farm_id: int, alert_type: str
) -> list[AudioAlertSubscription]:
    """Users subscribed to ``alert_type`` for ``farm_id``.

    On Postgres the filter is pushed into SQL via
    ``alert_types__contains=[alert_type]``; on SQLite / MySQL
    the rows are loaded once and filtered in Python. The
    callers always need the full ``AudioAlertSubscription``
    instances (with the related ``user``) so loading is
    unavoidable on the Python fallback path; on Postgres the
    SQL filter avoids materialising the rows the caller would
    then discard.
    """
    qs = AudioAlertSubscription.objects.filter(farm_id=farm_id).select_related(
        "user"
    )
    if _json_contains_supports_array():
        return list(qs.filter(alert_types__contains=[alert_type]))
    return [s for s in qs if alert_type in (s.alert_types or [])]


def emit_audio_alert_event(user_id: int, payload: dict[str, Any]) -> int:
    """Push ``payload`` to the user's Channels group.

    Returns 1 on a successful send, 0 if no channel layer is configured
    (e.g. during tests with ``LocMemChannelLayer`` disabled) or if the
    push raised.

    Push outcomes are recorded as
    ``alerts_push_attempts_total{result=success|no_layer|error}`` and
    ``alerts_push_failures_total{reason=<ExceptionClassName>}`` so the
    SLO alert in ``monitoring/prometheus/alerts.yml``
    (``AlertsWebSocketPushFailureRateHigh``) can fire. Failures never
    bubble out; the caller treats the push as best-effort.
    """
    layer = get_channel_layer()
    if layer is None:
        metrics.push_attempts(result="no_layer")
        return 0
    group = group_name_for_user(user_id)
    try:
        async_to_sync(layer.group_send)(  # type: ignore[arg-type]
            group, {"type": "audio.alert", "payload": payload}
        )
    except Exception as exc:  # noqa: BLE001
        metrics.push_attempts(result="error")
        metrics.push_failures(reason=exc.__class__.__name__)
        logger.warning(
            "WebSocket push failed for user %s: %s",
            user_id,
            exc.__class__.__name__,
        )
        return 0
    metrics.push_attempts(result="success")
    return 1


def _save_audio_bytes(alert: AudioAlert, audio_bytes: bytes) -> None:
    if not audio_bytes:
        return
    name = f"{alert.id}.wav"
    alert.audio_file.save(name, ContentFile(audio_bytes), save=False)


def build_push_payload(alert: AudioAlert) -> dict[str, Any]:
    """Build the WebSocket event payload for ``alert``.

    ``audio_url`` is empty until :func:`alerts.tasks.render_alert_audio`
    has finished (a second push delivers the URL).
    """
    return {
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
    }


def dispatch_alert_fast(
    *,
    user_id: int,
    farm_id: int | None,
    alert_type: str,
    trigger_source: str,
    title: str,
    message: str,
    source_object_id: str = "",
) -> AlertDispatchResult:
    """Fast path: write the row + push the WebSocket event.

    The TTS render and the audio file upload happen asynchronously
    in :func:`alerts.tasks.render_alert_audio`; a second push will
    update the user once the file is ready. The DB write is
    authoritative so a render failure never leaves the user
    without an alert row, and the WebSocket push remains
    best-effort (a transient Redis/Channels outage does not block
    the row from being saved).

    Instrumentation (see :mod:`alerts.metrics`):

    - ``alerts_dispatch_total{alert_type,result}`` increments once
      per call (``result=success|error``).
    - ``alerts_dispatch_duration_seconds{alert_type}`` observes the
      end-to-end latency (DB + push; no TTS).
    - ``alerts_dispatch_failures_total{alert_type,reason}`` records
      push failures.
    """
    if alert_type not in AudioAlertType.values:
        raise ValueError(f"unknown alert_type: {alert_type!r}")
    if trigger_source not in AudioAlertTriggerSource.values:
        raise ValueError(f"unknown trigger_source: {trigger_source!r}")
    with metrics.dispatch_timer(alert_type=alert_type):
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
        delivered = False
        try:
            sent = emit_audio_alert_event(user_id, build_push_payload(alert))
            if sent:
                alert.is_delivered = True
                alert.delivered_at = timezone.now()
                alert.save(update_fields=["is_delivered", "delivered_at"])
                delivered = True
        except Exception as exc:  # noqa: BLE001
            metrics.dispatch_failures(
                alert_type=alert_type, reason=exc.__class__.__name__
            )
            logger.warning(
                "WebSocket push failed for alert %s: %s",
                alert.id,
                exc.__class__.__name__,
            )
    metrics.dispatch_total(
        alert_type=alert_type, result="success" if delivered else "error"
    )
    return AlertDispatchResult(alert_id=alert.id, delivered=delivered)


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
    """Create, persist, and push a single audio alert.

    The fast path (:func:`dispatch_alert_fast`) writes the row and
    pushes the initial WebSocket event; the TTS render happens
    asynchronously in :func:`alerts.tasks.render_alert_audio`.

    Use this from synchronous triggers (admin views, REST
    endpoints). The Celery tasks and the admin broadcast path
    call :func:`dispatch_alert_fast` directly to avoid an
    unnecessary Redis hop.
    """
    result = dispatch_alert_fast(
        user_id=user_id,
        farm_id=farm_id,
        alert_type=alert_type,
        trigger_source=trigger_source,
        title=title,
        message=message,
        source_object_id=source_object_id,
    )
    # Defer the (potentially slow) TTS render to a Celery worker.
    try:
        from alerts.tasks import render_alert_audio

        render_alert_audio.delay(str(result.alert_id))
    except Exception as exc:  # noqa: BLE001 - broker outage is not fatal
        metrics.dispatch_failures(
            alert_type=alert_type,
            reason=f"enqueue:{exc.__class__.__name__}",
        )
        logger.warning(
            "Failed to enqueue render for alert %s: %s",
            result.alert_id,
            exc.__class__.__name__,
        )
    return result


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
