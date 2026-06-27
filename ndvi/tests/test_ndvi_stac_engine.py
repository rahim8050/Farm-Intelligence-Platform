from __future__ import annotations

# ruff: noqa: S101
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from django.test import override_settings

import ndvi.engines.stac as stac_engine_module
from ndvi.engines.base import BBox
from ndvi.engines.stac import StacEngine, get_default_max_cloud
from ndvi.stac_client import NdviStats, StacClient, StacItem


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
            item_date.year,
            item_date.month,
            item_date.day,
            tzinfo=UTC,
        ),
        assets={"B04": "red.tif", "B08": "nir.tif"},
        cloud_cover=5.0,
    )


@override_settings(NDVI_STAC_MAX_CLOUD_DEFAULT=42)
def test_get_default_max_cloud_uses_settings() -> None:
    assert get_default_max_cloud() == 42


def test_stac_engine_timeseries_caches_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [_item(date(2025, 1, 2))]
    engine = StacEngine(
        client=cast(StacClient, FakeClient(items)),
        date_window_days=2,
    )
    calls: list[str] = []

    def fake_compute_stats(item: StacItem, bbox: BBox) -> NdviStats:
        calls.append(item.id)
        return NdviStats(mean=0.2, min=0.1, max=0.3, sample_count=4)

    monkeypatch.setattr(engine, "_compute_stats", fake_compute_stats)
    points = engine.get_timeseries(
        bbox=_bbox(),
        start=date(2025, 1, 1),
        end=date(2025, 1, 3),
        step_days=2,
        max_cloud=20,
    )
    assert len(points) == 2
    assert calls == ["item-1"]


def test_stac_engine_get_timeseries_returns_empty_when_no_items() -> None:
    engine = StacEngine(client=cast(StacClient, FakeClient([])))
    points = engine.get_timeseries(
        bbox=_bbox(),
        start=date(2025, 1, 1),
        end=date(2025, 1, 3),
        step_days=2,
        max_cloud=20,
    )
    assert points == []


def test_stac_engine_get_timeseries_skips_when_stats_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [_item(date(2025, 1, 2))]
    engine = StacEngine(client=cast(StacClient, FakeClient(items)))
    monkeypatch.setattr(engine, "_compute_stats", lambda *_: None)
    points = engine.get_timeseries(
        bbox=_bbox(),
        start=date(2025, 1, 1),
        end=date(2025, 1, 3),
        step_days=2,
        max_cloud=20,
    )
    assert points == []


def test_stac_engine_get_latest_returns_none_when_missing_item() -> None:
    engine = StacEngine(client=cast(StacClient, FakeClient([])))
    assert (
        engine.get_latest(
            bbox=_bbox(),
            lookback_days=7,
            max_cloud=20,
        )
        is None
    )


def test_stac_engine_get_latest_returns_none_when_stats_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [_item(date.today())]
    engine = StacEngine(client=cast(StacClient, FakeClient(items)))
    monkeypatch.setattr(engine, "_compute_stats", lambda *_: None)
    assert (
        engine.get_latest(
            bbox=_bbox(),
            lookback_days=7,
            max_cloud=20,
        )
        is None
    )


def test_stac_engine_get_latest_returns_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [_item(date.today())]
    engine = StacEngine(client=cast(StacClient, FakeClient(items)))

    def fake_compute_stats(_item: StacItem, _bbox: BBox) -> NdviStats:
        return NdviStats(mean=0.2, min=0.1, max=0.3, sample_count=4)

    monkeypatch.setattr(engine, "_compute_stats", fake_compute_stats)
    point = engine.get_latest(
        bbox=_bbox(),
        lookback_days=7,
        max_cloud=20,
    )
    assert point is not None
    assert point.mean == 0.2


def test_stac_engine_compute_stats_missing_assets_returns_none() -> None:
    engine = StacEngine(client=cast(StacClient, FakeClient([])))
    item = StacItem(
        id="missing",
        datetime=datetime(2025, 1, 2, tzinfo=UTC),
        assets={"B04": "red.tif"},
        cloud_cover=5.0,
    )
    assert engine._compute_stats(item, _bbox()) is None


