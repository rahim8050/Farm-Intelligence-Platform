from __future__ import annotations

import re
from datetime import date
from typing import Any, cast

from django.conf import settings
from rest_framework import serializers

from .models import NdviJob, NdviObservation
from .services import (
    get_default_max_cloud,
    normalize_latest_params,
    normalize_timeseries_params,
)


def _default_max_cloud_for_engine(engine: str | None) -> int:
    if engine == "stac":
        return int(getattr(settings, "NDVI_STAC_MAX_CLOUD_DEFAULT", 30))
    return get_default_max_cloud()


class FlexibleDateField(serializers.DateField):
    """Accept ISO dates plus MM/DD/YYYY inputs and normalize to ISO."""

    def to_internal_value(self, value: object) -> date:
        normalized = value
        if isinstance(value, str):
            raw = value.strip()
            match = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", raw)
            if match:
                month = int(match.group(1))
                day = int(match.group(2))
                year = int(match.group(3))
                normalized = f"{year:04d}-{month:02d}-{day:02d}"
        return super().to_internal_value(cast(str | date, normalized))


class NdviObservationSerializer(serializers.ModelSerializer):
    class Meta:
        model = NdviObservation
        fields = [
            "bucket_date",
            "mean",
            "min",
            "max",
            "sample_count",
            "cloud_fraction",
        ]


class FarmStateSerializer(serializers.Serializer):
    farm_id = serializers.IntegerField()
    mean_ndvi = serializers.FloatField(allow_null=True)
    max_ndvi = serializers.FloatField(allow_null=True)
    coverage_pct = serializers.FloatField(allow_null=True)
    trend = serializers.FloatField(allow_null=True)
    state = serializers.CharField()
    interpretation = serializers.CharField()
    action = serializers.CharField()


class TimeseriesRequestSerializer(serializers.Serializer):
    start = FlexibleDateField()
    end = FlexibleDateField()
    step_days = serializers.IntegerField(
        required=False, min_value=1, max_value=30
    )
    max_cloud = serializers.IntegerField(
        required=False, min_value=0, max_value=100
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        engine = self.context.get("engine")
        default_max_cloud = _default_max_cloud_for_engine(
            cast(str | None, engine)
        )
        params = normalize_timeseries_params(
            start=cast(date, attrs["start"]),
            end=cast(date, attrs["end"]),
            step_days=cast(int | None, attrs.get("step_days")),
            max_cloud=cast(int | None, attrs.get("max_cloud")),
            default_max_cloud=default_max_cloud,
        )
        return {
            "start": params.start,
            "end": params.end,
            "step_days": params.step_days,
            "max_cloud": params.max_cloud,
        }


class LatestRequestSerializer(serializers.Serializer):
    lookback_days = serializers.IntegerField(required=False, min_value=1)
    max_cloud = serializers.IntegerField(
        required=False, min_value=0, max_value=100
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        engine = self.context.get("engine")
        default_max_cloud = _default_max_cloud_for_engine(
            cast(str | None, engine)
        )
        params = normalize_latest_params(
            lookback_days=cast(int | None, attrs.get("lookback_days")),
            max_cloud=cast(int | None, attrs.get("max_cloud")),
            default_max_cloud=default_max_cloud,
        )
        return {
            "lookback_days": params.lookback_days,
            "max_cloud": params.max_cloud,
        }


class RasterPngRequestSerializer(serializers.Serializer):
    date = FlexibleDateField()
    size = serializers.IntegerField(required=False)
    max_cloud = serializers.IntegerField(
        required=False, min_value=0, max_value=100
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        engine = self.context.get("engine")
        default_max_cloud = _default_max_cloud_for_engine(
            cast(str | None, engine)
        )
        size_default = int(getattr(settings, "NDVI_RASTER_DEFAULT_SIZE", 512))
        size_max = int(getattr(settings, "NDVI_RASTER_MAX_SIZE", 1024))
        size = cast(int | None, attrs.get("size")) or size_default
        if size < 128 or size > size_max:
            raise serializers.ValidationError(
                f"size must be between 128 and {size_max}"
            )
        if size * size > 1024 * 1024:
            raise serializers.ValidationError(
                "size too large: max 1,048,576 pixels"
            )
        max_cloud = cast(int | None, attrs.get("max_cloud"))
        if max_cloud is None:
            max_cloud = default_max_cloud

        return {
            "date": cast(date, attrs["date"]),
            "size": size,
            "max_cloud": max_cloud,
        }


class NdviJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = NdviJob
        fields = [
            "id",
            "job_type",
            "status",
            "start",
            "end",
            "step_days",
            "max_cloud",
            "lookback_days",
            "created_at",
            "started_at",
            "finished_at",
            "attempts",
            "last_error",
        ]
        read_only_fields = fields


class NdviIngestSerializer(serializers.Serializer):
    farm_id = serializers.UUIDField()
    timestamp = serializers.DateTimeField()
    mean = serializers.FloatField()
    min = serializers.FloatField()
    max = serializers.FloatField()
    source = serializers.CharField(  # type: ignore[assignment]
        required=False,
        allow_null=True,
        allow_blank=True,
    )
    geometry = serializers.JSONField(required=False, allow_null=True)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        errors: dict[str, str] = {}
        for field in ("mean", "min", "max"):
            value = float(attrs[field])
            if value < 0.0 or value > 1.0:
                errors[field] = "NDVI values must be between 0.0 and 1.0."
        if errors:
            raise serializers.ValidationError(errors)

        min_value = float(attrs["min"])
        mean_value = float(attrs["mean"])
        max_value = float(attrs["max"])
        if not (min_value <= mean_value <= max_value <= 1.0):
            raise serializers.ValidationError(
                "NDVI values must satisfy min <= mean <= max <= 1.0."
            )
        return attrs
