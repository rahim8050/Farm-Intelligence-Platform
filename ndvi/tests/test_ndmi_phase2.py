"""Tests for NDMI Phase 2 modules — quality, fusion, colormaps, metrics, tasks.

Covers:
- science/quality/base.py — QualityScorer ABC, QualityResult,
  QUALITY_SCORERS registry
- science/quality/ndmi.py — NDMIQualityScorer, helper functions,
  functional API
- science/fusion/base.py — FusionEngine ABC, FusionCandidate,
  FusionResult, FUSION_ENGINES registry
- science/fusion/ndmi.py — NDMIFusionEngine, classify_ndmi,
  run_ndmi_fusion
- ndvi/colormaps/ndmi.py — COLORMAP_REGISTRY, constants, control points
- ndvi/metrics.py — NDMI-specific Prometheus metrics
- ndvi/tasks.py — enqueue_daily_ndmi_refresh, enqueue_ndmi_gap_fill
"""

from __future__ import annotations

import secrets
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import patch

import pytest
from django.test import override_settings

from farms.models import Farm
from science.fusion.base import get_fusion_engine
from science.fusion.ndmi import (
    NDMIFusionEngine,
    classify_ndmi,
    run_ndmi_fusion,
)
from science.quality.base import (
    QUALITY_SCORERS,
    QualityResult,
    QualityScorer,
    get_quality_scorer,
)
from science.quality.ndmi import (
    NDMIQualityScorer,
    _detect_cloud_shadow,
    _detect_swir_noise,
    _validate_moisture_range,
    build_ndmi_v2_observation,
    process_ndmi_v1_to_v2,
)

PASSWORD = secrets.token_urlsafe(12)


# ── 2.2 — science/quality/ndmi.py helper tests ────────────────────────


class TestDetectSwirNoise:
    """Unit tests for _detect_swir_noise()."""

    def test_extreme_positive_is_noisy(self) -> None:
        assert _detect_swir_noise(0.95) is True

    def test_extreme_negative_is_noisy(self) -> None:
        assert _detect_swir_noise(-0.95) is True

    def test_zero_is_not_noisy(self) -> None:
        assert _detect_swir_noise(0.0) is False

    def test_mid_range_is_not_noisy(self) -> None:
        assert _detect_swir_noise(0.5) is False

    def test_above_09_is_noisy(self) -> None:
        assert _detect_swir_noise(0.91) is True

    def test_below_neg_09_is_noisy(self) -> None:
        assert _detect_swir_noise(-0.91) is True


class TestDetectCloudShadow:
    """Unit tests for _detect_cloud_shadow()."""

    def test_shadow_detected(self) -> None:
        assert _detect_cloud_shadow(0.2, 0.5, 0.7) is True

    def test_no_shadow_when_cloud_fraction_low(self) -> None:
        assert _detect_cloud_shadow(0.04, 0.5, 0.7) is False

    def test_no_shadow_when_cloud_fraction_high(self) -> None:
        assert _detect_cloud_shadow(0.41, 0.5, 0.7) is False

    def test_no_shadow_when_valid_pixels_high(self) -> None:
        assert _detect_cloud_shadow(0.2, 0.61, 0.7) is False

    def test_no_shadow_when_ndmi_not_high(self) -> None:
        assert _detect_cloud_shadow(0.2, 0.5, 0.59) is False

    def test_no_shadow_when_cloud_is_none(self) -> None:
        assert _detect_cloud_shadow(None, 0.5, 0.7) is False

    def test_no_shadow_when_valid_pixels_is_none(self) -> None:
        assert _detect_cloud_shadow(0.2, None, 0.7) is False


class TestValidateMoistureRange:
    """Unit tests for _validate_moisture_range()."""

    def test_zero_is_valid(self) -> None:
        assert _validate_moisture_range(0.0) is True

    def test_positive_moderate_is_valid(self) -> None:
        assert _validate_moisture_range(0.5) is True

    def test_negative_moderate_is_valid(self) -> None:
        assert _validate_moisture_range(-0.5) is True

    def test_upper_boundary_inclusive(self) -> None:
        assert _validate_moisture_range(0.95) is True

    def test_lower_boundary_inclusive(self) -> None:
        assert _validate_moisture_range(-0.95) is True

    def test_above_max_is_invalid(self) -> None:
        assert _validate_moisture_range(0.96) is False

    def test_below_min_is_invalid(self) -> None:
        assert _validate_moisture_range(-0.96) is False


# ── 2.3 — science/fusion/ndmi.py classify_ndmi tests ──────────────────


class TestClassifyNdmi:
    """Unit tests for classify_ndmi()."""

    def test_high_moisture(self) -> None:
        assert classify_ndmi(0.3) == "high_moisture"

    def test_adequate_moisture(self) -> None:
        assert classify_ndmi(0.0) == "adequate"

    def test_moisture_stress(self) -> None:
        assert classify_ndmi(-0.2) == "moisture_stress"

    def test_at_moisture_threshold(self) -> None:
        assert classify_ndmi(0.2) == "high_moisture"

    def test_just_below_moisture_threshold(self) -> None:
        assert classify_ndmi(0.19) == "adequate"

    def test_at_stress_threshold(self) -> None:
        # stress threshold is -0.1, so -0.1 >= stress_threshold => adequate
        assert classify_ndmi(-0.1) == "adequate"

    def test_below_stress_threshold(self) -> None:
        assert classify_ndmi(-0.11) == "moisture_stress"


