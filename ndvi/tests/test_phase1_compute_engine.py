"""Tests for Phase 1: SpectralComputeEngine and StacDataProvider.

Covers:
- SpectralComputeEngine construction and compute()
- StacDataProvider search / load_band / get_latest
- Formula/band registry integration
- NDVIEngine protocol compliance
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from ndvi.engines.base import BBox, NdviPoint
from ndvi.engines.compute import SpectralComputeEngine
from ndvi.providers.base import DataProvider
from ndvi.providers.stac import (
    StacDataProvider,
    _download_asset,
    _is_remote_href,
    _load_single_band,
)
from ndvi.stac_client import StacItem
from science.formulas.band_registry import BAND_REGISTRY
from science.formulas.registry import FORMULA_REGISTRY


def _bbox() -> BBox:
    return BBox(
        south=Decimal("0.0"),
        west=Decimal("0.0"),
        north=Decimal("0.1"),
        east=Decimal("0.1"),
    )


def _make_item(
    item_id: str = "test-item",
    cloud_cover: float = 5.0,
    dt: datetime | None = None,
) -> StacItem:
    return StacItem(
        id=item_id,
        datetime=dt or datetime(2026, 6, 1, tzinfo=UTC),
        assets={
            "B08_10m": "http://fake/nir.tif",
            "B04_10m": "http://fake/red.tif",
        },
        cloud_cover=cloud_cover,
    )


# ──────────────────────────────────────────────
# FakeProvider for unit testing SpectralComputeEngine
# ──────────────────────────────────────────────


class FakeProvider:
    """Minimal DataProvider that returns canned items and band arrays."""

    sensor_key: str = "sentinel2_l2a"

    def __init__(
        self,
        items: list[StacItem] | None = None,
        band_array: np.ndarray | None = None,
    ) -> None:
        if items is None:
            self._items = [_make_item()]
        else:
            self._items = items
        self._band_array = (
            band_array
            if band_array is not None
            else np.ones((10, 10), dtype=np.float32)
        )

    def search(
        self,
        bbox: BBox,
        start: date,
        end: date,
        max_cloud: int,
    ) -> list[StacItem]:
        return list(self._items)

    def load_band(
        self,
        item: StacItem,
        band_asset_key: str,
        bbox: BBox,
    ) -> np.ndarray:
        return self._band_array.copy()

    def get_latest(
        self,
        bbox: BBox,
        lookback_days: int,
        max_cloud: int,
    ) -> StacItem | None:
        if self._items:
            return self._items[-1]
        return None


class FakeMultiBandProvider:
    """Provider that returns arrays per band name for formula tests."""

    sensor_key: str = "sentinel2_l2a"

    def __init__(self, items: list[StacItem] | None = None) -> None:
        if items is None:
            self._items = [_make_item()]
        else:
            self._items = items
        # nir = 0.8, red = 0.2 => NDVI = (0.8-0.2)/(0.8+0.2) = 0.6
        self._band_data: dict[str, np.ndarray] = {
            "B08_10m": np.full((10, 10), 0.8, dtype=np.float32),
            "B04_10m": np.full((10, 10), 0.2, dtype=np.float32),
            "B03_10m": np.full((10, 10), 0.5, dtype=np.float32),
            "B11_20m": np.full((10, 10), 0.3, dtype=np.float32),
        }

    def search(
        self,
        bbox: BBox,
        start: date,
        end: date,
        max_cloud: int,
    ) -> list[StacItem]:
        return list(self._items)

    def load_band(
        self,
        item: StacItem,
        band_asset_key: str,
        bbox: BBox,
    ) -> np.ndarray:
        if band_asset_key in self._band_data:
            return self._band_data[band_asset_key].copy()
        return np.array([])

    def get_latest(
        self,
        bbox: BBox,
        lookback_days: int,
        max_cloud: int,
    ) -> StacItem | None:
        if self._items:
            return self._items[-1]
        return None


class TestSpectralComputeEngine:
    """Tests for the generic spectral compute engine."""

    def test_construct_with_provider_and_formula(self) -> None:
        """Engine can be constructed with a provider and formula."""
        provider = FakeProvider()
        formula = FORMULA_REGISTRY["NDVI"]
        engine = SpectralComputeEngine(provider=provider, formula=formula)
        assert engine.index_type == "NDVI"
        assert "sentinel2_l2a" in engine.engine_name

    def test_construct_ndwi_formula(self) -> None:
        """Engine works with NDWI formula."""
        provider = FakeProvider()
        formula = FORMULA_REGISTRY["NDWI"]
        engine = SpectralComputeEngine(provider=provider, formula=formula)
        assert engine.index_type == "NDWI"

    def test_construct_ndmi_formula(self) -> None:
        """Engine works with NDMI formula."""
        provider = FakeProvider()
        formula = FORMULA_REGISTRY["NDMI"]
        engine = SpectralComputeEngine(provider=provider, formula=formula)
        assert engine.index_type == "NDMI"

    def test_compute_returns_ndvi_points(self) -> None:
        """Compute returns a list of NdviPoints."""
        provider = FakeMultiBandProvider()
        formula = FORMULA_REGISTRY["NDVI"]
        engine = SpectralComputeEngine(provider=provider, formula=formula)

        points = engine.compute(
            bbox=_bbox(),
            start=date(2026, 6, 1),
            end=date(2026, 6, 1),
            step_days=5,
            max_cloud=30,
        )
        assert len(points) >= 1
        point = points[0]
        assert isinstance(point, NdviPoint)
        assert point.date == date(2026, 6, 1)
        assert point.mean is not None

    def test_ndvi_compute_expected_value(self) -> None:
        """NDVI with nir=0.8, red=0.2 gives expected mean of 0.6."""
        provider = FakeMultiBandProvider()
        formula = FORMULA_REGISTRY["NDVI"]
        engine = SpectralComputeEngine(provider=provider, formula=formula)

        points = engine.compute(
            bbox=_bbox(),
            start=date(2026, 6, 1),
            end=date(2026, 6, 1),
            step_days=5,
            max_cloud=30,
        )
        assert len(points) >= 1
        assert points[0].mean == pytest.approx(0.6, abs=1e-5)

    def test_ndwi_compute_expected_value(self) -> None:
        """NDWI with green=0.5 and nir=0.8 gives -0.2307..."""
        provider = FakeMultiBandProvider()
        formula = FORMULA_REGISTRY["NDWI"]
        engine = SpectralComputeEngine(provider=provider, formula=formula)

        points = engine.compute(
            bbox=_bbox(),
            start=date(2026, 6, 1),
            end=date(2026, 6, 1),
            step_days=5,
            max_cloud=30,
        )
        assert len(points) >= 1
        expected = (0.5 - 0.8) / (0.5 + 0.8)
        assert points[0].mean == pytest.approx(expected, abs=1e-5)

    def test_ndmi_compute_expected_value(self) -> None:
        """NDMI with nir=0.8 and swir1=0.3 gives 0.4545..."""
        provider = FakeMultiBandProvider()
        formula = FORMULA_REGISTRY["NDMI"]
        engine = SpectralComputeEngine(provider=provider, formula=formula)

        points = engine.compute(
            bbox=_bbox(),
            start=date(2026, 6, 1),
            end=date(2026, 6, 1),
            step_days=5,
            max_cloud=30,
        )
        assert len(points) >= 1
        expected = (0.8 - 0.3) / (0.8 + 0.3)
        assert points[0].mean == pytest.approx(expected, abs=1e-5)

    def test_compute_empty_items_returns_empty(self) -> None:
        """No items found returns empty list."""
        provider = FakeProvider(items=[])
        formula = FORMULA_REGISTRY["NDVI"]
        engine = SpectralComputeEngine(provider=provider, formula=formula)

        points = engine.compute(
            bbox=_bbox(),
            start=date(2026, 6, 1),
            end=date(2026, 6, 10),
            step_days=5,
            max_cloud=30,
        )
        assert points == []

    def test_get_latest_returns_point(self) -> None:
        """get_latest returns an NdviPoint."""
        provider = FakeMultiBandProvider()
        formula = FORMULA_REGISTRY["NDVI"]
        engine = SpectralComputeEngine(provider=provider, formula=formula)

        point = engine.get_latest(
            bbox=_bbox(),
            lookback_days=30,
            max_cloud=30,
        )
        assert point is not None
        assert isinstance(point, NdviPoint)
        assert point.mean == pytest.approx(0.6, abs=1e-5)

    def test_get_latest_no_items_returns_none(self) -> None:
        """get_latest returns None when no items found."""
        provider = FakeProvider(items=[])
        formula = FORMULA_REGISTRY["NDVI"]
        engine = SpectralComputeEngine(provider=provider, formula=formula)

        point = engine.get_latest(
            bbox=_bbox(),
            lookback_days=30,
            max_cloud=30,
        )
        assert point is None

    def test_implements_ndvi_engine_protocol(self) -> None:
        """SpectralComputeEngine satisfies the NDVIEngine protocol."""
        provider = FakeProvider()
        formula = FORMULA_REGISTRY["NDVI"]
        engine = SpectralComputeEngine(provider=provider, formula=formula)
        # Duck-type check: verify the engine has the required attributes
        assert hasattr(engine, "get_timeseries")
        assert hasattr(engine, "get_latest")
        assert hasattr(engine, "engine_name")
        assert hasattr(engine, "index_type")

    def test_get_timeseries_delegates_to_compute(self) -> None:
        """get_timeseries returns same result as compute."""
        provider = FakeMultiBandProvider()
        formula = FORMULA_REGISTRY["NDVI"]
        engine = SpectralComputeEngine(provider=provider, formula=formula)

        compute_points = engine.compute(
            bbox=_bbox(),
            start=date(2026, 6, 1),
            end=date(2026, 6, 1),
            step_days=5,
            max_cloud=30,
        )
        ts_points = engine.get_timeseries(
            bbox=_bbox(),
            start=date(2026, 6, 1),
            end=date(2026, 6, 1),
            step_days=5,
            max_cloud=30,
        )
        assert len(compute_points) == len(ts_points)
        for cp, tp in zip(compute_points, ts_points, strict=True):
            assert cp.mean == tp.mean

    def test_iter_buckets(self) -> None:
        """_iter_buckets produces correct date list."""
        provider = FakeProvider()
        formula = FORMULA_REGISTRY["NDVI"]
        engine = SpectralComputeEngine(provider=provider, formula=formula)

        buckets = engine._iter_buckets(
            date(2026, 6, 1), date(2026, 6, 11), step_days=5
        )
        assert buckets == [
            date(2026, 6, 1),
            date(2026, 6, 6),
            date(2026, 6, 11),
        ]

    def test_unknown_sensor_key(self) -> None:
        """Unknown sensor key returns None for _compute_for_item."""
        provider = FakeProvider()
        provider.sensor_key = "unknown_sensor"
        formula = FORMULA_REGISTRY["NDVI"]
        engine = SpectralComputeEngine(provider=provider, formula=formula)

        item = _make_item()
        result = engine._compute_for_item(item, _bbox(), date(2026, 6, 1))
        assert result is None

    def test_empty_band_array(self) -> None:
        """When load_band returns empty array, item is skipped."""
        provider = FakeProvider(band_array=np.array([]))
        formula = FORMULA_REGISTRY["NDVI"]
        engine = SpectralComputeEngine(provider=provider, formula=formula)

        points = engine.compute(
            bbox=_bbox(),
            start=date(2026, 6, 1),
            end=date(2026, 6, 1),
            step_days=5,
            max_cloud=30,
        )
        assert points == []

    def test_all_nan_array_returns_none(self) -> None:
        """When computed index is all NaN, _compute_for_item returns None."""
        provider = FakeProvider(
            band_array=np.full((10, 10), np.nan, dtype=np.float32)
        )
        formula = FORMULA_REGISTRY["NDVI"]
        engine = SpectralComputeEngine(provider=provider, formula=formula)
        item = _make_item()
        result = engine._compute_for_item(item, _bbox(), date(2026, 6, 1))
        assert result is None

    def test_get_timeseries_uses_provider_window(self) -> None:
        """get_timeseries delegates to compute with correct args."""
        provider = FakeMultiBandProvider(
            items=[
                _make_item(
                    item_id="item1",
                    dt=datetime(2026, 6, 1, tzinfo=UTC),
                )
            ]
        )
        formula = FORMULA_REGISTRY["NDVI"]
        engine = SpectralComputeEngine(provider=provider, formula=formula)
        points = engine.get_timeseries(
            bbox=_bbox(),
            start=date(2026, 6, 1),
            end=date(2026, 6, 1),
            step_days=5,
            max_cloud=30,
        )
        assert len(points) == 1
        assert points[0].date == date(2026, 6, 1)


class TestStacDataProvider:
    """Tests for the STAC data provider."""

    def test_construct_defaults(self) -> None:
        """StacDataProvider can be constructed with defaults."""
        provider = StacDataProvider()
        assert provider.sensor_key == "sentinel2_l2a"
        assert provider.timeout_seconds == 30.0

    def test_construct_custom(self) -> None:
        """StacDataProvider accepts custom parameters."""
        provider = StacDataProvider(
            sensor_key="landsat89_l2",
            timeout_seconds=60.0,
        )
        assert provider.sensor_key == "landsat89_l2"
        assert provider.timeout_seconds == 60.0

    @patch("ndvi.providers.stac.StacClient.search")
    def test_search_delegates_to_client(self, mock_search: MagicMock) -> None:
        """search delegates to StacClient.search."""
        items = [_make_item()]
        mock_search.return_value = items
        provider = StacDataProvider()
        result = provider.search(
            bbox=_bbox(),
            start=date(2026, 1, 1),
            end=date(2026, 1, 10),
            max_cloud=30,
        )
        assert result == items
        mock_search.assert_called_once()

    @patch("ndvi.providers.stac.StacClient.search")
    def test_get_latest_calls_search_and_select_best(
        self, mock_search: MagicMock
    ) -> None:
        """get_latest searches and selects best item."""
        items = [_make_item(dt=datetime(2026, 6, 1, tzinfo=UTC))]
        mock_search.return_value = items
        provider = StacDataProvider()
        result = provider.get_latest(
            bbox=_bbox(),
            lookback_days=30,
            max_cloud=30,
        )
        assert result is not None
        assert result.id == "test-item"
        mock_search.assert_called_once()

    @patch("ndvi.providers.stac.StacClient.search")
    def test_get_latest_no_items_returns_none(
        self, mock_search: MagicMock
    ) -> None:
        """get_latest returns None when no items found."""
        mock_search.return_value = []
        provider = StacDataProvider()
        result = provider.get_latest(
            bbox=_bbox(),
            lookback_days=30,
            max_cloud=30,
        )
        assert result is None

    @patch("ndvi.providers.stac.resolve_asset_href_candidates")
    @patch("ndvi.providers.stac._load_single_band")
    def test_load_band(
        self,
        mock_load: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        """load_band resolves asset and delegates to _load_single_band."""
        mock_resolve.return_value = "http://fake/asset.tif"
        expected_array = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32)
        mock_load.return_value = expected_array

        provider = StacDataProvider()
        item = _make_item()
        result = provider.load_band(item, "B08_10m", _bbox())
        assert np.array_equal(result, expected_array)
        mock_resolve.assert_called_once()
        mock_load.assert_called_once()

    @patch("ndvi.providers.stac.resolve_asset_href_candidates")
    def test_load_band_no_href(self, mock_resolve: MagicMock) -> None:
        """load_band returns empty array when asset cannot be resolved."""
        mock_resolve.return_value = None
        provider = StacDataProvider()
        item = _make_item()
        result = provider.load_band(item, "nonexistent", _bbox())
        assert result.size == 0

    def test_implements_data_provider_protocol(self) -> None:
        """StacDataProvider satisfies the DataProvider protocol."""
        provider = StacDataProvider()
        assert isinstance(provider, DataProvider)

    def test_sensor_key_in_band_registry(self) -> None:
        """StacDataProvider's sensor_key exists in BAND_REGISTRY."""
        provider = StacDataProvider()
        assert provider.sensor_key in BAND_REGISTRY

    def test_sensor_key_landsat_in_band_registry(self) -> None:
        """Landsat sensor key exists in BAND_REGISTRY."""
        provider = StacDataProvider(sensor_key="landsat89_l2")
        assert provider.sensor_key in BAND_REGISTRY
        assert "nir" in BAND_REGISTRY[provider.sensor_key]
        assert "swir1" in BAND_REGISTRY[provider.sensor_key]

    def test_custom_client(self) -> None:
        """StacDataProvider accepts a custom StacClient."""
        from ndvi.stac_client import StacClient

        custom_client = StacClient(timeout_seconds=15.0)
        provider = StacDataProvider(client=custom_client)
        assert provider.client is custom_client
        assert provider.client == custom_client


