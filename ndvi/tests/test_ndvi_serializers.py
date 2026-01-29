from __future__ import annotations

from datetime import date
from typing import Any

from ndvi.serializers import (
    LatestRequestSerializer,
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