class TestClassifyNdmiCustomThresholds:
    """Verify settings overrides for classify_ndmi()."""

    @override_settings(NDMI_MOISTURE_THRESHOLD=0.5)
    def test_custom_moisture_threshold(self) -> None:
        assert classify_ndmi(0.3) == "adequate"
        assert classify_ndmi(0.5) == "high_moisture"

    @override_settings(NDMI_STRESS_THRESHOLD=-0.3)
    def test_custom_stress_threshold(self) -> None:
        assert classify_ndmi(-0.2) == "adequate"
        assert classify_ndmi(-0.3) == "adequate"
        assert classify_ndmi(-0.31) == "moisture_stress"


# ── 2.2 — NDMIQualityScorer integration tests ─────────────────────────


class TestNdmiQualityScorer:
    """Integration tests for NDMIQualityScorer."""

    @pytest.fixture
    def ndmi_obs_kwargs(self) -> dict:
        return dict(
            farm_id=1,
            engine="sentinel-2",
            bucket_date=date(2025, 6, 1),
            mean=0.30,
            index_type="NDMI",
            is_latest=True,
            state="FINAL",
            cloud_fraction=0.05,
            valid_pixel_fraction=0.80,
            acquired_at=datetime(2025, 6, 1, 10, 0, 0, tzinfo=UTC),
        )

    def _make_obs(self, **kwargs: object) -> Any:
        from ndvi.models import NdviObservation

        return NdviObservation(**kwargs)

    @pytest.mark.django_db
    @override_settings(
        NDMI_MIN_ROLLING_CONTEXT=0,
        NDMI_MAX_CONFIDENCE_WITHOUT_CONTEXT=0.80,
    )
    def test_happy_path_returns_quality_result(
        self, ndmi_obs_kwargs: dict
    ) -> None:
        obs = self._make_obs(**ndmi_obs_kwargs)
        result = build_ndmi_v2_observation(obs)
        assert result is not None
        assert result.is_null is False
        assert result.selected_value == pytest.approx(0.30, abs=0.01)
        assert result.confidence > 0.0
        assert isinstance(result, QualityResult)

    @pytest.mark.django_db
    @override_settings(NDMI_MIN_ROLLING_CONTEXT=0)
    def test_null_when_valid_pixel_below_threshold(
        self, ndmi_obs_kwargs: dict
    ) -> None:
        kwargs = dict(ndmi_obs_kwargs)
        kwargs["valid_pixel_fraction"] = 0.20
        obs = self._make_obs(**kwargs)
        result = build_ndmi_v2_observation(obs)
        assert result.is_null
        assert result.null_reason == "low_valid_pixel_fraction"

    @pytest.mark.django_db
    @override_settings(
        NDMI_MIN_ROLLING_CONTEXT=3,
        NDMI_MAX_CONFIDENCE_WITHOUT_CONTEXT=0.30,
    )
    def test_null_when_confidence_below_threshold(
        self, ndmi_obs_kwargs: dict
    ) -> None:
        kwargs = dict(ndmi_obs_kwargs)
        kwargs.update(valid_pixel_fraction=0.30, cloud_fraction=0.95)
        obs = self._make_obs(**kwargs)
        result = build_ndmi_v2_observation(obs)
        assert result.is_null
        assert result.null_reason == "low_confidence"

    @pytest.mark.django_db
    @override_settings(NDMI_MIN_ROLLING_CONTEXT=0)
    def test_null_when_mean_is_none(self, ndmi_obs_kwargs: dict) -> None:
        kwargs = dict(ndmi_obs_kwargs)
        kwargs["mean"] = None
        obs = self._make_obs(**kwargs)
        result = build_ndmi_v2_observation(obs)
        assert result.is_null
        assert result.null_reason == "missing_ndvi_value"

    @pytest.mark.django_db
    @override_settings(NDMI_MIN_ROLLING_CONTEXT=0)
    def test_swir_noise_flag_set(self, ndmi_obs_kwargs: dict) -> None:
        kwargs = dict(ndmi_obs_kwargs)
        kwargs["mean"] = 0.95
        obs = self._make_obs(**kwargs)
        result = build_ndmi_v2_observation(obs)
        assert result.quality_flags.get("swir_noise") is True

    @pytest.mark.django_db
    @override_settings(NDMI_MIN_ROLLING_CONTEXT=0)
    def test_invalid_moisture_range_flag(self, ndmi_obs_kwargs: dict) -> None:
        kwargs = dict(ndmi_obs_kwargs)
        kwargs["mean"] = 0.96
        obs = self._make_obs(**kwargs)
        result = build_ndmi_v2_observation(obs)
        assert result.quality_flags.get("invalid_moisture_range") is True
        # Should be nulled because invalid range gives 0 confidence
        assert result.is_null

    @pytest.mark.django_db
    @override_settings(NDMI_MIN_ROLLING_CONTEXT=0)
    def test_cloud_shadow_flag_set(self, ndmi_obs_kwargs: dict) -> None:
        kwargs = dict(ndmi_obs_kwargs)
        kwargs.update(
            cloud_fraction=0.2,
            valid_pixel_fraction=0.5,
            mean=0.7,
        )
        obs = self._make_obs(**kwargs)
        result = build_ndmi_v2_observation(obs)
        assert result.quality_flags.get("cloud_shadow") is True

    @pytest.mark.django_db
    @override_settings(NDMI_MIN_ROLLING_CONTEXT=0)
    def test_process_ndmi_v1_to_v2_persists(
        self, ndmi_obs_kwargs: dict, django_user_model: Any
    ) -> None:
        from ndvi.models import NdviObservation

        user = django_user_model.objects.create_user(
            username="ndmi-persist",
            email="ndmi-persist@example.com",
            password=PASSWORD,
        )
        farm = Farm.objects.create(
            owner=user,
            name="NDMI Persist",
            slug="ndmi-persist",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        kwargs = dict(ndmi_obs_kwargs)
        kwargs["farm"] = farm
        obs = NdviObservation.objects.create(**kwargs)
        result, derived = process_ndmi_v1_to_v2(obs, persist=True)
        assert result is not None
        assert derived is not None
        assert derived.index_type == "NDMI"
        assert derived.selected_ndvi == pytest.approx(0.30, abs=0.01)

    @pytest.mark.django_db
    @override_settings(NDMI_MIN_ROLLING_CONTEXT=0)
    def test_process_ndmi_v1_to_v2_no_persist(
        self, ndmi_obs_kwargs: dict, django_user_model: Any
    ) -> None:
        from ndvi.models import NdviObservation

        user = django_user_model.objects.create_user(
            username="ndmi-no-persist",
            email="ndmi-no-persist@example.com",
            password=PASSWORD,
        )
        farm = Farm.objects.create(
            owner=user,
            name="NDMI No Persist",
            slug="ndmi-no-persist",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        kwargs = dict(ndmi_obs_kwargs)
        kwargs["farm"] = farm
        obs = NdviObservation.objects.create(**kwargs)
        result, derived = process_ndmi_v1_to_v2(obs, persist=False)
        assert result is not None
        assert derived is None


# ── 2.3 — NDMIFusionEngine integration tests ──────────────────────────


class TestNDMIFusionEngine:
    """Integration tests for NDMIFusionEngine and run_ndmi_fusion."""

    @pytest.mark.django_db
    @override_settings(
        NDMI_MIN_ROLLING_CONTEXT=0,
        NDMI_MAX_CONFIDENCE_WITHOUT_CONTEXT=0.80,
    )
    def test_fusion_selects_candidate(self, django_user_model: Any) -> None:
        user = django_user_model.objects.create_user(
            username="ndmi-fusion-test",
            email="ndmi-fusion-test@example.com",
            password=PASSWORD,
        )
        farm = Farm.objects.create(
            owner=user,
            name="NDMI Fusion Test",
            slug="ndmi-fusion-test",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        from ndvi.models import NdviObservation

        NdviObservation.objects.create(
            farm=farm,
            engine="sentinel-2",
            bucket_date=date(2025, 1, 1),
            mean=0.15,
            is_latest=True,
            state="FINAL",
            cloud_fraction=0.0,
            valid_pixel_fraction=1.0,
            acquired_at=datetime(2025, 1, 1, 10, 0, 0, tzinfo=UTC),
            index_type="NDMI",
        )
        result = run_ndmi_fusion(
            farm_id=farm.id,
            bucket_date=date(2025, 1, 1),
        )
        assert result is not None
        assert result.selected is not None
        assert result.candidates_evaluated >= 1

    @pytest.mark.django_db
    def test_fusion_no_candidates(self, django_user_model: Any) -> None:
        user = django_user_model.objects.create_user(
            username="ndmi-fusion-none2",
            email="ndmi-fusion-none2@example.com",
            password=PASSWORD,
        )
        farm = Farm.objects.create(
            owner=user,
            name="NDMI Fusion None",
            slug="ndmi-fusion-none2",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        result = run_ndmi_fusion(
            farm_id=farm.id,
            bucket_date=date(2025, 1, 1),
        )
        assert result.selected is None
        assert result.candidates_evaluated == 0

    @pytest.mark.django_db
    @override_settings(
        NDMI_MIN_ROLLING_CONTEXT=0,
        NDMI_MAX_CONFIDENCE_WITHOUT_CONTEXT=0.80,
    )
    def test_fusion_water_class_set(self, django_user_model: Any) -> None:
        user = django_user_model.objects.create_user(
            username="ndmi-fusion-water2",
            email="ndmi-fusion-water2@example.com",
            password=PASSWORD,
        )
        farm = Farm.objects.create(
            owner=user,
            name="NDMI Fusion Water2",
            slug="ndmi-fusion-water2",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        from ndvi.models import NdviObservation

        NdviObservation.objects.create(
            farm=farm,
            engine="sentinel-2",
            bucket_date=date(2025, 1, 1),
            mean=0.35,
            is_latest=True,
            state="FINAL",
            cloud_fraction=0.0,
            valid_pixel_fraction=1.0,
            acquired_at=datetime(2025, 1, 1, 10, 0, 0, tzinfo=UTC),
            index_type="NDMI",
        )
        result = run_ndmi_fusion(
            farm_id=farm.id,
            bucket_date=date(2025, 1, 1),
        )
        assert result.selected is not None
        assert result.water_class == "high_moisture"

    @pytest.mark.django_db
    @override_settings(
        NDMI_MIN_ROLLING_CONTEXT=0,
        NDMI_MAX_CONFIDENCE_WITHOUT_CONTEXT=0.80,
    )
    def test_fusion_prefers_sentinel2_over_landsat(
        self, django_user_model: Any
    ) -> None:
        user = django_user_model.objects.create_user(
            username="ndmi-fusion-pref",
            email="ndmi-fusion-pref@example.com",
            password=PASSWORD,
        )
        farm = Farm.objects.create(
            owner=user,
            name="NDMI Fusion Pref",
            slug="ndmi-fusion-pref",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        from ndvi.models import NdviObservation

        NdviObservation.objects.create(
            farm=farm,
            engine="sentinel-2",
            bucket_date=date(2025, 1, 1),
            mean=0.20,
            is_latest=True,
            state="FINAL",
            cloud_fraction=0.0,
            valid_pixel_fraction=0.8,
            acquired_at=datetime(2025, 1, 1, 10, 0, 0, tzinfo=UTC),
            index_type="NDMI",
        )
        NdviObservation.objects.create(
            farm=farm,
            engine="landsat",
            bucket_date=date(2025, 1, 1),
            mean=0.30,
            is_latest=True,
            state="FINAL",
            cloud_fraction=0.0,
            valid_pixel_fraction=0.8,
            acquired_at=datetime(2025, 1, 1, 10, 10, 0, tzinfo=UTC),
            index_type="NDMI",
        )
        result = run_ndmi_fusion(
            farm_id=farm.id,
            bucket_date=date(2025, 1, 1),
        )
        assert result.selected is not None
        # Sentinel-2 has higher priority
        assert result.selected.source == "sentinel-2"
        assert result.selected.selected_value == pytest.approx(0.20, abs=0.01)

    @pytest.mark.django_db
    @override_settings(
        NDMI_MIN_ROLLING_CONTEXT=0,
        NDMI_MAX_CONFIDENCE_WITHOUT_CONTEXT=0.80,
    )
    def test_fusion_discards_low_confidence(
        self, django_user_model: Any
    ) -> None:
        """Test low-confidence candidates are discarded."""
        user = django_user_model.objects.create_user(
            username="ndmi-fusion-lowconf",
            email="ndmi-fusion-lowconf@example.com",
            password=PASSWORD,
        )
        farm = Farm.objects.create(
            owner=user,
            name="NDMI Fusion LowConf",
            slug="ndmi-fusion-lowconf",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        from ndvi.models import NdviObservation

        # Create observation with high cloud fraction -> low confidence
        NdviObservation.objects.create(
            farm=farm,
            engine="sentinel-2",
            bucket_date=date(2025, 1, 1),
            mean=0.15,
            is_latest=True,
            state="FINAL",
            cloud_fraction=0.00,
            valid_pixel_fraction=0.99,  # good pixels -> decent confidence
            acquired_at=datetime(2025, 1, 1, 10, 0, 0, tzinfo=UTC),
            index_type="NDMI",
        )
        engine = NDMIFusionEngine()
        candidates = engine.gather_candidates(
            farm_id=farm.id,
            bucket_date=date(2025, 1, 1),
        )
        # The candidate should pass quality and be gathered
        assert len(candidates) >= 1

    @pytest.mark.django_db
    @override_settings(
        NDMI_MIN_ROLLING_CONTEXT=0,
        NDMI_MAX_CONFIDENCE_WITHOUT_CONTEXT=0.80,
        NDMI_LOW_CONFIDENCE_THRESHOLD=0.99,  # High threshold = discard
    )
    def test_gather_candidates_discards_low_confidence(
        self, django_user_model: Any
    ) -> None:
        """Test gather_candidates discards when confidence < threshold."""
        user = django_user_model.objects.create_user(
            username="ndmi-gather-discard",
            email="ndmi-gather-discard@example.com",
            password=PASSWORD,
        )
        farm = Farm.objects.create(
            owner=user,
            name="NDMI Gather Discard",
            slug="ndmi-gather-discard",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        from ndvi.models import NdviObservation

        NdviObservation.objects.create(
            farm=farm,
            engine="sentinel-2",
            bucket_date=date(2025, 1, 1),
            mean=0.15,
            is_latest=True,
            state="FINAL",
            cloud_fraction=0.0,
            valid_pixel_fraction=0.8,
            acquired_at=datetime(2025, 1, 1, 10, 0, 0, tzinfo=UTC),
            index_type="NDMI",
        )
        engine = NDMIFusionEngine()
        candidates = engine.gather_candidates(
            farm_id=farm.id,
            bucket_date=date(2025, 1, 1),
        )
        # High threshold (0.99) should filter out all candidates
        assert len(candidates) == 0

    @pytest.mark.django_db
    @override_settings(
        NDMI_MIN_ROLLING_CONTEXT=0,
        NDMI_MAX_CONFIDENCE_WITHOUT_CONTEXT=0.80,
    )
    def test_fusion_not_implemented_error(self) -> None:
        """Test base gather_candidates raises NotImplementedError."""
        from science.fusion.base import FusionEngine

        class _MinimalEngine(FusionEngine):
            SOURCE_PRIORITY = []

            def select_candidate(self, candidates: Any) -> Any:
                from science.fusion.base import FusionResult

                return FusionResult()

        with pytest.raises(NotImplementedError):
            engine = _MinimalEngine()
            engine.gather_candidates(
                farm_id=1,
                bucket_date=date(2025, 1, 1),
                index_type="NDMI",
            )

    def test_ndmi_fusion_engine_class_vars(self) -> None:
        """Test NDMIFusionEngine class variable configuration."""
        assert NDMIFusionEngine.SOURCE_PRIORITY == [
            "sentinel-2",
            "sentinelhub",
            "stac",
            "landsat",
            "modis",
        ]
        assert NDMIFusionEngine.CONFIDENCE_DEGRADATION["sentinel-2"] == 1.0
        assert NDMIFusionEngine.CONFIDENCE_DEGRADATION["modis"] == 0.8
        assert (
            NDMIFusionEngine.SOURCE_CONFIDENCE_THRESHOLDS["sentinel-2"] == 0.75
        )

    def test_select_candidate_fallback_source_match(self) -> None:
        """Test fallback source matching when value match fails."""
        from science.fusion.base import FusionCandidate
        from science.fusion.ndmi import NDMIFusionEngine

        engine = NDMIFusionEngine()

        # Create candidates where value won't match exactly
        candidates = [
            FusionCandidate(
                source="sentinel-2",
                bucket_date=date(2025, 1, 1),
                selected_value=0.25,
                confidence=0.9,
                degraded_confidence=0.9,
            ),
            FusionCandidate(
                source="landsat",
                bucket_date=date(2025, 1, 1),
                selected_value=0.30,
                confidence=0.7,
                degraded_confidence=0.63,
            ),
        ]

        result = engine.select_candidate(candidates)
        # Should select the highest-priority candidate
        assert result.selected is not None
        assert result.selected.source == "sentinel-2"
        assert result.candidates_evaluated == 2


# ── 2.4 — ndvi/colormaps/ndmi.py tests ────────────────────────────────


class TestNdmiColormap:
    """Tests for the NDMI colormap module."""

    def test_colormap_registry_has_ndmi(self) -> None:
        from ndvi.colormaps.ndmi import COLORMAP_REGISTRY

        assert "NDMI" in COLORMAP_REGISTRY

    def test_colormap_name_is_brbg(self) -> None:
        from ndvi.colormaps.ndmi import COLORMAP_REGISTRY

        assert COLORMAP_REGISTRY["NDMI"]["colormap_name"] == "BrBG"

    def test_default_min_is_neg_02(self) -> None:
        from ndvi.colormaps.ndmi import COLORMAP_REGISTRY

        assert COLORMAP_REGISTRY["NDMI"]["default_min"] == -0.2

    def test_default_max_is_08(self) -> None:
        from ndvi.colormaps.ndmi import COLORMAP_REGISTRY

        assert COLORMAP_REGISTRY["NDMI"]["default_max"] == 0.8

    def test_control_points_shape(self) -> None:
        from ndvi.colormaps.ndmi import COLORMAP_REGISTRY

        ctrl = COLORMAP_REGISTRY["NDMI"]["control_points"]
        assert ctrl.shape == (11, 3)

    def test_control_points_dtype(self) -> None:
        from ndvi.colormaps.ndmi import COLORMAP_REGISTRY

        ctrl = COLORMAP_REGISTRY["NDMI"]["control_points"]
        assert "float" in str(ctrl.dtype)

    def test_exported_constants(self) -> None:
        from ndvi.colormaps.ndmi import (
            NDMI_COLORMAP_NAME,
            NDMI_DEFAULT_MAX,
            NDMI_DEFAULT_MIN,
        )

        assert NDMI_COLORMAP_NAME == "BrBG"
        assert NDMI_DEFAULT_MIN == -0.2
        assert NDMI_DEFAULT_MAX == 0.8

    def test_description_has_diverging(self) -> None:
        from ndvi.colormaps.ndmi import COLORMAP_REGISTRY

        desc = COLORMAP_REGISTRY["NDMI"]["description"]
        assert "Diverging" in desc
        assert "NDMI" in desc


# ── 2.5 — ndvi/metrics.py NDMI metric tests ───────────────────────────


class TestNdmiMetrics:
    """Tests that NDMI-specific metrics exist and are properly typed."""

    def test_ndmi_observations_ingested_total_exists(self) -> None:
        from ndvi.metrics import ndmi_observations_ingested_total

        assert callable(ndmi_observations_ingested_total.labels)

    def test_ndmi_observations_null_total_exists(self) -> None:
        from ndvi.metrics import ndmi_observations_null_total

        assert callable(ndmi_observations_null_total.labels)

    def test_ndmi_compute_duration_seconds_exists(self) -> None:
        from ndvi.metrics import ndmi_compute_duration_seconds

        assert callable(ndmi_compute_duration_seconds.labels)

    def test_ndmi_cache_hit_ratio_exists(self) -> None:
        from ndvi.metrics import ndmi_cache_hit_ratio

        assert callable(ndmi_cache_hit_ratio.labels)

    def test_ndmi_job_duration_seconds_exists(self) -> None:
        from ndvi.metrics import ndmi_job_duration_seconds

        assert callable(ndmi_job_duration_seconds.labels)

    def test_ndmi_metrics_label_names(self) -> None:
        from ndvi.metrics import ndmi_observations_ingested_total

        # Verify label names
        assert ndmi_observations_ingested_total._labelnames == (
            "engine",
            "status",
        )

    def test_ndmi_null_metrics_label_names(self) -> None:
        from ndvi.metrics import ndmi_observations_null_total

        assert ndmi_observations_null_total._labelnames == (
            "engine",
            "null_reason",
        )

    def test_ndmi_compute_duration_label_names(self) -> None:
        from ndvi.metrics import ndmi_compute_duration_seconds

        assert ndmi_compute_duration_seconds._labelnames == ("step",)

    def test_ndmi_cache_hit_label_names(self) -> None:
        from ndvi.metrics import ndmi_cache_hit_ratio

        assert ndmi_cache_hit_ratio._labelnames == ("level",)

    def test_ndmi_job_duration_label_names(self) -> None:
        from ndvi.metrics import ndmi_job_duration_seconds

        assert ndmi_job_duration_seconds._labelnames == ("queue", "status")


# ── 2.1 — ndvi/tasks.py NDMI task tests ───────────────────────────────


class TestNdmiTasksModule:
    """Tests that NDMI task functions exist and have correct signatures."""

    def test_enqueue_daily_ndmi_refresh_exists(self) -> None:
        from ndvi.tasks import enqueue_daily_ndmi_refresh

        assert callable(enqueue_daily_ndmi_refresh)

    def test_enqueue_ndmi_gap_fill_exists(self) -> None:
        from ndvi.tasks import enqueue_ndmi_gap_fill

        assert callable(enqueue_ndmi_gap_fill)

    def test_ndmi_tasks_are_shared_tasks(self) -> None:
        from ndvi.tasks import (
            enqueue_daily_ndmi_refresh,
            enqueue_ndmi_gap_fill,
        )

        # Both should have the 'delay' method that Celery shared_task adds
        assert hasattr(enqueue_daily_ndmi_refresh, "delay")
        assert hasattr(enqueue_ndmi_gap_fill, "delay")

    @pytest.mark.django_db
    @override_settings(CELERY_TASK_ALWAYS_EAGER=True)
    def test_enqueue_daily_ndmi_refresh_with_no_farms(self) -> None:
        from ndvi.tasks import enqueue_daily_ndmi_refresh

        result = enqueue_daily_ndmi_refresh()
        assert result == 0

    @pytest.mark.django_db
    @override_settings(CELERY_TASK_ALWAYS_EAGER=True)
    @patch("ndvi.tasks.dispatch_ndvi_job")
    def test_enqueue_daily_ndmi_refresh_with_active_farm(
        self, mock_dispatch: Any, django_user_model: Any
    ) -> None:
        user = django_user_model.objects.create_user(
            username="ndmi-task-farm",
            email="ndmi-task-farm@example.com",
            password=PASSWORD,
        )
        Farm.objects.create(
            owner=user,
            name="NDMI Task Farm",
            slug="ndmi-task-farm",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        from ndvi.tasks import enqueue_daily_ndmi_refresh

        result = enqueue_daily_ndmi_refresh()
        assert result >= 1
        mock_dispatch.assert_called_once()

    @pytest.mark.django_db
    @override_settings(CELERY_TASK_ALWAYS_EAGER=True)
    def test_enqueue_ndmi_gap_fill_with_no_farms(self) -> None:
        from ndvi.tasks import enqueue_ndmi_gap_fill

        result = enqueue_ndmi_gap_fill()
        assert result == 0

    @pytest.mark.django_db
    @override_settings(CELERY_TASK_ALWAYS_EAGER=True)
    @patch("ndvi.tasks.dispatch_ndvi_job")
    def test_enqueue_ndmi_gap_fill_with_active_farm(
        self, mock_dispatch: Any, django_user_model: Any
    ) -> None:
        user = django_user_model.objects.create_user(
            username="ndmi-gap-farm",
            email="ndmi-gap-farm@example.com",
            password=PASSWORD,
        )
        Farm.objects.create(
            owner=user,
            name="NDMI Gap Farm",
            slug="ndmi-gap-farm",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        from ndvi.tasks import enqueue_ndmi_gap_fill

        result = enqueue_ndmi_gap_fill()
        assert result >= 1
        mock_dispatch.assert_called_once()

    @pytest.mark.django_db
    @override_settings(CELERY_TASK_ALWAYS_EAGER=True)
    @patch("ndvi.tasks.dispatch_ndvi_job")
    def test_farm_without_bbox_is_skipped(
        self, mock_dispatch: Any, django_user_model: Any
    ) -> None:
        user = django_user_model.objects.create_user(
            username="ndmi-no-bbox",
            email="ndmi-no-bbox@example.com",
            password=PASSWORD,
        )
        Farm.objects.create(
            owner=user,
            name="NDMI No Bbox",
            slug="ndmi-no-bbox",
            is_active=True,
        )
        from ndvi.tasks import (
            enqueue_daily_ndmi_refresh,
            enqueue_ndmi_gap_fill,
        )

        assert enqueue_daily_ndmi_refresh() == 0
        assert enqueue_ndmi_gap_fill() == 0
        mock_dispatch.assert_not_called()


# ── 2.2 & 2.3 — Registry tests ────────────────────────────────────────


class TestNdmiRegistries:
    """Tests NDMI registration in QUALITY_SCORERS and FUSION_ENGINES."""

    def test_quality_scorer_registered(self) -> None:
        assert "NDMI" in QUALITY_SCORERS
        assert QUALITY_SCORERS["NDMI"] is NDMIQualityScorer

    def test_get_quality_scorer_returns_instance(self) -> None:
        scorer = get_quality_scorer("NDMI")
        assert isinstance(scorer, NDMIQualityScorer)
        assert isinstance(scorer, QualityScorer)

    def test_get_quality_scorer_unknown_raises(self) -> None:
        with pytest.raises(KeyError, match="NONEXISTENT"):
            get_quality_scorer("NONEXISTENT")

    def test_fusion_engine_registered(self) -> None:
        from science.fusion.base import FUSION_ENGINES

        assert "NDMI" in FUSION_ENGINES
        assert FUSION_ENGINES["NDMI"] is NDMIFusionEngine

    def test_get_fusion_engine_from_base(self) -> None:
        engine = get_fusion_engine("NDMI")
        assert isinstance(engine, NDMIFusionEngine)

    def test_get_fusion_engine_unknown_raises(self) -> None:
        with pytest.raises(KeyError, match="NONEXISTENT"):
            get_fusion_engine("NONEXISTENT")


# ── 2.3 — Base class tests ────────────────────────────────────────────


class TestFusionBaseClasses:
    """Tests for science/fusion/base.py dataclasses and ABC."""

    def test_fusion_candidate_defaults(self) -> None:
        from science.fusion.base import FusionCandidate

        candidate = FusionCandidate(
            source="sentinel-2",
            bucket_date=date(2025, 1, 1),
            selected_value=0.5,
            confidence=0.8,
        )
        assert candidate.source == "sentinel-2"
        assert candidate.selected_value == 0.5
        assert candidate.confidence == 0.8
        assert candidate.is_selected is False
        assert candidate.selection_reason is None

    def test_fusion_result_defaults(self) -> None:
        from science.fusion.base import FusionResult

        result = FusionResult()
        assert result.selected is None
        assert result.candidates_evaluated == 0
        assert result.candidates_discarded == 0
        assert result.decision_reason == ""
        assert result.conflict_detected is False


class TestQualityBaseClasses:
    """Tests for science/quality/base.py dataclasses and ABC."""

    def test_quality_result_defaults(self) -> None:
        result = QualityResult(
            selected_value=0.5,
            smoothed_value=0.52,
            confidence=0.8,
        )
        assert result.selected_value == 0.5
        assert result.smoothed_value == 0.52
        assert result.confidence == 0.8
        assert result.is_null is False
        assert result.null_reason is None
        assert result.confidence_components == {}
        assert result.quality_flags == {}

    def test_quality_result_null_defaults(self) -> None:
        result = QualityResult(
            selected_value=None,
            smoothed_value=None,
            confidence=0.0,
            is_null=True,
            null_reason="test_reason",
        )
        assert result.selected_value is None
        assert result.is_null
        assert result.null_reason == "test_reason"


# ── 2.5 — CELERY_BEAT_SCHEDULE conformance ────────────────────────────


class TestCeleryBeatSchedule:
    """Verify NDMI entries in CELERY_BEAT_SCHEDULE."""

    def test_ndmi_refresh_in_settings(self) -> None:
        from django.conf import settings

        schedule = settings.CELERY_BEAT_SCHEDULE
        assert "ndmi-daily-refresh" in schedule
        entry = schedule["ndmi-daily-refresh"]
        assert entry["task"] == "ndvi.tasks.enqueue_daily_ndmi_refresh"

    def test_ndmi_gap_fill_in_settings(self) -> None:
        from django.conf import settings

        schedule = settings.CELERY_BEAT_SCHEDULE
        assert "ndmi-gap-fill" in schedule
        entry = schedule["ndmi-gap-fill"]
        assert entry["task"] == "ndvi.tasks.enqueue_ndmi_gap_fill"


class TestRenderNdmiPng:
    """Tests for rendering NDMI PNGs."""

    @pytest.mark.django_db
    @override_settings(
        NDVI_RASTER_ENGINE_NAME="sentinelhub",
        NDVI_RASTER_ENGINE_PATH="ndvi.tests.fakes.FakeRasterEngine",
        NDVI_RASTER_ENGINE_PATH_STAC="ndvi.tests.fakes.FakeRasterEngine",
    )
    def test_render_ndmi_png(self, django_user_model: Any) -> None:
        from ndvi.engines.base import BBox
        from ndvi.raster.service import render_ndmi_png

        user = django_user_model.objects.create_user(
            username="raster-user", password=PASSWORD
        )
        farm = Farm.objects.create(
            owner=user,
            name="NDMI Raster Farm",
            slug="ndmi-raster-farm",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        from decimal import Decimal

        content, content_hash = render_ndmi_png(
            farm=farm,
            bbox=BBox(
                Decimal("0.0"), Decimal("0.0"), Decimal("0.2"), Decimal("0.2")
            ),
            day=date(2025, 1, 1),
            size=256,
            max_cloud=10,
        )
        assert content.startswith(b"\x89PNG")
        assert len(content_hash) == 64


class TestNdmiV2Pipeline:
    """Tests for NDMI V2 quality + fusion pipeline."""

    @pytest.mark.django_db
    @patch("ndvi.tasks.process_ndmi_v1_to_v2")
    @patch("ndvi.tasks.run_ndmi_fusion")
    def test_run_ndmi_v2_pipeline(
        self, mock_fusion: Any, mock_process: Any, django_user_model: Any
    ) -> None:
        from ndvi.models import NdviJob, NdviObservation
        from ndvi.tasks import _run_ndmi_v2_pipeline

        user = django_user_model.objects.create_user(
            username="pipeline-user", password=PASSWORD
        )
        farm = Farm.objects.create(
            owner=user,
            name="NDMI Pipeline Farm",
            slug="ndmi-pipeline-farm",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        job = NdviJob.objects.create(
            owner=user,
            farm=farm,
            job_type=NdviJob.JobType.REFRESH_LATEST,
            index_type="NDMI",
        )
        obs = NdviObservation.objects.create(
            farm=farm,
            bucket_date=date(2025, 1, 1),
            mean=0.5,
        )
        _run_ndmi_v2_pipeline([obs], job)
        mock_process.assert_called_once_with(obs, persist=True)
        mock_fusion.assert_called_once_with(farm.id, date(2025, 1, 1))

    @pytest.mark.django_db
    @patch(
        "ndvi.tasks.process_ndmi_v1_to_v2",
        side_effect=Exception("process error"),
    )
    @patch("ndvi.tasks.run_ndmi_fusion", side_effect=Exception("fusion error"))
    def test_run_ndmi_v2_pipeline_handles_exceptions(
        self, mock_fusion: Any, mock_process: Any, django_user_model: Any
    ) -> None:
        from ndvi.models import NdviJob, NdviObservation
        from ndvi.tasks import _run_ndmi_v2_pipeline

        user = django_user_model.objects.create_user(
            username="pipeline-user-2", password=PASSWORD
        )
        farm = Farm.objects.create(
            owner=user,
            name="NDMI Pipeline Farm 2",
            slug="ndmi-pipeline-farm-2",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        job = NdviJob.objects.create(
            owner=user,
            farm=farm,
            job_type=NdviJob.JobType.REFRESH_LATEST,
            index_type="NDMI",
        )
        obs = NdviObservation.objects.create(
            farm=farm,
            bucket_date=date(2025, 1, 1),
            mean=0.5,
        )
        # Should catch exceptions and not crash
        _run_ndmi_v2_pipeline([obs], job)
        mock_process.assert_called_once_with(obs, persist=True)
        mock_fusion.assert_called_once_with(farm.id, date(2025, 1, 1))

    @pytest.mark.django_db
    @override_settings(
        NDVI_RASTER_ENGINE_NAME="sentinelhub",
        NDVI_RASTER_ENGINE_PATH="ndvi.tests.fakes.FakeRasterEngine",
        NDVI_RASTER_ENGINE_PATH_STAC="ndvi.tests.fakes.FakeRasterEngine",
    )
    @patch("ndvi.tasks.acquire_lock", return_value=True)
    def test_run_ndvi_job_raster_ndmi(
        self, mock_lock: Any, django_user_model: Any
    ) -> None:
        from ndvi.models import NdviJob, NdviRasterArtifact
        from ndvi.tasks import run_ndvi_job

        user = django_user_model.objects.create_user(
            username="raster-ndmi", password=PASSWORD
        )
        farm = Farm.objects.create(
            owner=user,
            name="NDMI Job Farm",
            slug="ndmi-job-farm",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        from django.conf import settings

        job = NdviJob.objects.create(
            owner=user,
            farm=farm,
            engine=getattr(settings, "NDVI_RASTER_ENGINE_NAME", "sentinelhub"),
            job_type=NdviJob.JobType.RASTER_PNG,
            index_type="NDMI",
            start=date(2025, 1, 1),
            end=date(2025, 1, 1),
            step_days=256,
            max_cloud=10,
        )
        result = run_ndvi_job(job.id)
        assert result == "ok"
        assert NdviRasterArtifact.objects.filter(farm=farm).count() == 1
