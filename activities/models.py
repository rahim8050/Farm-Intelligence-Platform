from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

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
        DISPATCHED = "dispatched", "Dispatched"
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        RETRY = "retry", "Retry"

    class RecurrenceType(models.TextChoices):
        NONE = "none", "One-time"
        INTERVAL = "interval", "Interval"
        CRON = "cron", "Cron"

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
    cron_expression = models.CharField(max_length=100, null=True, blank=True)

    metadata = models.JSONField(default=dict, blank=True)

    execution_id = models.UUIDField(null=True, blank=True, editable=False)
    execution_started_at = models.DateTimeField(null=True, blank=True)
    execution_completed_at = models.DateTimeField(null=True, blank=True)

    last_error = models.TextField(null=True, blank=True)
    retry_count = models.PositiveIntegerField(default=0)
    max_retries = models.PositiveIntegerField(default=3)

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

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.next_due_at is None:
            if (
                self.recurrence_type == self.RecurrenceType.INTERVAL
                and self.interval_days
            ):
                self.next_due_at = self.scheduled_at + timedelta(
                    days=self.interval_days
                )
            elif (
                self.recurrence_type == self.RecurrenceType.CRON
                and self.cron_expression
            ):
                self.next_due_at = self._compute_cron_next(
                    self.cron_expression, self.scheduled_at
                )
            else:
                self.next_due_at = self.scheduled_at
        super().save(*args, **kwargs)

    @staticmethod
    def _parse_cron_field(value: str, lo: int, hi: int) -> set[int]:
        """Parse a single cron field into a set of allowed values."""
        result: set[int] = set()
        for part in value.split(","):
            part = part.strip()
            if "/" in part:
                base, step = part.split("/", 1)
                step = int(step)
                if base == "*":
                    rng = range(lo, hi + 1)
                elif "-" in base:
                    a, b = base.split("-", 1)
                    rng = range(int(a), int(b) + 1)
                else:
                    a = int(base)
                    rng = range(a, hi + 1)
                result.update(rng[::step])
            elif "-" in part:
                a, b = part.split("-", 1)
                result.update(range(int(a), int(b) + 1))
            elif part == "*":
                result.update(range(lo, hi + 1))
            else:
                result.add(int(part))
        return result

    @staticmethod
    def _compute_cron_next(expression: str, from_date: datetime) -> datetime:
        """Compute the next datetime matching a 5-field cron expression.

        Standard cron format: minute hour day_of_month month day_of_week.
        day_of_week: 0=Sunday, 6=Saturday (matches Celery convention).
        """
        fields = expression.strip().split()
        if len(fields) != 5:
            msg = f"Expected 5 fields, got {len(fields)}: {expression!r}"
            raise ValueError(msg)

        minute_set = Activity._parse_cron_field(fields[0], 0, 59)
        hour_set = Activity._parse_cron_field(fields[1], 0, 23)
        day_set = Activity._parse_cron_field(fields[2], 1, 31)
        month_set = Activity._parse_cron_field(fields[3], 1, 12)
        dow_set = Activity._parse_cron_field(fields[4], 0, 6)

        # Ceil to the next minute
        candidate = from_date.replace(second=0, microsecond=0) + timedelta(
            minutes=1
        )

        for _ in range(366 * 24 * 60):
            if candidate.month not in month_set:
                year = candidate.year + (candidate.month // 12)
                month = (candidate.month % 12) + 1
                candidate = candidate.replace(
                    year=year, month=month, day=1, hour=0, minute=0
                )
                continue

            # day_of_week: Celery uses 0=Sunday, so convert
            # Python weekday() (0=Monday) to Celery convention
            cel_dow = (candidate.weekday() + 1) % 7
            if candidate.day not in day_set or cel_dow not in dow_set:
                # Advance to next day
                candidate += timedelta(days=1)
                candidate = candidate.replace(hour=0, minute=0)
                continue

            if candidate.hour not in hour_set:
                # Advance to next hour
                candidate += timedelta(hours=1)
                candidate = candidate.replace(minute=0)
                continue

            if candidate.minute not in minute_set:
                # Advance to next minute
                candidate += timedelta(minutes=1)
                continue

            return candidate

        msg = (
            f"Could not find next cron match for {expression!r}"
            f" from {from_date}"
        )
        raise ValueError(msg)
