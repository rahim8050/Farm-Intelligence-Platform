"""Tests for Phase 3 Multi-Engine Fallback (fusion service).

Covers:
- Fusion candidate gathering
- Confidence degradation on fallback
- Deterministic decision tree selection
- Conflict rule (source disagreement)
- Landsat and MODIS engines (mocked STAC)
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

import numpy as np
import pytest

from ndvi.engines.base import BBox, NdviPoint
from ndvi.engines.landsat import LandsatEngine
from ndvi.engines.modis import (
    ModisEngine,
    _compute_band_stats,
    _is_remote_href,
)
from ndvi.fusion import (
    FusionCandidate,
    _apply_confidence_degradation,
    _check_conflict,
    _get_source_priority_index,
    _get_source_threshold,
    _is_primary_source,
    _normalize_source,
    _select_by_decision_tree,
    fuse_observations,
)
from ndvi.stac_client import StacItem
from ndvi.v2_quality import ConfidenceComponents, V2Result


def _make_v2_result(
    selected_ndvi: float = 0.5,
    smoothed_ndvi: float | None = None,
    confidence: float = 0.85,
    is_null: bool = False,
    null_reason: str | None = None,
) -> V2Result:
    return V2Result(
        selected_ndvi=selected_ndvi,
        smoothed_ndvi=smoothed_ndvi,
        confidence=confidence,
        confidence_components=ConfidenceComponents(
            source_weight=1.0,
            cloud_weight=0.9,
            valid_pixel_weight=0.8,
            recency_weight=1.0,
            temporal_consistency_weight=0.9,
        ),
        quality_flags={
            "cloud_heavy": False,
            "low_confidence": False,
        },
        is_null=is_null,
        null_reason=null_reason,
    )


class _FakeV1:
    def __init__(
        self,
        engine: str = "sentinelhub",
        mean: float = 0.5,
        cloud_fraction: float = 0.1,
        valid_pixel_fraction: float = 0.8,
    ) -> None:
        self.engine = engine
        self.mean = mean
        self.cloud_fraction = cloud_fraction
        self.valid_pixel_fraction = valid_pixel_fraction


class TestNormalizeSource:
    def test_lowercase(self) -> None:
        assert _normalize_source("Sentinel-2") == "sentinel-2"

    def test_strips_whitespace(self) -> None:
        assert _normalize_source("  landsat  ") == "landsat"

    def test_already_normalized(self) -> None:
        assert _normalize_source("modis") == "modis"


class TestSourcePriority:
    def test_sentinel2_is_first(self) -> None:
        assert _get_source_priority_index("sentinel-2") == 0

    def test_sentinelhub_is_primary(self) -> None:
        idx = _get_source_priority_index("sentinelhub")
        assert idx < _get_source_priority_index("landsat")

    def test_stac_is_primary(self) -> None:
        idx = _get_source_priority_index("stac")
        assert idx < _get_source_priority_index("landsat")

    def test_landsat_is_after_s2(self) -> None:
        s2_idx = _get_source_priority_index("sentinel-2")
        ls_idx = _get_source_priority_index("landsat")
        assert ls_idx > s2_idx

    def test_modis_is_last(self) -> None:
        modis_idx = _get_source_priority_index("modis")
        for src in ["sentinel-2", "sentinelhub", "stac", "landsat"]:
            assert modis_idx > _get_source_priority_index(src)

    def test_unknown_source_is_last(self) -> None:
        modis_idx = _get_source_priority_index("modis")
        unknown_idx = _get_source_priority_index("unknown")
        assert unknown_idx >= modis_idx


class TestIsPrimarySource:
    def test_sentinel2_is_primary(self) -> None:
        assert _is_primary_source("sentinel-2") is True

    def test_sentinelhub_is_primary(self) -> None:
        assert _is_primary_source("sentinelhub") is True

    def test_stac_is_primary(self) -> None:
        assert _is_primary_source("stac") is True

    def test_landsat_is_not_primary(self) -> None:
        assert _is_primary_source("landsat") is False

    def test_modis_is_not_primary(self) -> None:
        assert _is_primary_source("modis") is False


class TestConfidenceDegradation:
    def test_sentinel2_no_degradation(self) -> None:
        assert _apply_confidence_degradation(0.90, "sentinel-2") == 0.90

    def test_landsat_90_percent(self) -> None:
        assert _apply_confidence_degradation(0.90, "landsat") == 0.81

    def test_modis_80_percent(self) -> None:
        assert _apply_confidence_degradation(0.90, "modis") == 0.72

    def test_sentinelhub_no_degradation(self) -> None:
        assert _apply_confidence_degradation(0.85, "sentinelhub") == 0.85

    def test_stac_no_degradation(self) -> None:
        assert _apply_confidence_degradation(0.80, "stac") == 0.80


class TestSourceThresholds:
    def test_sentinel2_threshold(self) -> None:
        assert _get_source_threshold("sentinel-2") == 0.75

    def test_landsat_threshold(self) -> None:
        assert _get_source_threshold("landsat") == 0.70

    def test_modis_threshold(self) -> None:
        assert _get_source_threshold("modis") == 0.60

    def test_unknown_threshold(self) -> None:
        assert _get_source_threshold("unknown") == 0.50


class TestConflictDetection:
    def test_no_conflict_single(self) -> None:
        candidates = [
            FusionCandidate(
                v1_observation=_FakeV1(engine="sentinelhub", mean=0.5),  # type: ignore[arg-type]
                v2_result=_make_v2_result(selected_ndvi=0.5, confidence=0.90),
                degraded_confidence=0.90,
            )
        ]
        conflict, reason = _check_conflict(candidates)
        assert conflict is False

    def test_no_conflict_small_diff(self) -> None:
        candidates = [
            FusionCandidate(
                v1_observation=_FakeV1(engine="sentinelhub", mean=0.50),  # type: ignore[arg-type]
                v2_result=_make_v2_result(selected_ndvi=0.50, confidence=0.80),
                degraded_confidence=0.80,
            ),
            FusionCandidate(
                v1_observation=_FakeV1(engine="landsat", mean=0.55),  # type: ignore[arg-type]
                v2_result=_make_v2_result(selected_ndvi=0.55, confidence=0.70),
                degraded_confidence=0.63,
            ),
        ]
        conflict, reason = _check_conflict(candidates)
        assert conflict is False

    def test_conflict_large_diff_low_conf(self) -> None:
        candidates = [
            FusionCandidate(
                v1_observation=_FakeV1(engine="sentinelhub", mean=0.30),  # type: ignore[arg-type]
                v2_result=_make_v2_result(selected_ndvi=0.30, confidence=0.60),
                degraded_confidence=0.60,
            ),
            FusionCandidate(
                v1_observation=_FakeV1(engine="landsat", mean=0.50),  # type: ignore[arg-type]
                v2_result=_make_v2_result(selected_ndvi=0.50, confidence=0.55),
                degraded_confidence=0.495,
            ),
        ]
        conflict, reason = _check_conflict(candidates)
        assert conflict is True
        assert "source_disagreement" in reason

    def test_no_conflict_top_exceeds_cap(self) -> None:
        candidates = [
            FusionCandidate(
                v1_observation=_FakeV1(engine="sentinelhub", mean=0.30),  # type: ignore[arg-type]
                v2_result=_make_v2_result(selected_ndvi=0.30, confidence=0.80),
                degraded_confidence=0.80,
            ),
            FusionCandidate(
                v1_observation=_FakeV1(engine="landsat", mean=0.50),  # type: ignore[arg-type]
                v2_result=_make_v2_result(selected_ndvi=0.50, confidence=0.60),
                degraded_confidence=0.54,
            ),
        ]
        conflict, reason = _check_conflict(candidates)
        assert conflict is False


class TestDecisionTree:
    def test_select_sentinel2_qualified(self) -> None:
        candidates = [
            FusionCandidate(
                v1_observation=_FakeV1(engine="sentinelhub", mean=0.5),  # type: ignore[arg-type]
                v2_result=_make_v2_result(selected_ndvi=0.5, confidence=0.85),
                degraded_confidence=0.85,
            ),
        ]
        result = _select_by_decision_tree(candidates)
        assert result.selected is not None
        assert result.selected.source == "sentinelhub"
        assert result.decision_reason == "sentinel2_selected"

    def test_select_landsat_when_s2_below(self) -> None:
        candidates = [
            FusionCandidate(
                v1_observation=_FakeV1(engine="sentinelhub", mean=0.5),  # type: ignore[arg-type]
                v2_result=_make_v2_result(selected_ndvi=0.5, confidence=0.60),
                degraded_confidence=0.60,
            ),
            FusionCandidate(
                v1_observation=_FakeV1(engine="landsat", mean=0.55),  # type: ignore[arg-type]
                v2_result=_make_v2_result(selected_ndvi=0.55, confidence=0.80),
                degraded_confidence=0.72,
            ),
        ]
        result = _select_by_decision_tree(candidates)
        assert result.selected is not None
        assert result.selected.source == "landsat"
        assert result.decision_reason == "landsat_selected"

    def test_select_modis_when_others_below(self) -> None:
        candidates = [
            FusionCandidate(
                v1_observation=_FakeV1(engine="sentinelhub", mean=0.5),  # type: ignore[arg-type]
                v2_result=_make_v2_result(selected_ndvi=0.5, confidence=0.50),
                degraded_confidence=0.50,
            ),
            FusionCandidate(
                v1_observation=_FakeV1(engine="landsat", mean=0.55),  # type: ignore[arg-type]
                v2_result=_make_v2_result(selected_ndvi=0.55, confidence=0.55),
                degraded_confidence=0.495,
            ),
            FusionCandidate(
                v1_observation=_FakeV1(engine="modis", mean=0.52),  # type: ignore[arg-type]
                v2_result=_make_v2_result(selected_ndvi=0.52, confidence=0.75),
                degraded_confidence=0.60,
            ),
        ]
        result = _select_by_decision_tree(candidates)
        assert result.selected is not None
        assert result.selected.source == "modis"
        assert result.decision_reason == "modis_selected"

    def test_select_highest_when_none_qualified(self) -> None:
        candidates = [
            FusionCandidate(
                v1_observation=_FakeV1(engine="landsat", mean=0.5),  # type: ignore[arg-type]
                v2_result=_make_v2_result(selected_ndvi=0.5, confidence=0.55),
                degraded_confidence=0.495,
            ),
            FusionCandidate(
                v1_observation=_FakeV1(engine="modis", mean=0.55),  # type: ignore[arg-type]
                v2_result=_make_v2_result(selected_ndvi=0.55, confidence=0.50),
                degraded_confidence=0.40,
            ),
        ]
        result = _select_by_decision_tree(candidates)
        assert result.selected is not None
        assert result.selected.source == "landsat"
        assert result.decision_reason == "highest_confidence"

    def test_no_candidates_returns_null(self) -> None:
        result = _select_by_decision_tree([])
        assert result.selected is None
        assert result.decision_reason == "no_candidates"

    def test_conflict_returns_null(self) -> None:
        candidates = [
            FusionCandidate(
                v1_observation=_FakeV1(engine="sentinelhub", mean=0.30),  # type: ignore[arg-type]
                v2_result=_make_v2_result(selected_ndvi=0.30, confidence=0.60),
                degraded_confidence=0.60,
            ),
            FusionCandidate(
                v1_observation=_FakeV1(engine="landsat", mean=0.50),  # type: ignore[arg-type]
                v2_result=_make_v2_result(selected_ndvi=0.50, confidence=0.55),
                degraded_confidence=0.495,
            ),
        ]
        result = _select_by_decision_tree(candidates)
        assert result.selected is None
        assert result.conflict_detected is True


class TestFuseObservations:
    @pytest.mark.django_db
    def test_fuse_with_pre_gathered(self) -> None:
        v1 = _FakeV1(engine="sentinelhub", mean=0.5)
        v2 = _make_v2_result(selected_ndvi=0.5, confidence=0.85)
        candidate = FusionCandidate(
            v1_observation=v1,  # type: ignore[arg-type]
            v2_result=v2,
            degraded_confidence=0.85,
        )
        result = fuse_observations(1, date(2025, 6, 1), candidates=[candidate])
        assert result.selected is not None
        assert result.selected.source == "sentinelhub"


class _MockStacClient:
    """Minimal mock that returns no STAC items."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.collection = str(kwargs.get("collection", ""))

    def search(self, *args: object, **kwargs: object) -> list[object]:
        return []


