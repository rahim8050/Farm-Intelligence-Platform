"""Tests for Phase 4 Fusion and Intelligence (S1 context, anomaly).

Covers:
- Sentinel1Context dataclass and to_flags()
- fetch_sentinel1_context() stub
- merge_s1_context_flags()
- detect_anomaly() for flooding, wet soil, urban artifact, no anomaly
- FusionResult quality_flags propagation (source_disagreement, fallback_used)
"""

from __future__ import annotations

from datetime import date

from ndvi.fusion import (
    FusionCandidate,
    _build_result_flags,
    _select_by_decision_tree,
)
from ndvi.sentinel1_context import (
    S1_CONTEXT_FLAG_FIELDS,
    Sentinel1Context,
    detect_anomaly,
    fetch_sentinel1_context,
    merge_s1_context_flags,
)
from ndvi.v2_quality import ConfidenceComponents, V2Result


def _make_v2_result(
    selected_ndvi: float = 0.5,
    confidence: float = 0.85,
    is_null: bool = False,
    null_reason: str | None = None,
) -> V2Result:
    return V2Result(
        selected_ndvi=selected_ndvi,
        smoothed_ndvi=None,
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


class TestSentinel1Context:
    def test_dataclass_defaults(self) -> None:
        ctx = Sentinel1Context()
        assert ctx.wet_soil is False
        assert ctx.flooding is False
        assert ctx.rough_surface is False
        assert ctx.urban_interference is False

    def test_to_flags_empty(self) -> None:
        ctx = Sentinel1Context()
        flags = ctx.to_flags()
        for key in S1_CONTEXT_FLAG_FIELDS:
            assert key in flags
            assert flags[key] is False

    def test_to_flags_with_values(self) -> None:
        ctx = Sentinel1Context(wet_soil=True, flooding=True)
        flags = ctx.to_flags()
        assert flags["s1_wet_soil"] is True
        assert flags["s1_flooding"] is True
        assert flags["s1_rough_surface"] is False
        assert flags["s1_urban_interference"] is False

    def test_has_any_signal_false(self) -> None:
        ctx = Sentinel1Context()
        assert ctx.has_any_signal() is False

    def test_has_any_signal_true(self) -> None:
        ctx = Sentinel1Context(wet_soil=True)
        assert ctx.has_any_signal() is True

    def test_has_any_signal_flooding(self) -> None:
        ctx = Sentinel1Context(flooding=True)
        assert ctx.has_any_signal() is True

    def test_has_any_signal_urban(self) -> None:
        ctx = Sentinel1Context(urban_interference=True)
        assert ctx.has_any_signal() is True


class TestFetchSentinel1Context:
    def test_stub_returns_default(self) -> None:
        ctx = fetch_sentinel1_context(1, date(2025, 6, 1))
        assert isinstance(ctx, Sentinel1Context)
        assert ctx.has_any_signal() is False


class TestMergeS1ContextFlags:
    def test_merges_empty(self) -> None:
        flags = {"existing": True}
        ctx = Sentinel1Context()
        merged = merge_s1_context_flags(flags, ctx)
        assert merged["existing"] is True
        assert merged["s1_wet_soil"] is False

    def test_merges_with_values(self) -> None:
        flags = {"existing": True}
        ctx = Sentinel1Context(wet_soil=True, flooding=True)
        merged = merge_s1_context_flags(flags, ctx)
        assert merged["existing"] is True
        assert merged["s1_wet_soil"] is True
        assert merged["s1_flooding"] is True
        assert merged["s1_rough_surface"] is False

    def test_does_not_mutate_original(self) -> None:
        flags = {"existing": True}
        ctx = Sentinel1Context(wet_soil=True)
        merge_s1_context_flags(flags, ctx)
        assert "s1_wet_soil" not in flags


class TestDetectAnomaly:
    def test_no_anomaly_no_context(self) -> None:
        ctx = Sentinel1Context()
        is_anom, reason = detect_anomaly(0.5, ctx)
        assert is_anom is False
        assert reason is None

    def test_flooding_low_ndvi(self) -> None:
        ctx = Sentinel1Context(flooding=True)
        is_anom, reason = detect_anomaly(0.1, ctx)
        assert is_anom is True
        assert reason == "possible_flooding"

    def test_wet_soil_low_ndvi(self) -> None:
        ctx = Sentinel1Context(wet_soil=True)
        is_anom, reason = detect_anomaly(0.12, ctx)
        assert is_anom is True
        assert reason == "wet_soil_depression"

    def test_urban_artifact_high_ndvi(self) -> None:
        ctx = Sentinel1Context(urban_interference=True)
        is_anom, reason = detect_anomaly(0.90, ctx)
        assert is_anom is True
        assert reason == "urban_artifact"

    def test_no_anomaly_wet_soil_normal_ndvi(self) -> None:
        ctx = Sentinel1Context(wet_soil=True)
        is_anom, reason = detect_anomaly(0.5, ctx)
        assert is_anom is False
        assert reason is None

    def test_no_anomaly_none_ndvi(self) -> None:
        ctx = Sentinel1Context(flooding=True)
        is_anom, reason = detect_anomaly(None, ctx)
        assert is_anom is False
        assert reason is None

    def test_custom_threshold(self) -> None:
        ctx = Sentinel1Context(flooding=True)
        is_anom, reason = detect_anomaly(0.20, ctx, ndvi_threshold=0.25)
        assert is_anom is True
        assert reason == "possible_flooding"

    def test_custom_threshold_above(self) -> None:
        ctx = Sentinel1Context(flooding=True)
        is_anom, reason = detect_anomaly(0.20, ctx, ndvi_threshold=0.15)
        assert is_anom is False
        assert reason is None


class TestBuildResultFlags:
    def test_no_selected(self) -> None:
        flags = _build_result_flags(None, False)
        assert flags["source_disagreement"] is False
        assert flags["fallback_used"] is False
        assert flags["anomaly_detected"] is False

    def test_conflict_detected(self) -> None:
        flags = _build_result_flags(None, True)
        assert flags["source_disagreement"] is True

    def test_sentinel2_no_fallback(self) -> None:
        candidate = FusionCandidate(
            v1_observation=_FakeV1(engine="sentinelhub"),  # type: ignore[arg-type]
            v2_result=_make_v2_result(),
            degraded_confidence=0.85,
        )
        flags = _build_result_flags(candidate, False)
        assert flags["fallback_used"] is False

    def test_landsat_fallback(self) -> None:
        candidate = FusionCandidate(
            v1_observation=_FakeV1(engine="landsat"),  # type: ignore[arg-type]
            v2_result=_make_v2_result(),
            degraded_confidence=0.72,
        )
        flags = _build_result_flags(candidate, False)
        assert flags["fallback_used"] is True

    def test_modis_fallback(self) -> None:
        candidate = FusionCandidate(
            v1_observation=_FakeV1(engine="modis"),  # type: ignore[arg-type]
            v2_result=_make_v2_result(),
            degraded_confidence=0.60,
        )
        flags = _build_result_flags(candidate, False)
        assert flags["fallback_used"] is True

    def test_s1_context_merged(self) -> None:
        candidate = FusionCandidate(
            v1_observation=_FakeV1(engine="sentinelhub"),  # type: ignore[arg-type]
            v2_result=_make_v2_result(),
            degraded_confidence=0.85,
        )
        s1 = Sentinel1Context(wet_soil=True)
        flags = _build_result_flags(candidate, False, s1_context=s1)
        assert flags["s1_wet_soil"] is True

    def test_anomaly_detected_with_s1(self) -> None:
        candidate = FusionCandidate(
            v1_observation=_FakeV1(engine="sentinelhub", mean=0.1),  # type: ignore[arg-type]
            v2_result=_make_v2_result(selected_ndvi=0.1),
            degraded_confidence=0.85,
        )
        s1 = Sentinel1Context(flooding=True)
        flags = _build_result_flags(candidate, False, s1_context=s1)
        assert flags["anomaly_detected"] is True
        assert flags["anomaly_possible_flooding"] is True

    def test_no_anomaly_without_s1_signal(self) -> None:
        candidate = FusionCandidate(
            v1_observation=_FakeV1(engine="sentinelhub", mean=0.1),  # type: ignore[arg-type]
            v2_result=_make_v2_result(selected_ndvi=0.1),
            degraded_confidence=0.85,
        )
        s1 = Sentinel1Context()
        flags = _build_result_flags(candidate, False, s1_context=s1)
        assert flags["anomaly_detected"] is False


class TestFusionResultQualityFlags:
    def test_sentinel2_result_no_fallback(self) -> None:
        candidates = [
            FusionCandidate(
                v1_observation=_FakeV1(engine="sentinelhub"),  # type: ignore[arg-type]
                v2_result=_make_v2_result(confidence=0.85),
                degraded_confidence=0.85,
            ),
        ]
        result = _select_by_decision_tree(candidates)
        assert result.selected is not None
        assert result.quality_flags["fallback_used"] is False
        assert result.quality_flags["source_disagreement"] is False

    def test_landsat_result_sets_fallback(self) -> None:
        candidates = [
            FusionCandidate(
                v1_observation=_FakeV1(engine="sentinelhub"),  # type: ignore[arg-type]
                v2_result=_make_v2_result(confidence=0.60),
                degraded_confidence=0.60,
            ),
            FusionCandidate(
                v1_observation=_FakeV1(engine="landsat"),  # type: ignore[arg-type]
                v2_result=_make_v2_result(confidence=0.80),
                degraded_confidence=0.72,
            ),
        ]
        result = _select_by_decision_tree(candidates)
        assert result.selected is not None
        assert result.selected.source == "landsat"
        assert result.quality_flags["fallback_used"] is True

    def test_conflict_sets_source_disagreement(self) -> None:
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
        assert result.quality_flags["source_disagreement"] is True

    def test_no_candidates_flags(self) -> None:
        result = _select_by_decision_tree([])
        assert result.selected is None
        assert result.quality_flags["source_disagreement"] is False
        assert result.quality_flags["fallback_used"] is False

    def test_s1_context_through_fuse(self) -> None:
        candidates = [
            FusionCandidate(
                v1_observation=_FakeV1(engine="sentinelhub"),  # type: ignore[arg-type]
                v2_result=_make_v2_result(confidence=0.85),
                degraded_confidence=0.85,
            ),
        ]
        from ndvi.sentinel1_context import Sentinel1Context

        s1 = Sentinel1Context(wet_soil=True)
        result = _select_by_decision_tree(candidates, s1_context=s1)
        assert result.selected is not None
        assert result.quality_flags.get("s1_wet_soil") is True
