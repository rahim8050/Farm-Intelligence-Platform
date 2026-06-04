"""Trigger sources for the `alerts` app.

Each public function in this module is the entry point used by
another app (or by a periodic Celery task) to fire an audio alert.
All of them funnel through :func:`alerts.services.dispatch_alert` so
the subscription / synth / push contract stays in one place.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from django.db import close_old_connections

from alerts.models import AudioAlertTriggerSource, AudioAlertType
from alerts.services import (
    dispatch_alert,
    subscribed_users_for_farm,
)

logger = logging.getLogger("alerts.triggers")


def on_activity_completed(
    *,
    user_id: int,
    farm_id: int | None,
    activity_id: int,
    activity_type: str,
    status: str,
    message: str,
) -> None:
    """Hook called by ``activities.tasks.execute`` on terminal state.

    Only fires for terminal states (``success`` or ``failed``). The
    caller has already filtered; we double-check defensively.
    """
    if status not in {"success", "failed"}:
        return
    if farm_id is None:
        logger.debug("activity %s has no farm; skipping alert", activity_id)
        return
    close_old_connections()
    subs = subscribed_users_for_farm(
        farm_id=farm_id, alert_type=AudioAlertType.ACTIVITY_COMPLETED
    )
    for sub in subs:
        try:
            dispatch_alert(
                user_id=sub.user_id,
                farm_id=farm_id,
                alert_type=AudioAlertType.ACTIVITY_COMPLETED,
                trigger_source=AudioAlertTriggerSource.ACTIVITY_TASK,
                title=_activity_title(activity_type, status),
                message=message,
                source_object_id=str(activity_id),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "activity alert dispatch failed for user %s: %s",
                sub.user_id,
                exc.__class__.__name__,
            )


def on_ndvi_decline(
    *, farm_id: int, owner_id: int | None, message: str
) -> int:
    """Fire a decline alert to the farm owner (and any sub-users).

    Returns the number of alerts dispatched.
    """
    return _fan_out(
        farm_id=farm_id,
        owner_id=owner_id,
        alert_type=AudioAlertType.NDVI_DECLINE,
        trigger_source=AudioAlertTriggerSource.NDVI_TASK,
        title="NDVI decline detected",
        message=message,
        source_object_id=str(farm_id),
    )


def on_ndvi_low(
    *,
    farm_id: int,
    owner_id: int | None,
    message: str,
    source_object_id: str = "",
) -> int:
    """Fire a low-NDVI alert. Returns the number of alerts dispatched."""
    return _fan_out(
        farm_id=farm_id,
        owner_id=owner_id,
        alert_type=AudioAlertType.NDVI_LOW,
        trigger_source=AudioAlertTriggerSource.NDVI_TASK,
        title="NDVI low threshold breached",
        message=message,
        source_object_id=source_object_id,
    )


def on_admin_broadcast(
    *,
    recipients: Iterable[int],
    title: str,
    message: str,
    farm_id: int | None = None,
) -> int:
    """Fire a manual admin broadcast to a list of users."""
    n = 0
    for user_id in recipients:
        try:
            dispatch_alert(
                user_id=user_id,
                farm_id=farm_id,
                alert_type=AudioAlertType.ADMIN_BROADCAST,
                trigger_source=AudioAlertTriggerSource.ADMIN_VIEW,
                title=title[:200],
                message=message,
            )
            n += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "admin broadcast dispatch failed for user %s: %s",
                user_id,
                exc.__class__.__name__,
            )
    return n


def _fan_out(
    *,
    farm_id: int,
    owner_id: int | None,
    alert_type: str,
    trigger_source: str,
    title: str,
    message: str,
    source_object_id: str,
) -> int:
    close_old_connections()
    user_ids: set[int] = set()
    for sub in subscribed_users_for_farm(
        farm_id=farm_id, alert_type=alert_type
    ):
        user_ids.add(sub.user_id)
    if owner_id is not None:
        user_ids.add(owner_id)
    n = 0
    for uid in sorted(user_ids):
        try:
            dispatch_alert(
                user_id=uid,
                farm_id=farm_id,
                alert_type=alert_type,
                trigger_source=trigger_source,
                title=title,
                message=message,
                source_object_id=source_object_id,
            )
            n += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ndvi alert dispatch failed for user %s: %s",
                uid,
                exc.__class__.__name__,
            )
    return n


def _activity_title(activity_type: str, status: str) -> str:
    verb = "completed" if status == "success" else "failed"
    return f"Activity {activity_type} {verb}"