def test_stac_engine_compute_stats_returns_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = StacEngine(client=cast(StacClient, FakeClient([])))

    def fake_load_band(*_args: object, **_kwargs: object) -> np.ndarray:
        return np.array([[0.2]], dtype=np.float32)

    def fake_compute_ndvi_stats(_ndvi: np.ndarray) -> NdviStats:
        return NdviStats(mean=0.2, min=0.1, max=0.3, sample_count=1)

    monkeypatch.setattr(
        stac_engine_module, "_load_single_stac_band", fake_load_band
    )
    monkeypatch.setattr(
        stac_engine_module, "compute_ndvi_stats", fake_compute_ndvi_stats
    )

    item = _item(date(2025, 1, 2))
    stats = engine._compute_stats(item, _bbox())
    assert stats is not None
    assert stats.sample_count == 1


@patch("ndvi.engines.stac._load_single_stac_band")
def test_stac_engine_ndvi_loader(
    mock_load_band: MagicMock,
) -> None:
    """StacEngine loads NDVI using generic _load_stac_index_array."""
    mock_load_band.return_value = np.full((10, 10), 0.5, dtype=np.float32)
    engine = StacEngine()
    item = StacItem(
        id="ndvi-item",
        datetime=datetime(2025, 1, 2, tzinfo=UTC),
        assets={"B04": "red.tif", "B08": "nir.tif"},
        cloud_cover=5.0,
    )

    array = stac_engine_module._load_stac_index_array(engine, item, _bbox())
    assert array.size > 0


@patch("ndvi.engines.stac.compute_ndvi_stats", return_value=None)
@patch("ndvi.engines.stac._load_single_stac_band")
def test_stac_engine_compute_stats_returns_none_when_stats_missing(
    mock_load_band: MagicMock,
    mock_stats: MagicMock,
) -> None:
    mock_load_band.return_value = np.full((10, 10), 0.5, dtype=np.float32)
    engine = StacEngine(client=cast(StacClient, FakeClient([])))
    item = _item(date(2025, 1, 2))

    assert engine._compute_stats(item, _bbox()) is None
    mock_load_band.assert_called()


def test_stac_engine_compute_stats_unknown_index_returns_none() -> None:
    """Unknown index type returns None (no more ValueError)."""
    engine = StacEngine(index_type="UNKNOWN")
    item = _item(date(2025, 1, 2))

    result = engine._compute_stats(item, _bbox())
    assert result is None


# ── Coverage tests for _load_stac_index_array edge cases ─────────


@patch("ndvi.engines.stac._load_single_stac_band")
def test_stac_load_index_array_unknown_index(
    mock_load_band: MagicMock,
) -> None:
    """Unknown index type logs warning and returns empty array."""
    engine = StacEngine(index_type="UNKNOWN_INDEX")
    item = _item(date(2025, 1, 2))
    from ndvi.engines.stac import _load_stac_index_array

    result = _load_stac_index_array(engine, item, _bbox())
    assert result.size == 0
    mock_load_band.assert_not_called()


@patch("ndvi.engines.stac._load_single_stac_band")
def test_stac_load_index_array_missing_band_href(
    mock_load_band: MagicMock,
) -> None:
    """When a required band href cannot be resolved, returns empty."""
    engine = StacEngine()
    # Item without the nir asset triggers the missing band href path
    item = StacItem(
        id="missing-nir",
        datetime=datetime(2025, 1, 2, tzinfo=UTC),
        assets={"B04": "red.tif"},
        cloud_cover=5.0,
    )
    from ndvi.engines.stac import _load_stac_index_array

    result = _load_stac_index_array(engine, item, _bbox())
    assert result.size == 0
    mock_load_band.assert_not_called()


@patch("ndvi.engines.stac._load_single_stac_band")
def test_stac_load_index_array_empty_band_returns_empty(
    mock_load_band: MagicMock,
) -> None:
    """Empty band array returns empty."""
    mock_load_band.return_value = np.array([])
    engine = StacEngine()
    item = _item(date(2025, 1, 2))
    from ndvi.engines.stac import _load_stac_index_array

    result = _load_stac_index_array(engine, item, _bbox())
    assert result.size == 0


