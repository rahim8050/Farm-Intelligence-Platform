"""Tests for the GEE engine adapter (STAC-based)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

import numpy as np

from ndvi.engines.base import BBox
from ndvi.engines.gee import GeeEngine
from ndvi.stac_client import NdviStats, StacItem

_FAKE_TODAY = date(2025, 1, 15)


class _MockStacClient:
    """Minimal mock that returns no STAC items."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.collection = str(kwargs.get("collection", ""))

    def search(self, *args: object, **kwargs: object) -> list[object]:
        return []


class _MockStacClientWithItems:
    """Mock STAC client that returns a single test item."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.collection = str(kwargs.get("collection", ""))

    def search(self, *args: object, **kwargs: object) -> list[StacItem]:
        return [
            StacItem(
                id="gee_test_item",
                datetime=datetime(2025, 1, 5, 10, 0, 0),
                assets={
                    "B04_10m": "https://example.com/red.tif",
                    "B08_10m": "https://example.com/nir.tif",
                    "SCL": "https://example.com/scl.tif",
                },
                cloud_cover=10.0,
            ),
        ]


def _bbox() -> BBox:
    return BBox(
        south=Decimal(0),
        west=Decimal(0),
        north=Decimal(1),
        east=Decimal(1),
    )


class TestGeeEngine:
    """GeeEngine is STAC-based, returning results from remote APIs."""

    def setup_method(self) -> None:
        self.engine = GeeEngine(
            client=_MockStacClient(collection="sentinel-2-l2a"),  # type: ignore[arg-type]
        )

    def test_timeseries_returns_empty_list(self) -> None:
        result = self.engine.get_timeseries(
            bbox=_bbox(),
            start=date(2025, 1, 1),
            end=date(2025, 1, 10),
            step_days=5,
            max_cloud=50,
        )
        assert result == []

    def test_latest_returns_none(self) -> None:
        result = self.engine.get_latest(
            bbox=_bbox(),
            lookback_days=30,
            max_cloud=50,
        )
        assert result is None

    def test_default_collection(self) -> None:
        assert self.engine.client.collection == "sentinel-2-l2a"

    def test_iter_buckets(self) -> None:
        buckets = self.engine._iter_buckets(
            date(2025, 1, 1), date(2025, 1, 10), 3
        )
        assert buckets == [
            date(2025, 1, 1),
            date(2025, 1, 4),
            date(2025, 1, 7),
            date(2025, 1, 10),
        ]

    def test_timeseries_with_items(self) -> None:
        engine = GeeEngine(
            client=_MockStacClientWithItems(collection="sentinel-2-l2a"),  # type: ignore[arg-type]
        )
        fake_stats = NdviStats(
            mean=0.5,
            min=0.3,
            max=0.7,
            sample_count=3,
            valid_pixel_fraction=0.9,
            quality_flags={"cloud_fraction": True},
        )
        with (
            patch(
                "ndvi.engines.gee._load_single_gee_band",
                return_value=np.full((10, 10), 0.5, dtype=np.float32),
            ),
            patch(
                "ndvi.engines.gee.compute_ndvi_stats", return_value=fake_stats
            ),
        ):
            result = engine.get_timeseries(
                bbox=_bbox(),
                start=date(2025, 1, 1),
                end=date(2025, 1, 10),
                step_days=5,
                max_cloud=50,
            )
        assert len(result) == 1
        assert result[0].mean == 0.5
        assert result[0].min == 0.3
        assert result[0].max == 0.7
        assert result[0].sample_count == 3
        assert result[0].valid_pixel_fraction == 0.9
        assert result[0].quality_flags == {"cloud_fraction": True}
        assert result[0].cloud_fraction is True

    def test_latest_with_items(self) -> None:
        engine = GeeEngine(
            client=_MockStacClientWithItems(collection="sentinel-2-l2a"),  # type: ignore[arg-type]
        )
        fake_stats = NdviStats(
            mean=0.5,
            min=0.4,
            max=0.6,
            sample_count=2,
        )
        with (
            patch(
                "ndvi.engines.gee._load_single_gee_band",
                return_value=np.full((10, 10), 0.5, dtype=np.float32),
            ),
            patch(
                "ndvi.engines.gee.compute_ndvi_stats", return_value=fake_stats
            ),
            patch("ndvi.engines.gee.date", wraps=date) as mock_date,
        ):
            mock_date.today.return_value = _FAKE_TODAY
            result = engine.get_latest(
                bbox=_bbox(),
                lookback_days=30,
                max_cloud=50,
            )
        assert result is not None
        assert result.mean == 0.5
        assert result.cloud_fraction == 10.0

    def test_compute_stats_no_scl(self) -> None:
        engine = GeeEngine(
            client=_MockStacClient(collection="sentinel-2-l2a"),  # type: ignore[arg-type]
        )
        # Item without SCL asset so resolve_asset_href_candidates returns None
        item = StacItem(
            id="no_scl_item",
            datetime=datetime(2025, 1, 5),
            assets={"B04_10m": "r.tif", "B08_10m": "n.tif"},
            cloud_cover=5.0,
        )
        fake_stats = NdviStats(mean=0.5, min=0.2, max=0.8, sample_count=2)
        with (
            patch(
                "ndvi.engines.gee._load_single_gee_band",
                return_value=np.full((10, 10), 0.5, dtype=np.float32),
            ),
            patch(
                "ndvi.engines.gee.compute_ndvi_stats", return_value=fake_stats
            ),
        ):
            stats = engine._compute_stats(item, _bbox())
        assert stats is not None
        assert stats.mean == 0.5

    def test_compute_stats_missing_assets(self) -> None:
        engine = GeeEngine(
            client=_MockStacClient(collection="sentinel-2-l2a"),  # type: ignore[arg-type]
        )
        item = StacItem(
            id="missing_item",
            datetime=datetime(2025, 1, 5),
            assets={"WRONG_BAND": "x.tif"},
            cloud_cover=5.0,
        )
        stats = engine._compute_stats(item, _bbox())
        assert stats is None

    def test_compute_stats_returns_none_when_ndvi_poor(self) -> None:
        engine = GeeEngine(
            client=_MockStacClient(collection="sentinel-2-l2a"),  # type: ignore[arg-type]
        )
        item = StacItem(
            id="poor_item",
            datetime=datetime(2025, 1, 5),
            assets={"B04_10m": "r.tif", "B08_10m": "n.tif"},
            cloud_cover=5.0,
        )
        with (
            patch(
                "ndvi.engines.gee._load_single_gee_band",
                return_value=np.array([]),
            ),
            patch("ndvi.engines.gee.compute_ndvi_stats", return_value=None),
        ):
            stats = engine._compute_stats(item, _bbox())
        assert stats is None

    def test_timeseries_stats_cache_hit(self) -> None:
        """Two buckets matching same item hit stats_cache."""

        class _MockItemsSameDate:
            def __init__(self, *args: object, **kwargs: object) -> None:
                self.collection = str(kwargs.get("collection", ""))

            def search(
                self, *args: object, **kwargs: object
            ) -> list[StacItem]:
                return [
                    StacItem(
                        id="cache_test_item",
                        datetime=datetime(2025, 1, 10, 10, 0, 0),
                        assets={
                            "B04_10m": "r.tif",
                            "B08_10m": "n.tif",
                            "SCL": "s.tif",
                        },
                        cloud_cover=10.0,
                    ),
                ]

        engine = GeeEngine(
            client=_MockItemsSameDate(collection="sentinel-2-l2a"),  # type: ignore[arg-type]
            date_window_days=5,
        )
        fake_stats = NdviStats(mean=0.5, min=0.3, max=0.7, sample_count=2)
        with (
            patch(
                "ndvi.engines.gee._load_single_gee_band",
                return_value=np.full((10, 10), 0.5, dtype=np.float32),
            ),
            patch(
                "ndvi.engines.gee.compute_ndvi_stats", return_value=fake_stats
            ),
        ):
            result = engine.get_timeseries(
                bbox=_bbox(),
                start=date(2025, 1, 8),
                end=date(2025, 1, 14),
                step_days=6,
                max_cloud=50,
            )
        # Two buckets (Jan 8, Jan 14), both within 5 days of item at Jan 10.
        # Second hit should use cached stats (same item id).
        assert len(result) == 2
        assert result[0].mean == 0.5
        assert result[1].mean == 0.5

    def test_timeseries_skips_when_stats_none(self) -> None:
        engine = GeeEngine(
            client=_MockStacClientWithItems(collection="sentinel-2-l2a"),  # type: ignore[arg-type]
            date_window_days=5,
        )
        with (
            patch(
                "ndvi.engines.gee._load_single_gee_band",
                return_value=np.full((10, 10), 0.5, dtype=np.float32),
            ),
            patch("ndvi.engines.gee.compute_ndvi_stats", return_value=None),
        ):
            result = engine.get_timeseries(
                bbox=_bbox(),
                start=date(2025, 1, 5),
                end=date(2025, 1, 12),
                step_days=7,
                max_cloud=50,
            )
        assert result == []

    def test_latest_returns_none_when_stats_fail(self) -> None:
        engine = GeeEngine(
            client=_MockStacClientWithItems(collection="sentinel-2-l2a"),  # type: ignore[arg-type]
        )
        with (
            patch(
                "ndvi.engines.gee._load_single_gee_band",
                return_value=np.full((10, 10), 0.5, dtype=np.float32),
            ),
            patch("ndvi.engines.gee.compute_ndvi_stats", return_value=None),
            patch("ndvi.engines.gee.date", wraps=date) as mock_date,
        ):
            mock_date.today.return_value = _FAKE_TODAY
            result = engine.get_latest(
                bbox=_bbox(),
                lookback_days=30,
                max_cloud=50,
            )
        assert result is None


class TestGeeEngineCommonDefaults:
    """Test that the engine can be constructed with various defaults."""

    def test_default_constructor(self) -> None:
        engine = GeeEngine(
            client=_MockStacClient(collection="sentinel-2-l2a"),  # type: ignore[arg-type]
        )
        assert engine.asset_red == "B04_10m"
        assert engine.asset_nir == "B08_10m"
        assert engine.asset_scl == "SCL"
        assert engine.mask_water is False

    def test_custom_asset_names(self) -> None:
        engine = GeeEngine(
            asset_red="B04",
            asset_nir="B08",
            mask_water=True,
            client=_MockStacClient(collection="sentinel-2-l2a"),  # type: ignore[arg-type]
        )
        assert engine.asset_red == "B04"
        assert engine.asset_nir == "B08"
        assert engine.asset_scl == "SCL"
        assert engine.mask_water is True
