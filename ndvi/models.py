from __future__ import annotations

from datetime import date, datetime

from django.conf import settings
from django.db import models
from django.db.models import QuerySet
from django.utils import timezone

from farms.models import Farm


def default_ndvi_engine_name() -> str:
    return str(getattr(settings, "NDVI_ENGINE", "sentinelhub")).lower()


class ValidObservationQuerySet(QuerySet):
    """QuerySet that enforces analytical validity rules.

    Only returns observations that are:
    - state=FINAL
    - is_latest=True
    - mean is not None

    This prevents ad-hoc ORM queries from bypassing the canonical
    validity rules used across all NDVI read paths.
    """

    def valid(self) -> ValidObservationQuerySet:
        """Filter to analytically valid observations only."""
        return self.filter(
            state=NdviObservation.ObservationState.FINAL,
            is_latest=True,
        ).exclude(mean__isnull=True)

    def for_engine(self, engine: str) -> ValidObservationQuerySet:
        """Filter by engine."""
        return self.filter(engine=engine)

    def for_farm(self, farm: Farm) -> ValidObservationQuerySet:
        """Filter by farm."""
        return self.filter(farm=farm)

    def for_date_range(
        self, start: date | None, end: date | None
    ) -> ValidObservationQuerySet:
        """Filter by date range (inclusive)."""
        qs = self
        if start:
            qs = qs.filter(bucket_date__gte=start)
        if end:
            qs = qs.filter(bucket_date__lte=end)
        return qs

    def with_min_version(self, min_version: str) -> ValidObservationQuerySet:
        """Filter by minimum version (string comparison).

        Note: For strict semantic version comparison, filter results
        in Python using services.version_gte().
        """
        return self.filter(version__gte=min_version)

    def for_engines(self, engines: list[str]) -> ValidObservationQuerySet:
        """Filter to allowed engines."""
        return self.filter(engine__in=engines)


class ObservationManager(
    models.Manager.from_queryset(ValidObservationQuerySet)  # type: ignore[misc]
):
    """Custom manager for NdviObservation with validity enforcement.

    Use `NdviObservation.objects.valid()` as the canonical read path
    instead of building ad-hoc filters. This prevents filter drift
    and ensures consistent validity rules across all consumers.
    """


class NdviObservation(models.Model):
    """Materialized NDVI observation for a farm and date bucket.

    Use `NdviObservation.objects.valid()` as the canonical read path
    for analytically valid observations. This enforces:
    - state=FINAL
    - is_latest=True
    - mean is not None

    For additional validity checks (min_version, allowed_engines),
    use the chainable methods on ValidObservationQuerySet or the
    services.is_analytically_valid() function for Python-level checks.
    """

    objects = ObservationManager()

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

    index_type = models.CharField(
        max_length=16,
        choices=[("NDVI", "NDVI"), ("NDWI", "NDWI")],
        default="NDVI",
        db_index=True,
        help_text="Spectral index discriminator (NDVI, NDWI, etc.)",
    )

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
    valid_pixel_fraction = models.FloatField(
        null=True,
        blank=True,
        help_text="Fraction of pixels that passed SCL/quality masking",
    )
    quality_flags = models.JSONField(
        default=dict,
        blank=True,
        help_text="Quality indicators: cloud_heavy, partial_tile, "
        "low_valid_pixel_fraction, water_detected, etc.",
    )
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
                fields=[
                    "farm",
                    "engine",
                    "bucket_date",
                    "version",
                    "index_type",
                ],
                name="uniq_observation_per_index_farm_engine_bucket_version",
            ),
            models.UniqueConstraint(
                fields=["farm", "engine", "bucket_date", "index_type"],
                condition=models.Q(is_latest=True),
                name="uniq_observation_latest_per_index",
            ),
            models.UniqueConstraint(
                fields=[
                    "farm",
                    "engine",
                    "source_scene_id",
                    "provenance_hash",
                    "index_type",
                ],
                condition=models.Q(source_scene_id__isnull=False),
                name="uniq_scene_per_index_farm_engine",
            ),
        ]
        indexes = [
            models.Index(fields=["index_type", "farm", "bucket_date"]),
            models.Index(fields=["index_type", "engine", "bucket_date"]),
            models.Index(
                fields=[
                    "index_type",
                    "farm",
                    "engine",
                    "bucket_date",
                    "is_latest",
                ]
            ),
            models.Index(fields=["index_type", "version", "engine"]),
            models.Index(fields=["index_type", "state", "engine"]),
            models.Index(fields=["index_type", "acquired_at", "engine"]),
            models.Index(fields=["index_type", "source_scene_id", "engine"]),
        ]

    def __str__(self) -> str:
        return (
            f"{self.index_type} {self.bucket_date} farm={self.farm_id}"
            f" engine={self.engine} v={self.version} state={self.state}"
            f" latest={self.is_latest}"
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
    """Idempotent NDVI/NDWI job record tracked for Celery tasks."""

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
    index_type = models.CharField(
        max_length=16,
        choices=[("NDVI", "NDVI"), ("NDWI", "NDWI")],
        default="NDVI",
        help_text="Spectral index discriminator (NDVI, NDWI, etc.)",
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
                fields=[
                    "owner",
                    "farm",
                    "engine",
                    "request_hash",
                    "index_type",
                ],
                condition=models.Q(status__in=["queued", "running"]),
                name="uniq_active_job_per_index",
            ),
        ]
        indexes = [
            models.Index(fields=["index_type", "owner", "farm", "status"]),
            models.Index(fields=["index_type", "request_hash"]),
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
    index_type = models.CharField(
        max_length=16,
        choices=[("NDVI", "NDVI"), ("NDWI", "NDWI")],
        default="NDVI",
        help_text="Spectral index discriminator (NDVI, NDWI, etc.)",
    )
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
                fields=[
                    "farm",
                    "engine",
                    "date",
                    "size",
                    "max_cloud",
                    "index_type",
                ],
                name="uniq_raster_per_index_farm_engine_date_size_cloud",
            ),
        ]
        indexes = [
            models.Index(fields=["index_type", "owner_id", "date"]),
            models.Index(fields=["index_type", "engine", "date"]),
        ]

    def __str__(self) -> str:
        return (
            f"Raster {self.date} farm={self.farm_id} engine={self.engine} "
            f"size={self.size}"
        )


