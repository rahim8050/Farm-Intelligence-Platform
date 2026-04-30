from __future__ import annotations

from datetime import date
from typing import Any

from ndvi.serializers import (
    LatestRequestSerializer,
    NdviIngestSerializer,
    NdviJobSerializer,
    RasterPngRequestSerializer,
)
from ndvi.services import get_default_lookback_days, get_default_max_cloud


def test_latest_request_serializer_defaults() -> None:
    serializer = LatestRequestSerializer(data={})

    assert serializer.is_valid(), serializer.errors
    assert (
        serializer.validated_data["lookback_days"]
        == get_default_lookback_days()
    )
    assert serializer.validated_data["max_cloud"] == get_default_max_cloud()


def test_latest_request_serializer_stac_defaults(settings: Any) -> None:
    settings.NDVI_STAC_MAX_CLOUD_DEFAULT = 15
    serializer = LatestRequestSerializer(data={}, context={"engine": "stac"})

    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["max_cloud"] == 15


def test_raster_png_serializer_accepts_flexible_date_and_defaults(
    settings: Any,
) -> None:
    settings.NDVI_RASTER_DEFAULT_SIZE = 512
    settings.NDVI_DEFAULT_MAX_CLOUD = 35
    serializer = RasterPngRequestSerializer(data={"date": "01/02/2024"})

    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["date"] == date(2024, 1, 2)
    assert serializer.validated_data["size"] == 512
    assert serializer.validated_data["max_cloud"] == 35


def test_raster_png_serializer_rejects_small_size() -> None:
    serializer = RasterPngRequestSerializer(
        data={"date": "2024-01-02", "size": 64}
    )

    assert not serializer.is_valid()
    assert "size must be between" in serializer.errors["non_field_errors"][0]


def test_raster_png_serializer_rejects_large_canvas(settings: Any) -> None:
    settings.NDVI_RASTER_MAX_SIZE = 2048
    serializer = RasterPngRequestSerializer(
        data={"date": "2024-01-02", "size": 2000}
    )

    assert not serializer.is_valid()
    assert "size too large" in serializer.errors["non_field_errors"][0].lower()


def test_ndvi_job_serializer() -> None:
    from ndvi.models import NdviJob

    job = NdviJob(
        id=123,
        job_type=NdviJob.JobType.REFRESH_LATEST,
        status=NdviJob.JobStatus.QUEUED,
        attempts=1,
    )
    serializer = NdviJobSerializer(instance=job)
    assert serializer.data["id"] == 123
    assert serializer.data["job_type"] == "refresh_latest"
    assert serializer.data["status"] == "queued"


def test_ndvi_ingest_serializer_valid() -> None:
    data = {
        "farm_id": "550e8400-e29b-41d4-a716-446655440000",
        "timestamp": "2024-01-01T12:00:00Z",
        "mean": 0.5,
        "min": 0.3,
        "max": 0.7,
    }
    serializer = NdviIngestSerializer(data=data)
    assert serializer.is_valid(), serializer.errors


def test_ndvi_ingest_serializer_invalid_range() -> None:
    data = {
        "farm_id": "550e8400-e29b-41d4-a716-446655440000",
        "timestamp": "2024-01-01T12:00:00Z",
        "mean": 1.5,
        "min": 0.3,
        "max": 0.7,
    }
    serializer = NdviIngestSerializer(data=data)
    assert not serializer.is_valid()
    assert "mean" in serializer.errors


def test_ndvi_ingest_serializer_invalid_order() -> None:
    data = {
        "farm_id": "550e8400-e29b-41d4-a716-446655440000",
        "timestamp": "2024-01-01T12:00:00Z",
        "mean": 0.2,
        "min": 0.3,
        "max": 0.7,
    }
    serializer = NdviIngestSerializer(data=data)
    assert not serializer.is_valid()
    assert "NDVI values must satisfy min <= mean <= max" in str(
        serializer.errors["non_field_errors"][0]
    )
