from __future__ import annotations

from decimal import Decimal
from typing import Any, Final

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.text import slugify

_LAT_MIN: Final[Decimal] = Decimal("-90")
_LAT_MAX: Final[Decimal] = Decimal("90")
_LON_MIN: Final[Decimal] = Decimal("-180")
_LON_MAX: Final[Decimal] = Decimal("180")


class Farm(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="farms",
    )

    external_farm_id = models.UUIDField(
        unique=True,
        db_index=True,
        null=True,
        blank=True,
    )
    external_user_id = models.CharField(
        max_length=255,
        db_index=True,
        null=True,
        blank=True,
    )

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140)

    # Optional “label” location (useful for UI pin)
    centroid_lat = models.DecimalField(
        max_digits=8,
        decimal_places=5,
        null=True,
        blank=True,
        validators=[MinValueValidator(_LAT_MIN), MaxValueValidator(_LAT_MAX)],
    )
    centroid_lon = models.DecimalField(
        max_digits=8,
        decimal_places=5,
        null=True,
        blank=True,
        validators=[MinValueValidator(_LON_MIN), MaxValueValidator(_LON_MAX)],
    )

    # AOI bounding box for NDVI queries (WGS84 degrees)
    bbox_south = models.DecimalField(
        max_digits=8,
        decimal_places=5,
        null=True,
        blank=True,
        validators=[MinValueValidator(_LAT_MIN), MaxValueValidator(_LAT_MAX)],
    )
    bbox_west = models.DecimalField(
        max_digits=9,
        decimal_places=5,
        null=True,
        blank=True,
        validators=[MinValueValidator(_LON_MIN), MaxValueValidator(_LON_MAX)],
    )
    bbox_north = models.DecimalField(
        max_digits=8,
        decimal_places=5,
        null=True,
        blank=True,
        validators=[MinValueValidator(_LAT_MIN), MaxValueValidator(_LAT_MAX)],
    )
    bbox_east = models.DecimalField(
        max_digits=9,
        decimal_places=5,
        null=True,
        blank=True,
        validators=[MinValueValidator(_LON_MIN), MaxValueValidator(_LON_MAX)],
    )

    area_ha = models.DecimalField(
        max_digits=12, decimal_places=3, null=True, blank=True
    )

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "slug"],
                name="uniq_farm_owner_slug",
            ),
            models.UniqueConstraint(
                fields=["owner", "name"],
                name="uniq_farm_owner_name",
            ),
        ]
        indexes = [
            models.Index(fields=["owner", "created_at"]),
            models.Index(fields=["owner", "slug"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.owner_id})"

    def clean(self) -> None:
        super().clean()

        bbox_fields = (
            self.bbox_south,
            self.bbox_west,
            self.bbox_north,
            self.bbox_east,
        )
        bbox_any = any(v is not None for v in bbox_fields)
        bbox_all = all(v is not None for v in bbox_fields)

        if bbox_any and not bbox_all:
            raise ValidationError(
                "Bounding box must include south, west, north, and east."
            )

        if bbox_all:
            # Note: this assumes bbox does NOT cross the antimeridian (±180).
            if self.bbox_south is not None and self.bbox_north is not None:
                if self.bbox_south >= self.bbox_north:
                    raise ValidationError("bbox_south must be < bbox_north.")
            if self.bbox_west is not None and self.bbox_east is not None:
                if self.bbox_west >= self.bbox_east:
                    raise ValidationError("bbox_west must be < bbox_east.")

        centroid_any = (
            self.centroid_lat is not None or self.centroid_lon is not None
        )
        centroid_all = (
            self.centroid_lat is not None and self.centroid_lon is not None
        )
        if centroid_any and not centroid_all:
            raise ValidationError(
                "Centroid requires both centroid_lat and centroid_lon."
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self.slug:
            base = slugify(self.name)[:120] or "farm"
            self.slug = base
        super().save(*args, **kwargs)


class FarmObservation(models.Model):
    """Log entry describing an observation or activity on a farm."""

    farm = models.ForeignKey(
        Farm, on_delete=models.CASCADE, related_name="observations"
    )
    observed_at = models.DateTimeField()
    event_type = models.CharField(max_length=120)
    note = models.TextField(blank=True)
    metadata = models.JSONField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="farm_observations",
        null=True,
        blank=True,
    )
    created_by_client_id = models.CharField(
        max_length=120, null=True, blank=True
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["farm", "observed_at"]),
        ]

    def __str__(self) -> str:
        return f"FarmObservation {self.farm_id} {self.event_type}"


class FarmIntegrationAccess(models.Model):
    """Allow-listed integration client access to a farm."""

    farm = models.ForeignKey(
        Farm,
        on_delete=models.CASCADE,
        related_name="integration_access",
    )
    client_id = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["farm", "client_id"],
                name="uniq_farm_integration_access",
            )
        ]
        indexes = [
            models.Index(fields=["client_id", "farm"]),
        ]

    def __str__(self) -> str:
        return f"FarmIntegrationAccess {self.farm_id} {self.client_id}"


class FarmSyncIdempotencyRecord(models.Model):
    """Track Idempotency-Key outcomes for farm sync requests."""

    key = models.CharField(max_length=191, unique=True, db_index=True)
    external_farm_id = models.UUIDField(null=True, blank=True)
    status_code = models.PositiveSmallIntegerField()
    response_payload = models.JSONField()

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["created_at"]),
        ]

    def __str__(self) -> str:
        return f"FarmSyncIdempotencyRecord {self.key}"