class NdviDerivedObservation(models.Model):
    """V2 decision-grade observation derived from a V1 raw observation.

    This model stores confidence-scored, smoothed NDVI values with
    explicit null behavior when quality is insufficient. It is the
    output of the V2 quality engine (Phase 2).
    """

    farm = models.ForeignKey(
        Farm,
        on_delete=models.CASCADE,
        related_name="ndvi_v2_observations",
    )
    v1_observation = models.OneToOneField(
        NdviObservation,
        on_delete=models.CASCADE,
        related_name="v2_observation",
    )
    index_type = models.CharField(
        max_length=16,
        choices=[("NDVI", "NDVI"), ("NDWI", "NDWI")],
        default="NDVI",
        help_text="Spectral index discriminator (NDVI, NDWI, etc.)",
    )
    engine = models.CharField(max_length=64)
    bucket_date = models.DateField()
    source = models.CharField(
        max_length=32,
        help_text="Source engine that produced this V2 observation",
    )
    selected_ndvi = models.FloatField(
        null=True,
        blank=True,
        help_text="Selected NDVI value (may be null if quality insufficient)",
    )
    smoothed_ndvi = models.FloatField(
        null=True,
        blank=True,
        help_text="Temporally smoothed NDVI value",
    )
    confidence = models.FloatField(
        help_text="Confidence score in [0, 1]",
    )
    confidence_components = models.JSONField(
        default=dict,
        blank=True,
        help_text="Breakdown of confidence formula components",
    )
    quality_flags = models.JSONField(
        default=dict,
        blank=True,
        help_text="Quality flags: cloud_heavy, low_confidence, "
        "outlier_removed, etc.",
    )
    is_null = models.BooleanField(
        default=False,
        help_text="True when this observation was forced to null",
    )
    null_reason = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        help_text="Reason for null output",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["v1_observation"],
                name="uniq_v2_per_v1_observation",
            ),
            models.UniqueConstraint(
                fields=["farm", "engine", "bucket_date", "index_type"],
                name="uniq_v2_per_index_farm_engine_bucket",
            ),
        ]
        indexes = [
            models.Index(
                fields=["index_type", "farm", "engine", "bucket_date"]
            ),
            models.Index(fields=["index_type", "engine", "confidence"]),
            models.Index(fields=["index_type", "source", "bucket_date"]),
        ]

    def __str__(self) -> str:
        null_str = " NULL" if self.is_null else ""
        return (
            f"V2 NDVI {self.bucket_date} farm={self.farm_id} "
            f"engine={self.engine} confidence={self.confidence:.2f}{null_str}"
        )
