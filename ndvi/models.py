from __future__ import annotations

from datetime import datetime

from django.conf import settings
from django.db import models
from django.utils import timezone

from farms.models import Farm


def default_ndvi_engine_name() -> str:
    return str(getattr(settings, "NDVI_ENGINE", "sentinelhub")).lower()


class NdviObservation(models.Model):
    """Materialized NDVI observation for a farm and date bucket."""

    class ObservationState(models.TextChoices):
        RAW = "RAW", "Raw engine output before final acceptance"
        FINAL = "FINAL", "Data that passed cloud and quality checks"
        SUPERSEDED = "SUPERSEDED", "Replaced by a newer observation"
        INVALIDATED = "INVALIDATED", "Marked invalid due to bad source data"
        REJECTED = "REJECTED", "Rejected by quality or processing rules"

    VALID_TRANSITIONS: dict[str, list[str]] = {
        ObservationState.RAW: [
            ObservationState.FINAL,
            ObservationState.SUPERSEDED,
            ObservationState.REJECTED,
        ],
        ObservationState.FINAL: [
            ObservationState.SUPERSEDED,
            ObservationState.INVALIDATED,
        ],
        ObservationState.SUPERSEDED: [],
        ObservationState.INVALIDATED: [],
        ObservationState.REJECTED: [],
    }

    farm = models.ForeignKey(
        Farm, on_delete=models.CASCADE, related_name="ndvi_observations"
    )
    engine = models.CharField(max_length=64, default=default_ndvi_engine_name)
    bucket_date = models.DateField()
    mean = models.FloatField()
    min = models.FloatField(null=True, blank=True)
    max = models.FloatField(null=True, blank=True)
    sample_count = models.IntegerField(null=True, blank=True)
    cloud_fraction = models.FloatField(null=True, blank=True)
    version = models.CharField(max_length=32, default="v1-legacy")
    state = models.CharField(
        max_length=16,
        choices=ObservationState.choices,
        default=ObservationState.FINAL,
    )
    is_latest = models.BooleanField(default=True)
    acquired_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the satellite acquired the source scene",
    )
    computed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When NDVI was computed for this observation",
    )
    ingested_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this row was persisted to the database",
    )
    source_scene_id = models.CharField(
        max_length=256,
        null=True,
        blank=True,
        help_text="Provider scene identifier for deduplication and provenance",
    )
    provenance = models.JSONField(
        default=dict,
        blank=True,
        help_text="Processing inputs: engine version, SCL mask params, etc.",
    )
    provenance_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        db_index=True,
        help_text="Deterministic hash of provenance for idempotency",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["farm", "engine", "bucket_date", "version"],
                name="uniq_ndvi_observation_farm_engine_bucket_version",
            ),
            models.UniqueConstraint(
                fields=["farm", "engine", "bucket_date"],
                condition=models.Q(is_latest=True),
                name="uniq_ndvi_latest_observation",
            ),
            models.UniqueConstraint(
                fields=[
                    "farm",
                    "engine",
                    "source_scene_id",
                    "provenance_hash",
                ],
                condition=models.Q(source_scene_id__isnull=False),
                name="uniq_ndvi_scene_per_farm_engine",
            ),
        ]
        indexes = [
            models.Index(fields=["farm", "bucket_date"]),
            models.Index(fields=["engine", "bucket_date"]),
            models.Index(
                fields=["farm", "engine", "bucket_date", "is_latest"]
            ),
            models.Index(fields=["version", "engine"]),
            models.Index(fields=["state", "engine"]),
            models.Index(fields=["acquired_at", "engine"]),
            models.Index(fields=["source_scene_id", "engine"]),
        ]

    def __str__(self) -> str:
        return (
            f"NDVI {self.bucket_date} farm={self.farm_id} engine={self.engine}"
            f" v={self.version} state={self.state} latest={self.is_latest}"
        )

    def can_transition_to(self, new_state: str) -> bool:
        """Check if the observation can transition to the given state."""
        allowed = self.VALID_TRANSITIONS.get(self.state, [])
        return new_state in allowed

    def transition_state(self, new_state: str) -> None:
        """Transition to a new state with validation.

        Raises:
            ValueError: If the transition is not allowed.
        """
        if not self.can_transition_to(new_state):
            raise ValueError(
                f"Cannot transition from {self.state} to {new_state}. "
                f"Allowed: {self.VALID_TRANSITIONS.get(self.state, [])}"
            )
        self.state = new_state
        self.save(update_fields=["state", "updated_at"])


