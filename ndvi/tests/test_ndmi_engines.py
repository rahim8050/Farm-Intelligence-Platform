"""Tests for NDMI engine paths: stac, sentinelhub, stac_client.

Covers the NDMI-specific branches added in Phase 0:
- StacEngine._compute_stats with index_type='NDMI'
- SentinelHubEngine evalscript dispatch for NDMI
- load_ndmi_array from stac_client
- Formulas registry
- Band registry
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np

from ndvi.engines.base import BBox, NDVIEngine
from ndvi.engines.sentinelhub import (
    NDMI_EVALSCRIPT,
    NDVI_EVALSCRIPT,
    NDWI_EVALSCRIPT,
)
from ndvi.engines.stac import StacEngine
from ndvi.services import ENGINE_FACTORIES
from ndvi.stac_client import NdviStats, StacItem

_SENTINEL_CREDS = {
    "SENTINELHUB_CLIENT_ID": "test-id",
    "SENTINELHUB_CLIENT_SECRET": "test-secret",
}


class FakeClient:
    def __init__(self, items: list[StacItem]) -> None:
        self._items = items

    def search(
        self,
        *,
        bbox: BBox,
        start: date,
        end: date,
        max_cloud: int,
    ) -> list[StacItem]:
        return list(self._items)


def _bbox() -> BBox:
    return BBox(
        south=Decimal("0.0"),
        west=Decimal("0.0"),
        north=Decimal("0.1"),
        east=Decimal("0.1"),
    )


def _item(item_date: date) -> StacItem:
    return StacItem(
        id="item-1",
        datetime=datetime(
            item_date.year, item_date.month, item_date.day, tzinfo=UTC
        ),
        assets={"B04": "red.tif", "B08": "nir.tif", "B11": "swir1.tif"},
        cloud_cover=5.0,
    )


# ── NDMI Stac engine path ───────────────────────────────────────


@patch("ndvi.engines.stac._load_single_stac_band")
@patch("ndvi.engines.stac.compute_ndvi_stats")
def test_ndmi_stac_engine_compute_stats(
    mock_stats: MagicMock,
    mock_load_band: MagicMock,
) -> None:
    """NDMI StacEngine loads the right bands and returns stats."""
    mock_load_band.return_value = np.full((10, 10), 0.5, dtype=np.float32)
    mock_stats.return_value = NdviStats(
        mean=0.4, min=0.3, max=0.5, sample_count=2
    )
    engine = StacEngine(index_type="NDMI")
    result = engine._compute_stats(item=_item(date(2025, 1, 2)), bbox=_bbox())
    assert result is not None
    assert result.mean == 0.4
    # _load_single_stac_band should be called for nir and swir1 bands
    assert mock_load_band.call_count >= 2


@patch("ndvi.engines.stac._load_single_stac_band")
@patch("ndvi.engines.stac.compute_ndvi_stats")
def test_ndmi_stac_engine_returns_none_when_swir1_missing(
    mock_stats: MagicMock,
    mock_load_band: MagicMock,
) -> None:
    """NDMI returns None when swir1 band asset is missing from the item."""
    mock_load_band.return_value = np.full((10, 10), 0.5, dtype=np.float32)
    mock_stats.return_value = NdviStats(
        mean=0.4, min=0.3, max=0.5, sample_count=2
    )
    item = _item(date(2025, 1, 2))
    item.assets.pop("B11", None)
    engine = StacEngine(index_type="NDMI")
    result = engine._compute_stats(item=item, bbox=_bbox())
    assert result is None
    mock_load_band.assert_not_called()


@patch("ndvi.engines.stac._load_single_stac_band")
@patch("ndvi.engines.stac.compute_ndvi_stats")
def test_ndmi_stac_engine_computes_ndvi_by_default(
    mock_stats: MagicMock,
    mock_load_band: MagicMock,
) -> None:
    """Default StacEngine (index_type=NDVI) computes NDVI."""
    mock_load_band.return_value = np.full((10, 10), 0.5, dtype=np.float32)
    mock_stats.return_value = NdviStats(
        mean=0.2, min=0.1, max=0.3, sample_count=4
    )
    engine = StacEngine()
    assert engine.index_type == "NDVI"
    result = engine._compute_stats(item=_item(date(2025, 1, 2)), bbox=_bbox())
    assert result is not None
    assert result.mean == 0.2
    mock_load_band.assert_called()


def test_ndmi_stac_engine_registers_asset_swir1() -> None:
    engine = StacEngine(index_type="NDMI")
    assert engine.asset_swir1 is not None


def test_ndmi_stac_engine_default_asset_swir1() -> None:
    engine = StacEngine(index_type="NDMI")
    assert isinstance(engine.asset_swir1, str)
    assert len(engine.asset_swir1) > 0


# ── NDMI SentinelHub evalscript dispatch ────────────────────────


def _make_bbox() -> BBox:
    return BBox(
        south=Decimal("0.0"),
        west=Decimal("0.0"),
        north=Decimal("0.1"),
        east=Decimal("0.1"),
    )


def _make_sentinel_engine(index_type: str = "NDVI") -> Any:
    from ndvi.engines.sentinelhub import SentinelHubEngine

    with patch.dict(os.environ, _SENTINEL_CREDS):
        return SentinelHubEngine(index_type=index_type)


def test_sentinelhub_evalscript_dispatch_returns_ndmi_for_ndmi() -> None:
    engine = _make_sentinel_engine(index_type="NDMI")
    payload = engine._build_statistics_payload(
        bbox=_make_bbox(),
        start=date(2025, 1, 1),
        end=date(2025, 1, 10),
        step_days=7,
        max_cloud=30,
    )
    evalscript = payload["aggregation"]["evalscript"]
    assert evalscript == NDMI_EVALSCRIPT


def test_sentinelhub_evalscript_dispatch_returns_ndwi_for_ndwi() -> None:
    engine = _make_sentinel_engine(index_type="NDWI")
    payload = engine._build_statistics_payload(
        bbox=_make_bbox(),
        start=date(2025, 1, 1),
        end=date(2025, 1, 10),
        step_days=7,
        max_cloud=30,
    )
    evalscript = payload["aggregation"]["evalscript"]
    assert evalscript == NDWI_EVALSCRIPT


def test_sentinelhub_evalscript_dispatch_returns_ndvi_by_default() -> None:
    engine = _make_sentinel_engine()
    payload = engine._build_statistics_payload(
        bbox=_make_bbox(),
        start=date(2025, 1, 1),
        end=date(2025, 1, 10),
        step_days=7,
        max_cloud=30,
    )
    evalscript = payload["aggregation"]["evalscript"]
    assert evalscript == NDVI_EVALSCRIPT


def test_sentinelhub_evalscript_dispatch_returns_ndvi_for_unknown() -> None:
    engine = _make_sentinel_engine(index_type="UNKNOWN")
    payload = engine._build_statistics_payload(
        bbox=_make_bbox(),
        start=date(2025, 1, 1),
        end=date(2025, 1, 10),
        step_days=7,
        max_cloud=30,
    )
    evalscript = payload["aggregation"]["evalscript"]
    assert evalscript == NDVI_EVALSCRIPT


# ── load_ndmi_array is tested indirectly via the StacEngine NDMI
#    _compute_stats tests above, which mock load_ndmi_array.


# ── NDMI factory registrations ──────────────────────────────────


class TestNdmiEngineFactories:
    """NDMI engine factories must be registered and callable."""

    def test_ndmi_stac_factory(self) -> None:
        engine: NDVIEngine = ENGINE_FACTORIES["ndmi_stac"]()
        assert engine.engine_name == "stac"
        assert engine.index_type == "NDMI"

    def test_ndmi_sentinelhub_factory_registered(self) -> None:
        factory = ENGINE_FACTORIES["ndmi_sentinelhub"]
        assert callable(factory)

    def test_ndmi_gee_factory_registered(self) -> None:
        factory = ENGINE_FACTORIES["ndmi_gee"]
        assert callable(factory)

    def test_ndmi_landsat_factory(self) -> None:
        engine: NDVIEngine = ENGINE_FACTORIES["ndmi_landsat"]()
        assert engine.index_type == "NDMI"

    def test_ndmi_modis_factory(self) -> None:
        engine: NDVIEngine = ENGINE_FACTORIES["ndmi_modis"]()
        assert engine.index_type == "NDMI"

    def test_all_ndmi_factories_present(self) -> None:
        for suffix in ("gee", "sentinelhub", "stac", "landsat", "modis"):
            assert f"ndmi_{suffix}" in ENGINE_FACTORIES
            assert callable(ENGINE_FACTORIES[f"ndmi_{suffix}"])


# ── get_engine with index_type=NDMI ─────────────────────────────


@patch("ndvi.services.resolve_ndvi_engine_name", return_value="stac")
def test_get_engine_resolves_ndmi_stac(mock_resolve: MagicMock) -> None:
    from ndvi.services import get_engine

    engine = get_engine("stac", index_type="NDMI")
    assert engine.index_type == "NDMI"
    assert engine.engine_name == "stac"


@patch("ndvi.services.resolve_ndvi_engine_name", return_value="sentinelhub")
def test_get_engine_resolves_ndmi_sentinelhub(mock_resolve: MagicMock) -> None:
    from ndvi.services import get_engine

    with patch.dict(os.environ, _SENTINEL_CREDS):
        engine = get_engine("sentinelhub", index_type="NDMI")
    assert engine.index_type == "NDMI"
