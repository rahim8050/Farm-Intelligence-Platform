"""Tests for Phase 3 Multi-Engine Fallback (fusion service).

Covers:
- Fusion candidate gathering
- Confidence degradation on fallback
- Deterministic decision tree selection
- Conflict rule (source disagreement)
- Landsat and MODIS engine stubs
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ndvi.engines.base import BBox
from ndvi.engines.landsat import LandsatEngine
from ndvi.engines.modis import ModisEngine
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


class TestLandsatEngine:
    def test_stub_empty_timeseries(self) -> None:
        engine = LandsatEngine()
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
        engine = LandsatEngine()
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


class TestModisEngine:
    def test_stub_empty_timeseries(self) -> None:
        engine = ModisEngine()
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
        engine = ModisEngine()
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