@patch("ndvi.stac_client.apply_scl_mask")
@patch("ndvi.engines.stac.compute_index")
@patch("ndvi.engines.stac._load_single_stac_band")
def test_stac_load_index_array_applies_scl_mask(
    mock_load_band: MagicMock,
    mock_compute: MagicMock,
    mock_apply_scl: MagicMock,
) -> None:
    """SCL masking is applied when SCL band is available."""
    mock_load_band.return_value = np.full((10, 10), 0.5, dtype=np.float32)
    mock_compute.return_value = np.full((10, 10), 0.5, dtype=np.float32)
    mock_apply_scl.return_value = (
        np.full((10, 10), 0.5, dtype=np.float32),
        None,
        None,
    )
    engine = StacEngine()
    item = StacItem(
        id="scl-item",
        datetime=datetime(2025, 1, 2, tzinfo=UTC),
        assets={"B04": "red.tif", "B08": "nir.tif", "SCL": "scl.tif"},
        cloud_cover=5.0,
    )
    from ndvi.engines.stac import _load_stac_index_array

    result = _load_stac_index_array(engine, item, _bbox())
    assert result.size > 0
    mock_apply_scl.assert_called_once()


@patch("ndvi.stac_client.apply_scl_mask")
@patch("ndvi.engines.stac.compute_index")
@patch("ndvi.engines.stac._load_single_stac_band")
def test_stac_load_index_array_empty_after_masking(
    mock_load_band: MagicMock,
    mock_compute: MagicMock,
    mock_apply_scl: MagicMock,
) -> None:
    """Index array emptied by SCL masking returns empty."""
    mock_load_band.return_value = np.full((10, 10), 0.5, dtype=np.float32)
    mock_compute.return_value = np.full((10, 10), 0.5, dtype=np.float32)
    mock_apply_scl.return_value = (np.array([]), None, None)
    engine = StacEngine()
    item = StacItem(
        id="empty-after-mask",
        datetime=datetime(2025, 1, 2, tzinfo=UTC),
        assets={"B04": "red.tif", "B08": "nir.tif", "SCL": "scl.tif"},
        cloud_cover=5.0,
    )
    from ndvi.engines.stac import _load_stac_index_array

    result = _load_stac_index_array(engine, item, _bbox())
    assert result.size == 0


# ── Coverage tests for _load_single_stac_band ────────────────────


@patch("rasterio.open")
@patch("httpx.Client")
def test_load_single_stac_band_local_file(
    mock_httpx_client: MagicMock,
    mock_rasterio_open: MagicMock,
    tmp_path: Path,
) -> None:
    """Local file path skips HTTP download."""
    from ndvi.engines.stac import _load_single_stac_band

    tif_path = tmp_path / "test.tif"
    tif_path.write_bytes(b"dummy")
    mock_src = MagicMock()
    mock_src.crs = "EPSG:4326"
    mock_src.height = 100
    mock_src.width = 100
    mock_src.read.return_value = np.full((100, 100), 0.5, dtype=np.float32)
    mock_rasterio_open.return_value.__enter__.return_value = mock_src

    result = _load_single_stac_band(str(tif_path), _bbox())
    assert result.size > 0
    mock_httpx_client.assert_not_called()


@patch("rasterio.open")
@patch("httpx.Client")
def test_load_single_stac_band_no_crs(
    mock_httpx_client: MagicMock,
    mock_rasterio_open: MagicMock,
    tmp_path: Path,
) -> None:
    """Missing CRS returns empty array."""
    from ndvi.engines.stac import _load_single_stac_band

    tif_path = tmp_path / "nocrs.tif"
    tif_path.write_bytes(b"dummy")
    mock_src = MagicMock()
    mock_src.crs = None
    mock_rasterio_open.return_value.__enter__.return_value = mock_src

    result = _load_single_stac_band(str(tif_path), _bbox())
    assert result.size == 0


@patch("rasterio.open")
@patch("httpx.Client")
def test_load_single_stac_band_http_download(
    mock_httpx_client: MagicMock,
    mock_rasterio_open: MagicMock,
    tmp_path: Path,
) -> None:
    """HTTP download path writes to temp dir and reads with rasterio."""
    from ndvi.engines.stac import _load_single_stac_band

    mock_response = MagicMock()
    mock_response.content = b"fakedata"
    mock_client_instance = MagicMock()
    mock_client_instance.get.return_value = mock_response
    mock_httpx_client.return_value = mock_client_instance

    mock_src = MagicMock()
    mock_src.crs = "EPSG:4326"
    mock_src.height = 100
    mock_src.width = 100
    mock_src.read.return_value = np.full((100, 100), 0.5, dtype=np.float32)
    mock_rasterio_open.return_value.__enter__.return_value = mock_src

    result = _load_single_stac_band(
        "https://example.com/band.tif?token=abc", _bbox()
    )
    assert result.size > 0
    mock_httpx_client.assert_called_once()
    mock_client_instance.get.assert_called_once_with(
        "https://example.com/band.tif?token=abc"
    )
