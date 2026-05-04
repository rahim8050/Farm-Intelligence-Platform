from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import models


class Activity(models.Model):
    """Activity scheduler model."""

    class Type(models.TextChoices):
        VACCINATION = "vaccination", "Vaccination"
        FERTILIZER = "fertilizer", "Fertilizer"
        IRRIGATION = "irrigation", "Irrigation"
        NDVI_TRIGGER = "ndvi_trigger", "NDVI Trigger"

    class Status(models.TextChoices):
        CREATED = "created", "Created"
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    class RecurrenceType(models.TextChoices):
        NONE = "none", "One-time"
        INTERVAL = "interval", "Interval"
        CRON = "cron", "Cron (future)"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="activities",
    )
    farm = models.ForeignKey(
        "farms.Farm",
        on_delete=models.CASCADE,
        related_name="activities",
        null=True,
        blank=True,
    )

    type = models.CharField(max_length=50, choices=Type.choices)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.CREATED,
    )

    scheduled_at = models.DateTimeField()
    next_due_at = models.DateTimeField(db_index=True)
    last_executed_at = models.DateTimeField(null=True, blank=True)

    recurrence_type = models.CharField(
        max_length=20,
        choices=RecurrenceType.choices,
        default=RecurrenceType.NONE,
    )
    interval_days = models.PositiveIntegerField(null=True, blank=True)

    metadata = models.JSONField(default=dict, blank=True)

    last_error = models.TextField(null=True, blank=True)
    retry_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["next_due_at"]
        indexes = [
            models.Index(
                fields=["status", "next_due_at"],
                name="activity_status_due_idx",
            ),
            models.Index(
                fields=["owner", "status", "next_due_at"],
                name="activity_owner_status_idx",
            ),
            models.Index(
                fields=["farm", "status", "next_due_at"],
                name="activity_farm_status_idx",
            ),
        ]

    def __str__(self) -> str:
        tid = self.id
        ttype = self.type
        tstatus = self.status
        return f"Activity(id={tid}, type={ttype}, status={tstatus})"

    def save(self, *args: object, **kwargs: object) -> None:
        if (
            self.recurrence_type == self.RecurrenceType.INTERVAL
            and self.interval_days
        ):
            if self.next_due_at is None:
                self.next_due_at = (
                    self.scheduled_at + timedelta(days=self.interval_days)
                )
        if self.next_due_at is None:
            self.next_due_at = self.scheduled_at
        super().save(*args, **kwargs)