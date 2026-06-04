"""Models for the `alerts` app.

Two top-level entities:

- ``AudioAlertSubscription`` — a per-user, per-farm opt-in row that lists
  which ``AudioAlertType`` values the user wants to receive. A user with
  no subscription rows receives no alerts.
- ``AudioAlert`` — a single generated audio notification. Carries the
  trigger source, the text that was synthesised, the generated audio file
  (under ``MEDIA_ROOT/audio_alerts/...``), delivery state, and the
  triggering farm / activity / observation when applicable.

The model is intentionally minimal: there is no FK to ``Activity`` or
``NdviObservation`` because we want to be able to retain an alert row
even after the source row is deleted (privacy: keep the user-facing
record, drop the source detail if it was transient). The
``source_object_id`` field carries the optional source id for ad-hoc
debugging.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class AudioAlertType(models.TextChoices):
    """High-level category of an audio alert."""

    NDVI_DECLINE = "ndvi_decline", "NDVI decline"
    NDVI_LOW = "ndvi_low", "NDVI low absolute"
    ACTIVITY_COMPLETED = "activity_completed", "Activity completed"
    ADMIN_BROADCAST = "admin_broadcast", "Admin broadcast"


class AudioAlertTriggerSource(models.TextChoices):
    """Where the alert was triggered from."""

    NDVI_TASK = "ndvi_task", "NDVI task"
    ACTIVITY_TASK = "activity_task", "Activity task"
    ADMIN_VIEW = "admin_view", "Admin view"


class AudioAlertSubscription(models.Model):
    """A user's per-farm opt-in for one or more audio-alert types."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="audio_alert_subscriptions",
    )
    farm = models.ForeignKey(
        "farms.Farm",
        on_delete=models.CASCADE,
        related_name="audio_alert_subscriptions",
    )
    alert_types = models.JSONField(
        default=list,
        help_text=(
            "List of AudioAlertType values the user wants to receive "
            "for this farm. An empty list means no alerts."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "alerts_audio_subscription"
        verbose_name = "Audio alert subscription"
        verbose_name_plural = "Audio alert subscriptions"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "farm"],
                name="alerts_audio_subscription_user_farm_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "farm"]),
        ]


class AudioAlert(models.Model):
    """One generated audio alert delivered (or queued) for a user."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="audio_alerts",
    )
    farm = models.ForeignKey(
        "farms.Farm",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audio_alerts",
    )
    alert_type = models.CharField(
        max_length=32, choices=AudioAlertType.choices
    )
    trigger_source = models.CharField(
        max_length=32,
        choices=AudioAlertTriggerSource.choices,
    )
    title = models.CharField(max_length=200)
    message = models.TextField()
    audio_file = models.FileField(
        upload_to="audio_alerts/%Y/%m/%d/",
        blank=True,
        null=True,
    )
    duration_ms = models.PositiveIntegerField(default=0)
    mime_type = models.CharField(max_length=64, blank=True)
    source_object_id = models.CharField(max_length=64, blank=True)
    is_delivered = models.BooleanField(
        default=False,
        help_text="True once a WebSocket push has been confirmed.",
    )
    is_acknowledged = models.BooleanField(
        default=False,
        help_text="True once the user has marked the alert as read.",
    )
    delivered_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "alerts_audio_alert"
        verbose_name = "Audio alert"
        verbose_name_plural = "Audio alerts"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["user", "is_acknowledged"]),
            models.Index(fields=["alert_type", "-created_at"]),
        ]
