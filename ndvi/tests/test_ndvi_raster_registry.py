from __future__ import annotations

# ruff: noqa: S101
from django.test import override_settings

from ndvi.raster.registry import get_engine, resolve_raster_engine_name
from ndvi.raster.stac_compute_engine import StacComputeRasterEngine


@override_settings(
    NDVI_RASTER_ENGINE_NAME="stac",
    NDVI_STAC_COLLECTION="collection",
)
def test_raster_get_engine_uses_settings_default() -> None:
    engine = get_engine(None)
    assert isinstance(engine, StacComputeRasterEngine)
    assert resolve_raster_engine_name(None) == "stac"