class NdviJob(models.Model):
    """Idempotent NDVI job record tracked for Celery tasks."""

    class JobType(models.TextChoices):
        REFRESH_LATEST = "refresh_latest", "Refresh latest"
        GAP_FILL = "gap_fill", "Gap fill"
        BACKFILL = "backfill", "Backfill"
        RASTER_PNG = "raster_png", "Raster PNG"

    class JobStatus(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ndvi_jobs",
    )
    farm = models.ForeignKey(
        Farm,
        on_delete=models.CASCADE,
        related_name="ndvi_jobs",
    )
    engine = models.CharField(max_length=64)
    job_type = models.CharField(max_length=32, choices=JobType.choices)
    start = models.DateField(null=True, blank=True)
    end = models.DateField(null=True, blank=True)
    step_days = models.PositiveIntegerField(null=True, blank=True)
    max_cloud = models.PositiveIntegerField(null=True, blank=True)
    lookback_days = models.PositiveIntegerField(null=True, blank=True)
    request_hash = models.CharField(max_length=128, db_index=True)
    status = models.CharField(
        max_length=16,
        choices=JobStatus.choices,
        default=JobStatus.QUEUED,
    )
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(null=True, blank=True)
    locked_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "farm", "engine", "request_hash"],
                condition=models.Q(status__in=["queued", "running"]),
                name="uniq_active_ndvi_job",
            ),
        ]
        indexes = [
            models.Index(fields=["owner", "farm", "status"]),
            models.Index(fields=["request_hash"]),
        ]

    def __str__(self) -> str:
        return (
            f"NdviJob {self.id} type={self.job_type} "
            f"farm={self.farm_id} status={self.status}"
        )

    def mark_running(self, locked_until: datetime | None = None) -> None:
        self.status = self.JobStatus.RUNNING
        self.started_at = timezone.now()
        if locked_until:
            self.locked_until = locked_until
        self.attempts += 1
        self.save(
            update_fields=["status", "started_at", "locked_until", "attempts"]
        )

    def mark_finished(self, status: str, error: str | None = None) -> None:
        self.status = status
        self.finished_at = timezone.now()
        self.last_error = error
        fields = ["status", "finished_at", "last_error"]
        self.save(update_fields=fields)


class NdviRasterArtifact(models.Model):
    """Persisted NDVI raster PNG artifact for a farm and date."""

    farm = models.ForeignKey(
        Farm,
        on_delete=models.CASCADE,
        related_name="ndvi_rasters",
    )
    owner_id = models.IntegerField(db_index=True)
    engine = models.CharField(max_length=64)
    date = models.DateField()
    size = models.PositiveSmallIntegerField()
    max_cloud = models.PositiveSmallIntegerField()
    content_hash = models.CharField(max_length=64, db_index=True)
    image = models.FileField(upload_to="ndvi/rasters/%Y/%m/%d/")
    created_at = models.DateTimeField(auto_now_add=True)
    last_error = models.TextField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["farm", "engine", "date", "size", "max_cloud"],
                name="uniq_ndvi_raster_farm_engine_date_size_cloud",
            ),
        ]
        indexes = [
            models.Index(fields=["owner_id", "date"]),
            models.Index(fields=["engine", "date"]),
        ]

    def __str__(self) -> str:
        return (
            f"Raster {self.date} farm={self.farm_id} engine={self.engine} "
            f"size={self.size}"
        )