class TestLandsatEngine:
    def test_stub_empty_timeseries(self) -> None:
        engine = LandsatEngine(client=_MockStacClient())  # type: ignore[arg-type]
        points = engine.get_timeseries(
            bbox=BBox(
                south=Decimal("1.0"),
                west=Decimal("1.0"),
                north=Decimal("2.0"),
                east=Decimal("2.0"),
            ),
            start=date(2025, 1, 1),
            end=date(2025, 6, 1),
            step_days=7,
            max_cloud=30,
        )
        assert points == []

    def test_stub_none_latest(self) -> None:
        engine = LandsatEngine(client=_MockStacClient())  # type: ignore[arg-type]
        result = engine.get_latest(
            bbox=BBox(
                south=Decimal("1.0"),
                west=Decimal("1.0"),
                north=Decimal("2.0"),
                east=Decimal("2.0"),
            ),
            lookback_days=14,
            max_cloud=30,
        )
        assert result is None

    def test_iter_buckets(self) -> None:
        engine = LandsatEngine(client=_MockStacClient())  # type: ignore[arg-type]
        buckets = engine._iter_buckets(date(2025, 1, 1), date(2025, 1, 10), 3)
        assert buckets == [
            date(2025, 1, 1),
            date(2025, 1, 4),
            date(2025, 1, 7),
            date(2025, 1, 10),
        ]

    def test_compute_stats_happy_path(self) -> None:
        engine = LandsatEngine(client=_MockStacClient())  # type: ignore[arg-type]
        item = StacItem(
            id="ls_test",
            datetime=datetime(2025, 1, 5),
            assets={"B4": "r.tif", "B5": "n.tif"},
            cloud_cover=10.0,
        )
        fake_point = NdviPoint(
            date=date(2025, 1, 5),
            mean=0.4,
            min=0.2,
            max=0.6,
            sample_count=2,
            cloud_fraction=10.0,
            valid_pixel_fraction=1.0,
            quality_flags={"phase3": True},
        )
        with patch(
            "ndvi.engines.compute.SpectralComputeEngine._compute_for_item",
            return_value=fake_point,
        ):
            stats = engine._compute_stats(
                item,
                BBox(
                    south=Decimal("1"),
                    west=Decimal("1"),
                    north=Decimal("2"),
                    east=Decimal("2"),
                ),
            )
        assert stats is not None
        assert stats.mean == 0.4
        assert stats.sample_count == 2

    def test_compute_stats_missing_assets(self) -> None:
        engine = LandsatEngine(client=_MockStacClient())  # type: ignore[arg-type]
        item = StacItem(
            id="ls_missing",
            datetime=datetime(2025, 1, 5),
            assets={"WRONG": "x.tif"},
            cloud_cover=10.0,
        )
        stats = engine._compute_stats(
            item,
            BBox(
                south=Decimal("1"),
                west=Decimal("1"),
                north=Decimal("2"),
                east=Decimal("2"),
            ),
        )
        assert stats is None

    def test_compute_stats_null_ndvi(self) -> None:
        engine = LandsatEngine(client=_MockStacClient())  # type: ignore[arg-type]
        item = StacItem(
            id="ls_null",
            datetime=datetime(2025, 1, 5),
            assets={"B4": "r.tif", "B5": "n.tif"},
            cloud_cover=10.0,
        )
        with patch(
            "ndvi.engines.compute.SpectralComputeEngine._compute_for_item",
            return_value=None,
        ):
            stats = engine._compute_stats(
                item,
                BBox(
                    south=Decimal("1"),
                    west=Decimal("1"),
                    north=Decimal("2"),
                    east=Decimal("2"),
                ),
            )
        assert stats is None

    def test_timeseries_with_items(self) -> None:
        engine = LandsatEngine(
            client=_MockStacClientWithItems(collection="landsat-8-c2-l2"),  # type: ignore[arg-type]
        )
        fake_point = NdviPoint(
            date=date(2025, 1, 12),
            mean=0.5,
            min=0.3,
            max=0.7,
            sample_count=2,
            cloud_fraction=10.0,
            valid_pixel_fraction=1.0,
            quality_flags={"phase3": True},
        )
        with patch(
            "ndvi.engines.compute.SpectralComputeEngine._compute_for_item",
            return_value=fake_point,
        ):
            result = engine.get_timeseries(
                bbox=BBox(
                    south=Decimal("1"),
                    west=Decimal("1"),
                    north=Decimal("2"),
                    east=Decimal("2"),
                ),
                start=date(2025, 1, 12),
                end=date(2025, 1, 20),
                step_days=7,
                max_cloud=30,
            )
        assert len(result) == 2
        for p in result:
            assert p.mean == 0.5

    def test_latest_with_items(self) -> None:
        engine = LandsatEngine(
            client=_MockStacClientWithItems(collection="landsat-8-c2-l2"),  # type: ignore[arg-type]
        )
        fake_point = NdviPoint(
            date=date(2025, 1, 20),
            mean=0.6,
            min=0.4,
            max=0.8,
            sample_count=2,
            cloud_fraction=10.0,
            valid_pixel_fraction=1.0,
            quality_flags={"phase3": True},
        )
        with (
            patch(
                "ndvi.engines.compute.SpectralComputeEngine._compute_for_item",
                return_value=fake_point,
            ),
            patch("ndvi.providers.stac.date", wraps=date) as mock_date,
        ):
            mock_date.today.return_value = _FAKE_TODAY
            result = engine.get_latest(
                bbox=BBox(
                    south=Decimal("1"),
                    west=Decimal("1"),
                    north=Decimal("2"),
                    east=Decimal("2"),
                ),
                lookback_days=30,
                max_cloud=30,
            )
        assert result is not None
        assert result.mean == 0.6
        assert result.cloud_fraction == 10.0

    def test_latest_returns_none_when_compute_fails(self) -> None:
        engine = LandsatEngine(
            client=_MockStacClientWithItems(collection="landsat-8-c2-l2"),  # type: ignore[arg-type]
        )
        with (
            patch(
                "ndvi.engines.compute.SpectralComputeEngine._compute_for_item",
                return_value=None,
            ),
            patch("ndvi.providers.stac.date", wraps=date) as mock_date,
        ):
            mock_date.today.return_value = _FAKE_TODAY
            result = engine.get_latest(
                bbox=BBox(
                    south=Decimal("1"),
                    west=Decimal("1"),
                    north=Decimal("2"),
                    east=Decimal("2"),
                ),
                lookback_days=30,
                max_cloud=30,
            )
        assert result is None

    def test_default_constructor(self) -> None:
        engine = LandsatEngine(
            client=_MockStacClient(collection="landsat-8-c2-l2"),  # type: ignore[arg-type]
        )
        assert engine.asset_red == "B4"
        assert engine.asset_nir == "B5"

    def test_custom_constructor(self) -> None:
        engine = LandsatEngine(
            asset_red="B4",
            asset_nir="B5",
            timeout_seconds=15.0,
            date_window_days=3,
            client=_MockStacClient(collection="landsat-8-c2-l2"),  # type: ignore[arg-type]
        )
        assert engine.timeout_seconds == 15.0
        assert engine.date_window_days == 3

    def test_timeseries_skips_when_stats_none(self) -> None:
        engine = LandsatEngine(
            client=_MockStacClientWithItems(collection="landsat-8-c2-l2"),  # type: ignore[arg-type]
            date_window_days=15,
        )
        with patch(
            "ndvi.engines.compute.SpectralComputeEngine._compute_for_item",
            return_value=None,
        ):
            result = engine.get_timeseries(
                bbox=BBox(
                    south=Decimal("1"),
                    west=Decimal("1"),
                    north=Decimal("2"),
                    east=Decimal("2"),
                ),
                start=date(2025, 1, 12),
                end=date(2025, 1, 20),
                step_days=7,
                max_cloud=30,
            )
        assert result == []


