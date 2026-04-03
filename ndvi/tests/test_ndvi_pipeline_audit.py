from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import cast

import matplotlib.pyplot as plt
import numpy as np
import pytest
from django.http import HttpResponse

from ndvi.engines.base import BBox
from ndvi.raster.base import RasterRequest
from ndvi.raster.png import NDVI_MAX, NDVI_MIN, ndvi_to_png_bytes
from ndvi.raster.stac_compute_engine import StacComputeRasterEngine
from ndvi.stac_client import (
    StacClient,
    StacItem,
    StacProcessingError,
    _validate_band_variation,
)
from ndvi.stac_client import (
    logger as stac_logger,
)


def _log_stage(stage: str, **values: object) -> None:
    print(
        f"[AUDIT] {stage}: ", " ".join(f"{k}={v}" for k, v in values.items())
    )


def _compute_ndvi(red: np.ndarray, nir: np.ndarray) -> np.ndarray:
    assert red.shape == nir.shape, "Band shape mismatch"
    assert red.ndim == 2, "RED must be single band"
    assert nir.ndim == 2, "NIR must be single band"
    _log_stage(
        "Band stats",
        red_min=red.min(),
        red_max=red.max(),
        red_dtype=red.dtype,
        nir_min=nir.min(),
        nir_max=nir.max(),
        nir_dtype=nir.dtype,
    )
    denom = nir + red + 1e-6
    ndvi = (nir - red) / denom
    assert not np.isnan(ndvi).all(), "NDVI is all NaN"
    _log_stage(
        "NDVI stats",
        ndvi_min=float(np.nanmin(ndvi)),
        ndvi_max=float(np.nanmax(ndvi)),
        ndvi_mean=float(np.nanmean(ndvi)),
    )
    assert ndvi.ndim == 2
    assert not np.isnan(ndvi).all(), "NDVI is all NaN"
    ndvi_min = float(np.nanmin(ndvi))
    ndvi_max = float(np.nanmax(ndvi))
    assert ndvi_min >= -1.1 and ndvi_max <= 1.1
    return ndvi


def test_ndvi_pipeline_audit_synthetic_random() -> None:
    np.random.seed(1)
    red = np.random.uniform(0.1, 0.7, (64, 64)).astype(np.float32)
    nir = red + np.random.uniform(0.05, 0.3, (64, 64)).astype(np.float32)
    ndvi = _compute_ndvi(red, nir)
    if np.isclose(ndvi.min(), ndvi.max()):
        pytest.skip("Synthetic NDVI lacks variation")
    _log_stage(
        "NDVI range", min_val=float(ndvi.min()), max_val=float(ndvi.max())
    )
    norm = (np.clip(ndvi, NDVI_MIN, NDVI_MAX) + 1.0) / 2.0
    p2, p98 = np.percentile(ndvi, (2, 98))
    stretched = np.clip((ndvi - p2) / (p98 - p2 + 1e-6), 0.0, 1.0)
    _log_stage("Percentiles", p2=float(p2), p98=float(p98))
    cmap = plt.get_cmap("RdYlGn")
    colored_raw = cmap(norm)
    colored_stretched = cmap(stretched)
    assert colored_raw.shape[2] == 4
    assert colored_stretched.shape[2] == 4
    rgb_raw = (colored_raw[:, :, :3] * 255).astype(np.uint8)
    rgb_stretched = (colored_stretched[:, :, :3] * 255).astype(np.uint8)
    assert not np.all(rgb_raw == rgb_raw[0, 0])
    assert not np.all(rgb_stretched == rgb_stretched[0, 0])
    png_bytes = ndvi_to_png_bytes(ndvi)
    _log_stage("PNG header", header=png_bytes[:8])
    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    response = HttpResponse(png_bytes, content_type="image/png")
    assert response["Content-Type"] == "image/png"
    assert len(response.content) > len(b"\x89PNG\r\n\x1a\n")
    _log_stage("HTTP response", length=len(response.content))
    _log_stage("Visual bytes", rgb_shape=rgb_raw.shape, dtype=rgb_raw.dtype)


def test_ndvi_pipeline_rejects_constant_ndvi() -> None:
    red = np.ones((8, 8), dtype=np.float32)
    nir = np.ones((8, 8), dtype=np.float32)
    ndvi = _compute_ndvi(red, nir)
    with pytest.raises(ValueError, match="no variation"):
        ndvi_to_png_bytes(ndvi)


def test_ndvi_pipeline_handles_nan_heavy_input() -> None:
    red = np.full((16, 16), np.nan, dtype=np.float32)
    nir = np.full((16, 16), np.nan, dtype=np.float32)
    red[0, 0] = 0.2
    nir[0, 0] = 0.4
    ndvi = _compute_ndvi(red, nir)
    png = ndvi_to_png_bytes(ndvi)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_validate_band_variation_raises_on_constant() -> None:
    arr = np.full((3, 3), 0.1, dtype=np.float32)
    with pytest.raises(StacProcessingError, match="Invalid test band"):
        _validate_band_variation(arr, "test")


def _stac_raster_request() -> RasterRequest:
    return RasterRequest(
        bbox=BBox(
            south=Decimal("0.0"),
            west=Decimal("0.0"),
            north=Decimal("0.1"),
            east=Decimal("0.1"),
        ),
        date=date(2025, 1, 1),
        size=16,
        max_cloud=20,
        engine="stac",
        job_id=1,
        farm_id=1,
    )


def _stac_item(
    *,
    collection: str | None = None,
    item_id: str = "item-1",
    cloud_cover: float = 5.0,
) -> StacItem:
    return StacItem(
        id=item_id,
        datetime=datetime(2025, 1, 1),
        assets={"B04": "red.tif", "B08": "nir.tif"},
        cloud_cover=cloud_cover,
        collection=collection,
    )


def test_ndvi_raster_logging(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeClient:
        def search(
            self,
            *,
            bbox: object,
            start: date,
            end: date,
            max_cloud: int,
        ) -> list[StacItem]:
            return [_stac_item()]

    def fake_load_ndvi_array(**kwargs: object) -> np.ndarray:
        red = np.array([[0.2]], dtype=np.float32)
        nir = np.array([[0.6]], dtype=np.float32)
        denom = nir + red + 1e-6
        ndvi = (nir - red) / denom
        stac_logger.info(
            "Bands | NIR min=%s max=%s | RED min=%s max=%s",
            float(nir.min()),
            float(nir.max()),
            float(red.min()),
            float(red.max()),
        )
        return ndvi

    monkeypatch.setattr(
        "ndvi.raster.stac_compute_engine.load_ndvi_array",
        fake_load_ndvi_array,
    )

    engine = StacComputeRasterEngine(
        client=cast(StacClient, FakeClient()),
    )
    caplog.set_level(logging.INFO)
    caplog.clear()

    with pytest.raises(ValueError, match="no variation"):
        engine.render_png(_stac_raster_request())
    assert any("Bands |" in record.message for record in caplog.records)
    assert any("NDVI stats |" in record.message for record in caplog.records)
    assert any(
        "NDVI percentiles |" in record.message for record in caplog.records
    )