class TestStacProviderHelpers:
    """Tests for STAC provider helper functions."""

    def test_is_remote_href_http(self) -> None:
        """_is_remote_href returns True for http URLs."""
        assert _is_remote_href("http://example.com/file.tif") is True

    def test_is_remote_href_https(self) -> None:
        """_is_remote_href returns True for https URLs."""
        assert _is_remote_href("https://example.com/file.tif") is True

    def test_is_remote_href_local(self) -> None:
        """_is_remote_href returns False for local paths."""
        assert _is_remote_href("local/file.tif") is False
        assert _is_remote_href("relative/path.tif") is False

    @patch("ndvi.providers.stac.httpx.Client")
    def test_download_asset_remote(self, mock_client_cls: MagicMock) -> None:
        """_download_asset downloads remote files."""
        import tempfile

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_response = MagicMock()
        mock_response.content = b"fake-cog-data"
        mock_client.get.return_value = mock_response

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _download_asset(
                "https://example.com/file.tif",
                tmpdir,
                30.0,
            )
            assert result.startswith(tmpdir)
            assert result.endswith("file.tif")
            mock_client.get.assert_called_once_with(
                "https://example.com/file.tif"
            )
            mock_client.close.assert_called_once()
            # Verify content was written
            with open(result, "rb") as f:
                assert f.read() == b"fake-cog-data"

    def test_download_asset_local(self) -> None:
        """_download_asset returns local paths unchanged."""
        result = _download_asset(
            "local/path/file.tif",
            "local-tmp",
            30.0,
        )
        assert result == "local/path/file.tif"

    @patch("rasterio.open")
    @patch("rasterio.Env")
    @patch("tempfile.TemporaryDirectory")
    def test_load_single_band_no_crs_returns_empty(
        self,
        mock_tmpdir: MagicMock,
        mock_env: MagicMock,
        mock_open: MagicMock,
    ) -> None:
        """_load_single_band returns empty array for assets without CRS."""
        mock_src = MagicMock()
        mock_src.crs = None
        mock_open.return_value.__enter__.return_value = mock_src

        result = _load_single_band(
            "/local/test.tif",
            _bbox(),
            timeout_seconds=30.0,
        )
        assert result.size == 0