_FAKE_TODAY = date(2025, 1, 20)


class _MockStacClientWithModisItems:
    """Mock STAC client returning a MODIS-like test item."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.collection = str(kwargs.get("collection", ""))

    def search(self, *args: object, **kwargs: object) -> list[StacItem]:
        return [
            StacItem(
                id="modis_test_item",
                datetime=datetime(2025, 1, 15, 10, 0, 0),
                assets={"NDVI": "n.tif", "DetailedQA": "q.tif"},
                cloud_cover=None,
            ),
        ]


class _MockStacClientWithItems:
    """Mock STAC client that returns a single test item."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.collection = str(kwargs.get("collection", ""))

    def search(self, *args: object, **kwargs: object) -> list[StacItem]:
        return [
            StacItem(
                id="ls_test_item",
                datetime=datetime(2025, 1, 15, 10, 0, 0),
                assets={
                    "B4": "https://example.com/red.tif",
                    "B5": "https://example.com/nir.tif",
                },
                cloud_cover=10.0,
            ),
        ]


class TestModisEngine:
    def test_stub_empty_timeseries(self) -> None:
        engine = ModisEngine(client=_MockStacClient())  # type: ignore[arg-type]
        points = engine.get_timeseries(
            bbox=BBox(
                south=Decimal("1.0"),
                west=Decimal("1.0"),
                north=Decimal("2.0"),
                east=Decimal("2.0"),
            ),
            start=date(2025, 1, 1),
            end=date(2025, 6, 1),
            step_days=7,
            max_cloud=30,
        )
        assert points == []

    def test_stub_none_latest(self) -> None:
        engine = ModisEngine(client=_MockStacClient())  # type: ignore[arg-type]
        result = engine.get_latest(
            bbox=BBox(
                south=Decimal("1.0"),
                west=Decimal("1.0"),
                north=Decimal("2.0"),
                east=Decimal("2.0"),
            ),
            lookback_days=14,
            max_cloud=30,
        )
        assert result is None

    def test_is_remote_href(self) -> None:
        assert _is_remote_href("https://example.com/file.tif") is True
        assert _is_remote_href("http://example.com/file.tif") is True
        assert _is_remote_href("/local/file.tif") is False
        assert _is_remote_href("s3://bucket/key") is False

    def test_compute_band_stats_normal(self) -> None:
        arr = np.array([0.1, 0.5, 0.9, np.nan])
        stats = _compute_band_stats(arr)
        assert stats["mean"] == pytest.approx(0.5)
        assert stats["min"] == pytest.approx(0.1)
        assert stats["max"] == pytest.approx(0.9)
        assert stats["sample_count"] == 3

    def test_compute_band_stats_all_nan(self) -> None:
        arr = np.array([np.nan, np.nan])
        stats = _compute_band_stats(arr)
        assert stats["mean"] is None
        assert stats["sample_count"] == 0

    def test_compute_band_stats_empty(self) -> None:
        arr = np.array([])
        stats = _compute_band_stats(arr)
        assert stats["mean"] is None
        assert stats["sample_count"] == 0

    def test_iter_buckets(self) -> None:
        engine = ModisEngine(client=_MockStacClient())  # type: ignore[arg-type]
        buckets = engine._iter_buckets(date(2025, 1, 1), date(2025, 1, 10), 3)
        assert buckets == [
            date(2025, 1, 1),
            date(2025, 1, 4),
            date(2025, 1, 7),
            date(2025, 1, 10),
        ]

    def test_process_item_missing_ndvi(self) -> None:
        engine = ModisEngine(client=_MockStacClient())  # type: ignore[arg-type]
        item = StacItem(
            id="modis_missing",
            datetime=datetime(2025, 1, 5),
            assets={"WRONG_BAND": "x.tif"},
            cloud_cover=None,
        )
        result = engine._process_item(
            item,
            BBox(
                south=Decimal("1"),
                west=Decimal("1"),
                north=Decimal("2"),
                east=Decimal("2"),
            ),
            date(2025, 1, 5),
        )
        assert result is None

    def test_process_item_empty_array(self) -> None:
        engine = ModisEngine(client=_MockStacClient())  # type: ignore[arg-type]
        item = StacItem(
            id="modis_empty",
            datetime=datetime(2025, 1, 5),
            assets={"NDVI": "n.tif", "DetailedQA": "q.tif"},
            cloud_cover=None,
        )
        with patch(
            "ndvi.engines.modis._load_single_band",
            return_value=np.array([]),
        ):
            result = engine._process_item(
                item,
                BBox(
                    south=Decimal("1"),
                    west=Decimal("1"),
                    north=Decimal("2"),
                    east=Decimal("2"),
                ),
                date(2025, 1, 5),
            )
        assert result is None

    def test_process_item_all_nan(self) -> None:
        """When loaded band is all NaN, stats mean is None -> return None."""
        engine = ModisEngine(client=_MockStacClient())  # type: ignore[arg-type]
        item = StacItem(
            id="modis_nan",
            datetime=datetime(2025, 1, 5),
            assets={"NDVI": "n.tif", "DetailedQA": "q.tif"},
            cloud_cover=None,
        )
        with patch(
            "ndvi.engines.modis._load_single_band",
            return_value=np.array([np.nan, np.nan]),
        ):
            result = engine._process_item(
                item,
                BBox(
                    south=Decimal("1"),
                    west=Decimal("1"),
                    north=Decimal("2"),
                    east=Decimal("2"),
                ),
                date(2025, 1, 5),
            )
        assert result is None

    def test_process_item_happy_path(self) -> None:
        engine = ModisEngine(client=_MockStacClient())  # type: ignore[arg-type]
        item = StacItem(
            id="modis_ok",
            datetime=datetime(2025, 1, 5),
            assets={"NDVI": "n.tif", "DetailedQA": "q.tif"},
            cloud_cover=None,
        )
        with patch(
            "ndvi.engines.modis._load_single_band",
            return_value=np.array([0.1, 0.5, 0.9]),
        ):
            result = engine._process_item(
                item,
                BBox(
                    south=Decimal("1"),
                    west=Decimal("1"),
                    north=Decimal("2"),
                    east=Decimal("2"),
                ),
                date(2025, 1, 5),
            )
        assert result is not None
        assert result.mean == pytest.approx(0.5)
        assert result.min == pytest.approx(0.1)
        assert result.max == pytest.approx(0.9)
        assert result.sample_count == 3
        assert result.quality_flags == {
            "modis": True,
            "pre_computed_ndvi": True,
        }

    def test_timeseries_with_items(self) -> None:
        engine = ModisEngine(
            client=_MockStacClientWithModisItems(),  # type: ignore[arg-type]
            date_window_days=15,
        )
        with patch(
            "ndvi.engines.modis._load_single_band",
            return_value=np.array([0.1, 0.5, 0.9]),
        ):
            result = engine.get_timeseries(
                bbox=BBox(
                    south=Decimal("1"),
                    west=Decimal("1"),
                    north=Decimal("2"),
                    east=Decimal("2"),
                ),
                start=date(2025, 1, 12),
                end=date(2025, 1, 20),
                step_days=7,
                max_cloud=30,
            )
        assert len(result) == 2
        for p in result:
            assert p.mean == pytest.approx(0.5)

    def test_latest_with_items(self) -> None:
        engine = ModisEngine(
            client=_MockStacClientWithModisItems(),  # type: ignore[arg-type]
        )
        with (
            patch(
                "ndvi.engines.modis._load_single_band",
                return_value=np.array([0.2, 0.6, 0.8]),
            ),
            patch("ndvi.engines.modis.date", wraps=date) as mock_date,
        ):
            mock_date.today.return_value = _FAKE_TODAY
            result = engine.get_latest(
                bbox=BBox(
                    south=Decimal("1"),
                    west=Decimal("1"),
                    north=Decimal("2"),
                    east=Decimal("2"),
                ),
                lookback_days=30,
                max_cloud=30,
            )
        assert result is not None
        assert result.mean == pytest.approx((0.2 + 0.6 + 0.8) / 3)

    def test_download_asset_local(self) -> None:
        """Local files are returned unchanged by _download_asset."""
        import os
        import tempfile

        from ndvi.engines.modis import _download_asset

        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
            f.write(b"fake_cog_content")
            local_path = f.name
        try:
            result = _download_asset(local_path, tempfile.gettempdir(), 10.0)
            assert result == local_path
        finally:
            os.unlink(local_path)

    def test_load_single_band_local_tif(self) -> None:
        """_load_single_band reads a local GeoTIFF correctly."""
        import os
        import tempfile

        import numpy as np
        import rasterio
        from rasterio.transform import from_bounds

        from ndvi.engines.modis import _load_single_band

        width, height = 20, 20
        data = np.ones((height, width), dtype=np.float32) * 0.5
        data[5, 5] = np.nan
        transform = from_bounds(0, 0, 1, 1, width, height)
        profile = {
            "driver": "GTiff",
            "width": width,
            "height": height,
            "count": 1,
            "dtype": data.dtype,
            "crs": "EPSG:4326",
            "transform": transform,
        }
        tmp = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
        tmp.close()
        try:
            with rasterio.open(tmp.name, "w", **profile) as dst:
                dst.write(data, 1)
            bbox_ = BBox(
                south=Decimal("0.1"),
                west=Decimal("0.1"),
                north=Decimal("0.9"),
                east=Decimal("0.9"),
            )
            arr = _load_single_band(
                tmp.name,
                bbox=bbox_,
                size=20,
                timeout_seconds=10.0,
            )
            assert arr.size > 0
            assert not np.isnan(arr).all()
            assert np.nanmean(arr) == pytest.approx(0.5, abs=0.05)
        finally:
            os.unlink(tmp.name)

    def test_load_single_band_with_scale_factor(self) -> None:
        """Scale factor multiplies the loaded band data."""
        import os
        import tempfile

        import numpy as np
        import rasterio
        from rasterio.transform import from_bounds

        from ndvi.engines.modis import _load_single_band

        width, height = 10, 10
        data = np.ones((height, width), dtype=np.float32) * 1000
        transform = from_bounds(0, 0, 1, 1, width, height)
        profile = {
            "driver": "GTiff",
            "width": width,
            "height": height,
            "count": 1,
            "dtype": data.dtype,
            "crs": "EPSG:4326",
            "transform": transform,
        }
        tmp = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
        tmp.close()
        try:
            with rasterio.open(tmp.name, "w", **profile) as dst:
                dst.write(data, 1)
            bbox_ = BBox(
                south=Decimal("0.1"),
                west=Decimal("0.1"),
                north=Decimal("0.9"),
                east=Decimal("0.9"),
            )
            arr = _load_single_band(
                tmp.name,
                bbox=bbox_,
                size=10,
                timeout_seconds=10.0,
                scale_factor=0.0001,
            )
            assert arr.size > 0
            assert np.nanmean(arr) == pytest.approx(0.1, abs=0.01)
        finally:
            os.unlink(tmp.name)

    def test_load_single_band_with_qa(self) -> None:
        """QA band is applied to mask out pixels."""
        import os
        import tempfile

        import numpy as np
        import rasterio
        from rasterio.transform import from_bounds

        from ndvi.engines.modis import _load_single_band

        width, height = 10, 10
        ndvi_data = np.full((height, width), 0.5, dtype=np.float32)
        qa_data = np.zeros((height, width), dtype=np.uint16)
        qa_data[3:6, 3:6] = 1
        transform = from_bounds(0, 0, 1, 1, width, height)
        profile = {
            "driver": "GTiff",
            "width": width,
            "height": height,
            "count": 1,
            "dtype": np.float32,
            "crs": "EPSG:4326",
            "transform": transform,
        }
        qa_profile = {**profile, "dtype": np.uint16}
        tmp_ndvi = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
        tmp_qa = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
        tmp_ndvi.close()
        tmp_qa.close()
        try:
            with rasterio.open(tmp_ndvi.name, "w", **profile) as dst:
                dst.write(ndvi_data, 1)
            with rasterio.open(tmp_qa.name, "w", **qa_profile) as dst:
                dst.write(qa_data, 1)
            bbox_ = BBox(
                south=Decimal("0.1"),
                west=Decimal("0.1"),
                north=Decimal("0.9"),
                east=Decimal("0.9"),
            )
            arr = _load_single_band(
                tmp_ndvi.name,
                bbox=bbox_,
                size=10,
                timeout_seconds=10.0,
                qa_href=tmp_qa.name,
            )
            assert arr.size > 0
            assert bool(np.isnan(arr).any())
        finally:
            os.unlink(tmp_ndvi.name)
            os.unlink(tmp_qa.name)

    def test_load_single_band_no_crs_returns_empty(self) -> None:
        """A GeoTIFF without CRS returns an empty array."""
        import os
        import tempfile

        import numpy as np
        import rasterio
        from rasterio.transform import from_bounds

        from ndvi.engines.modis import _load_single_band

        width, height = 10, 10
        data = np.ones((height, width), dtype=np.float32)
        transform = from_bounds(0, 0, 1, 1, width, height)
        profile = {
            "driver": "GTiff",
            "width": width,
            "height": height,
            "count": 1,
            "dtype": data.dtype,
            "crs": None,
            "transform": transform,
        }
        tmp = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
        tmp.close()
        try:
            with rasterio.open(tmp.name, "w", **profile) as dst:
                dst.write(data, 1)
            bbox_ = BBox(
                south=Decimal("0.1"),
                west=Decimal("0.1"),
                north=Decimal("0.9"),
                east=Decimal("0.9"),
            )
            arr = _load_single_band(
                tmp.name,
                bbox=bbox_,
                size=10,
                timeout_seconds=10.0,
            )
            assert arr.size == 0
        finally:
            os.unlink(tmp.name)

    def test_load_single_band_qa_download_failure_logs_warning(
        self,
    ) -> None:
        """QA download failure is logged and processing continues."""
        import os
        import tempfile

        import numpy as np
        import rasterio
        from rasterio.transform import from_bounds

        from ndvi.engines.modis import _load_single_band

        width, height = 10, 10
        data = np.full((height, width), 0.5, dtype=np.float32)
        transform = from_bounds(0, 0, 1, 1, width, height)
        profile = {
            "driver": "GTiff",
            "width": width,
            "height": height,
            "count": 1,
            "dtype": data.dtype,
            "crs": "EPSG:4326",
            "transform": transform,
        }
        tmp = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
        tmp.close()
        try:
            with rasterio.open(tmp.name, "w", **profile) as dst:
                dst.write(data, 1)
            bbox_ = BBox(
                south=Decimal("0.1"),
                west=Decimal("0.1"),
                north=Decimal("0.9"),
                east=Decimal("0.9"),
            )
            with patch(
                "ndvi.engines.modis.httpx.Client.get",
                side_effect=Exception("connection refused"),
            ):
                arr = _load_single_band(
                    tmp.name,
                    bbox=bbox_,
                    size=10,
                    timeout_seconds=10.0,
                    qa_href="https://example.com/qa.tif",
                )
            assert arr.size > 0
        finally:
            os.unlink(tmp.name)

    def test_download_asset_via_mock_http(self) -> None:
        """_download_asset downloads remote files via httpx."""
        import os
        import tempfile

        import httpx

        from ndvi.engines.modis import _download_asset

        class _FakeResponse:
            def raise_for_status(self) -> None:
                pass

            content: bytes = b"fake_binary_content"

        with patch.object(httpx.Client, "get", return_value=_FakeResponse()):
            result = _download_asset(
                "https://example.com/test.tif",
                tempfile.gettempdir(),
                10.0,
            )
        assert os.path.exists(result)
        with open(result, "rb") as f:
            assert f.read() == b"fake_binary_content"
        os.unlink(result)

    def test_default_constructor(self) -> None:
        engine = ModisEngine(
            client=_MockStacClient(collection="modis-13q1-061"),  # type: ignore[arg-type]
        )
        assert engine.ndvi_band == "NDVI"
        assert engine.qa_band == "DetailedQA"

    def test_custom_constructor(self) -> None:
        engine = ModisEngine(
            ndvi_band="custom_ndvi",
            qa_band="custom_qa",
            timeout_seconds=20.0,
            client=_MockStacClient(collection="modis-13q1-061"),  # type: ignore[arg-type]
        )
        assert engine.ndvi_band == "custom_ndvi"
        assert engine.qa_band == "custom_qa"
        assert engine.timeout_seconds == 20.0
