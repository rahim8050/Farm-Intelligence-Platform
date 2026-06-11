"""Edge case and coverage gap tests for ndvi services, farm_state, tasks.

Tests focus on uncovered branches identified by coverage analysis:
- Error/exception handlers
- Pathological inputs (empty, invalid)
- Wrapper/cache functions
- NDWI-specific branches not exercised by existing tests
"""

from __future__ import annotations

import secrets
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from django.core.cache import caches
from rest_framework.exceptions import ValidationError

from farms.models import Farm
from ndvi.engines.sentinelhub import SentinelHubAuthError
from ndvi.farm_state import (
    _acquire_coverage_lock,
    _coverage_pct_from_ndvi_array,
    _enqueue_coverage_compute,
    cache_coverage_for_farm,
    compute_coverage_for_farm,
    get_cached_coverage_for_farm,
)
from ndvi.fusion import FusionCandidate
from ndvi.models import NdviObservation
from ndvi.raster.base import ColormapNormalization
from ndvi.raster.service import render_ndwi_png
from ndvi.services import (
    LatestParams,
    TimeseriesParams,
    _parse_prerelease,
    acquire_lock,
    cache_ndwi_latest_response,
    cache_ndwi_timeseries_response,
    detect_gaps,
    enforce_quota,
    expected_buckets,
    get_cached_ndwi_latest_response,
    get_cached_ndwi_timeseries_response,
    get_default_colormap_normalization,
    get_engine,
    get_latest_observations,
    get_valid_observations_qs,
    normalize_bbox,
    release_lock,
)
from ndvi.stac_client import StacWafBlockedError
from ndvi.tasks import _parse_date, _safe_error_message
from ndvi.v2_quality import build_v2_observation

PASSWORD = secrets.token_urlsafe(12)


# ---------------------------------------------------------------------------
# ndvi/services.py -- small utility functions
# ---------------------------------------------------------------------------


class TestParsePrerelease:
    """_parse_prerelease covers lines 140-145."""

    def test_tag_with_number(self) -> None:
        assert _parse_prerelease("rc2") == ("rc", 2)

    def test_tag_without_number(self) -> None:
        assert _parse_prerelease("beta") == ("beta", 1)

    def test_alpha_with_number(self) -> None:
        assert _parse_prerelease("alpha3") == ("alpha", 3)

    def test_unknown_tag(self) -> None:
        assert _parse_prerelease("release") == ("release", 0)

    def test_blank_tag(self) -> None:
        assert _parse_prerelease("random") == ("release", 0)


class TestExpectedBucketsAndGaps:
    """expected_buckets and detect_gaps (lines 1147-1162)."""

    def test_expected_buckets_generates_dates(self) -> None:
        buckets = expected_buckets(
            start=date(2024, 1, 1),
            end=date(2024, 1, 5),
            step_days=2,
        )
        assert buckets == [
            date(2024, 1, 1),
            date(2024, 1, 3),
            date(2024, 1, 5),
        ]

    def test_expected_buckets_single_day(self) -> None:
        buckets = expected_buckets(
            start=date(2024, 1, 1),
            end=date(2024, 1, 1),
            step_days=1,
        )
        assert buckets == [date(2024, 1, 1)]

    def test_detect_gaps_returns_missing(self) -> None:
        existing = {date(2024, 1, 1), date(2024, 1, 5)}
        expected = [date(2024, 1, 1), date(2024, 1, 3), date(2024, 1, 5)]
        gaps = detect_gaps(existing, expected)
        assert gaps == [date(2024, 1, 3)]

    def test_detect_gaps_no_gaps(self) -> None:
        existing = {date(2024, 1, 1), date(2024, 1, 3), date(2024, 1, 5)}
        expected = [date(2024, 1, 1), date(2024, 1, 3), date(2024, 1, 5)]
        gaps = detect_gaps(existing, expected)
        assert gaps == []


class TestGetColormapNormalization:
    """get_default_colormap_normalization - invalid setting (lines 852-858)."""

    def test_invalid_mode_returns_default(self, settings: Any) -> None:
        settings.NDVI_COLORMAP_NORMALIZATION = "bogus_mode"
        result = get_default_colormap_normalization()
        assert result == ColormapNormalization.HISTOGRAM


class TestNormalizeBbox:
    """normalize_bbox raises ValidationError on invalid bbox (line 1055)."""

    @pytest.mark.django_db
    def test_invalid_bbox_raises_error(
        self,
        django_user_model: Any,
    ) -> None:
        user = django_user_model.objects.create_user(
            username="bboxfail",
            email="bboxfail@example.com",
            password=PASSWORD,
        )
        farm = Farm.objects.create(
            owner=user,
            name="Bad BBox",
            slug="bad-bbox",
            bbox_west=Decimal("1.0"),
            bbox_east=Decimal("0.0"),
            bbox_south=0.0,
            bbox_north=0.2,
            is_active=True,
        )
        with pytest.raises(ValidationError):
            normalize_bbox(farm)


class TestAcquireReleaseLock:
    """acquire_lock / release_lock (lines 1168-1205)."""

    def test_acquire_lock_success(self) -> None:
        caches["default"].clear()
        token = acquire_lock("test-lock-key", timeout=30)
        assert token is not None
        assert isinstance(token, str)

    def test_acquire_lock_failure(self) -> None:
        caches["default"].clear()
        token1 = acquire_lock("test-lock-key2", timeout=30)
        token2 = acquire_lock("test-lock-key2", timeout=30)
        assert token1 is not None
        assert token2 is None

    def test_release_lock_success(self) -> None:
        caches["default"].clear()
        token = acquire_lock("test-lock-key3", timeout=30)
        assert token is not None
        release_lock("test-lock-key3", token)
        token2 = acquire_lock("test-lock-key3", timeout=30)
        assert token2 is not None


# ---------------------------------------------------------------------------
# ndvi/services.py -- NDWI cache functions (lines 1274-1337)
# ---------------------------------------------------------------------------


class TestNdwiCacheFunctions:
    """NDWI-specific cache helpers for timeseries and latest responses."""

    def test_cache_and_get_ndwi_timeseries(self) -> None:
        caches["default"].clear()
        params = TimeseriesParams(
            start=date(2024, 1, 1),
            end=date(2024, 1, 5),
            step_days=1,
            max_cloud=30,
        )
        payload: dict[str, object] = {"data": [{"ndwi": 0.5}]}
        cache_ndwi_timeseries_response(
            owner_id=1,
            farm_id=1,
            engine="stac",
            params=params,
            payload=payload,
        )
        cached = get_cached_ndwi_timeseries_response(
            owner_id=1,
            farm_id=1,
            engine="stac",
            params=params,
        )
        assert cached == payload

    def test_cache_and_get_ndwi_latest(self) -> None:
        caches["default"].clear()
        params = LatestParams(lookback_days=30, max_cloud=30)
        payload: dict[str, object] = {"value": 0.42}
        cache_ndwi_latest_response(
            owner_id=1,
            farm_id=1,
            engine="stac",
            params=params,
            payload=payload,
        )
        cached = get_cached_ndwi_latest_response(
            owner_id=1,
            farm_id=1,
            engine="stac",
            params=params,
        )
        assert cached == payload

    def test_get_ndwi_timeseries_miss_returns_none(self) -> None:
        caches["default"].clear()
        params = TimeseriesParams(
            start=date(2024, 1, 1),
            end=date(2024, 1, 5),
            step_days=1,
            max_cloud=30,
        )
        result = get_cached_ndwi_timeseries_response(
            owner_id=999,
            farm_id=999,
            engine="stac",
            params=params,
        )
        assert result is None

    def test_get_ndwi_latest_miss_returns_none(self) -> None:
        caches["default"].clear()
        params = LatestParams(lookback_days=30, max_cloud=30)
        result = get_cached_ndwi_latest_response(
            owner_id=999,
            farm_id=999,
            engine="stac",
            params=params,
        )
        assert result is None


# ---------------------------------------------------------------------------
# ndvi/services.py -- get_engine NDWI branch (lines 978-1003)
# ---------------------------------------------------------------------------


class TestGetEngineNdwi:
    """Exercise real get_engine for NDWI index_type."""

    def test_get_engine_ndwi_sentinelhub(self) -> None:
        engine = get_engine("sentinelhub", index_type="NDWI")
        assert engine is not None

    def test_get_engine_ndwi_stac(self) -> None:
        engine = get_engine("stac", index_type="NDWI")
        assert engine is not None

    def test_get_engine_ndwi_gee(self) -> None:
        engine = get_engine("gee", index_type="NDWI")
        assert engine is not None

    def test_get_engine_ndwi_landsat(self) -> None:
        engine = get_engine("landsat", index_type="NDWI")
        assert engine is not None

    def test_get_engine_unsupported_ndvi(self) -> None:
        with pytest.raises(ValueError, match="Unsupported NDVI engine"):
            get_engine("bogus_engine")

    def test_get_engine_unsupported_ndwi(self) -> None:
        with pytest.raises(ValueError):
            get_engine("bogus_engine", index_type="NDWI")


# ---------------------------------------------------------------------------
# ndvi/services.py -- observation query filters (lines 1983-1988, 2020-2023)
# ---------------------------------------------------------------------------


class TestObservationQueryFilters:
    """Edge case filters on get_valid_observations_qs and get_latest."""

    @pytest.mark.django_db
    def test_get_valid_qs_with_start_end_and_cloud(
        self,
        django_user_model: Any,
    ) -> None:
        user = django_user_model.objects.create_user(
            username="obs-filter",
            email="obs-filter@example.com",
            password=PASSWORD,
        )
        farm = Farm.objects.create(
            owner=user,
            name="Filter Farm",
            slug="filter-farm",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        NdviObservation.objects.create(
            farm=farm,
            engine="stac",
            bucket_date=date(2024, 1, 5),
            mean=0.5,
            is_latest=True,
            state="FINAL",
            cloud_fraction=0.1,
        )
        qs = get_valid_observations_qs(
            farm=farm,
            engine="stac",
            start=date(2024, 1, 1),
            end=date(2024, 1, 10),
            max_cloud=50,
        )
        assert len(qs) == 1

    @pytest.mark.django_db
    def test_get_latest_observations_start_end(
        self,
        django_user_model: Any,
    ) -> None:
        user = django_user_model.objects.create_user(
            username="latest-filter",
            email="latest-filter@example.com",
            password=PASSWORD,
        )
        farm = Farm.objects.create(
            owner=user,
            name="Latest Farm",
            slug="latest-farm",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        NdviObservation.objects.create(
            farm=farm,
            engine="stac",
            bucket_date=date(2024, 1, 5),
            mean=0.5,
            is_latest=True,
            state="FINAL",
        )
        result = get_latest_observations(
            farm=farm,
            engine="stac",
            start=date(2024, 1, 1),
            end=date(2024, 1, 10),
        )
        assert len(result) == 1

    @pytest.mark.django_db
    def test_get_latest_observations_outside_range(
        self,
        django_user_model: Any,
    ) -> None:
        user = django_user_model.objects.create_user(
            username="latest-filter2",
            email="latest-filter2@example.com",
            password=PASSWORD,
        )
        farm = Farm.objects.create(
            owner=user,
            name="Latest Farm 2",
            slug="latest-farm-2",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        NdviObservation.objects.create(
            farm=farm,
            engine="stac",
            bucket_date=date(2024, 6, 1),
            mean=0.5,
            is_latest=True,
            state="FINAL",
        )
        result = get_latest_observations(
            farm=farm,
            engine="stac",
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
        )
        assert len(result) == 0


# ---------------------------------------------------------------------------
# ndvi/farm_state.py -- cache/lock/coverage helpers (lines 279-503)
# ---------------------------------------------------------------------------


class TestAcquireCoverageLock:
    """_acquire_coverage_lock (lines 279-294)."""

    def test_acquire_lock_succeeds(self) -> None:
        caches["default"].clear()
        result = _acquire_coverage_lock(
            farm_id=1,
            engine="stac",
            target_date=date(2024, 6, 1),
            threshold=0.5,
        )
        assert result is True

    def test_acquire_lock_fails_when_held(self) -> None:
        caches["default"].clear()
        _acquire_coverage_lock(
            farm_id=2,
            engine="stac",
            target_date=date(2024, 6, 1),
            threshold=0.5,
        )
        result = _acquire_coverage_lock(
            farm_id=2,
            engine="stac",
            target_date=date(2024, 6, 1),
            threshold=0.5,
        )
        assert result is False


class TestEnqueueCoverageCompute:
    """_enqueue_coverage_compute (line 300)."""

    @patch("ndvi.farm_state.dispatch_farm_state_coverage")
    def test_enqueue_coverage_calls_dispatch(
        self,
        mock_dispatch: MagicMock,
    ) -> None:
        _enqueue_coverage_compute(
            farm_id=1,
            engine="stac",
            target_date=date(2024, 6, 1),
            threshold=0.5,
        )
        mock_dispatch.assert_called_once_with(
            farm_id=1,
            engine="stac",
            target_date=date(2024, 6, 1),
            threshold=0.5,
        )


class TestCoveragePctFromNdviArray:
    """_coverage_pct_from_ndvi_array edge cases (lines 345-355)."""

    def test_empty_array_returns_none(self) -> None:
        arr = np.array([])
        result = _coverage_pct_from_ndvi_array(arr, threshold=0.3)
        assert result is None

    def test_all_nan_returns_none(self) -> None:
        arr = np.array([np.nan, np.nan, np.nan])
        result = _coverage_pct_from_ndvi_array(arr, threshold=0.3)
        assert result is None

    def test_all_above_threshold(self) -> None:
        arr = np.array([0.5, 0.6, 0.7])
        result = _coverage_pct_from_ndvi_array(arr, threshold=0.3)
        assert result == 100.0

    def test_mixed_values(self) -> None:
        arr = np.array([0.2, 0.5, np.nan, 0.1, 0.8])
        result = _coverage_pct_from_ndvi_array(arr, threshold=0.3)
        assert result == 50.0

    def test_zero_valid_only(self) -> None:
        arr = np.array([np.inf, np.nan])
        result = _coverage_pct_from_ndvi_array(arr, threshold=0.3)
        assert result is None

    def test_all_below_threshold(self) -> None:
        arr = np.array([0.1, 0.2, 0.05])
        result = _coverage_pct_from_ndvi_array(arr, threshold=0.3)
        assert result == 0.0


class TestCoverageWrapperFunctions:
    """Wrapper: compute_coverage_for_farm, cache/get (lines 475-508)."""

    @pytest.mark.django_db
    @patch("ndvi.farm_state._compute_coverage_value", return_value=75.0)
    def test_compute_coverage_for_farm_wrapper(
        self,
        mock_compute: MagicMock,
        django_user_model: Any,
    ) -> None:
        user = django_user_model.objects.create_user(
            username="cov-wrap",
            email="cov-wrap@example.com",
            password=PASSWORD,
        )
        farm = Farm.objects.create(
            owner=user,
            name="Cov Wrap",
            slug="cov-wrap",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        result = compute_coverage_for_farm(
            farm=farm,
            engine="stac",
            target_date=date(2024, 6, 1),
            threshold=0.5,
        )
        assert result == 75.0
        mock_compute.assert_called_once_with(
            farm=farm,
            engine="stac",
            target_date=date(2024, 6, 1),
            threshold=0.5,
        )

    def test_cache_and_get_wrapper(self) -> None:
        caches["default"].clear()
        cache_coverage_for_farm(
            farm_id=42,
            engine="stac",
            target_date=date(2024, 6, 1),
            threshold=0.5,
            value=88.0,
        )
        found, value = get_cached_coverage_for_farm(
            farm_id=42,
            engine="stac",
            target_date=date(2024, 6, 1),
            threshold=0.5,
        )
        assert found is True
        assert value == 88.0

    def test_cache_miss_returns_false_none(self) -> None:
        caches["default"].clear()
        found, value = get_cached_coverage_for_farm(
            farm_id=999,
            engine="stac",
            target_date=date(2024, 6, 1),
            threshold=0.5,
        )
        assert found is False
        assert value is None


# ---------------------------------------------------------------------------
# ndvi/farm_state.py -- get_farm_state cached dict path (lines 554-579)
# ---------------------------------------------------------------------------


class TestFarmStateCachePaths:
    """get_farm_state cache-hit and lock-contention branches."""

    @pytest.mark.django_db
    def test_build_farm_state_returns_cached_payload(
        self,
        django_user_model: Any,
    ) -> None:
        from ndvi.farm_state import build_farm_state

        user = django_user_model.objects.create_user(
            username="fs-cache",
            email="fs-cache@example.com",
            password=PASSWORD,
        )
        farm = Farm.objects.create(
            owner=user,
            name="FS Cache",
            slug="fs-cache",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        caches["default"].clear()
        cache_key = f"farm_state:{farm.id}:stac"
        cached_payload = {
            "farm_id": farm.id,
            "state": "full_canopy",
            "mean_ndvi": 0.7,
            "max_ndvi": 0.9,
            "trend": 0.01,
            "coverage_pct": 85.0,
            "interpretation": "Full canopy",
            "action": "no_action",
        }
        caches["default"].set(cache_key, cached_payload, timeout=300)
        result = build_farm_state(farm=farm, engine="stac")
        assert result.state == "full_canopy"
        assert result.mean_ndvi == 0.7

    @pytest.mark.django_db
    def test_build_farm_state_lock_contention_no_cache(
        self,
        django_user_model: Any,
    ) -> None:
        from ndvi.farm_state import build_farm_state

        user = django_user_model.objects.create_user(
            username="fs-contention",
            email="fs-contention@example.com",
            password=PASSWORD,
        )
        farm = Farm.objects.create(
            owner=user,
            name="FS Contention",
            slug="fs-contention",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        caches["default"].clear()
        cache_key = f"farm_state:{farm.id}:stac"
        lock_key = f"{cache_key}:lock"
        caches["default"].set(lock_key, "1", timeout=30)

        result = build_farm_state(farm=farm, engine="stac")
        assert result.farm_id == farm.id
        assert result.state == "unknown"


# ---------------------------------------------------------------------------
# ndvi/tasks.py -- utility functions
# ---------------------------------------------------------------------------


class TestSafeErrorMessage:
    """_safe_error_message edge cases (lines 101-105)."""

    def test_unknown_string_returns_internal_error(self) -> None:
        assert _safe_error_message("unknown_code") == "internal_error"

    def test_stac_waf_blocked_error(self) -> None:
        error = StacWafBlockedError("WAF blocked", support_id="abc123")
        assert _safe_error_message(error) == "waf_blocked"

    def test_auth_error(self) -> None:
        error = SentinelHubAuthError("auth failed")
        assert _safe_error_message(error) == "auth_failed"


class TestParseDate:
    """_parse_date edge cases (lines 120-123)."""

    def test_invalid_date_returns_none(self) -> None:
        assert _parse_date("not-a-date") is None

    def test_none_input_returns_none(self) -> None:
        assert _parse_date(None) is None

    def test_valid_date(self) -> None:
        result = _parse_date("2024-06-01")
        assert result == date(2024, 6, 1)


# ---------------------------------------------------------------------------
# ndvi/fusion.py -- FusionCandidate property
# ---------------------------------------------------------------------------


class TestFusionCandidateBucketDate:
    """FusionCandidate.bucket_date property (line 100)."""

    @pytest.mark.django_db
    def test_bucket_date_delegates_to_v1(self) -> None:
        obs = NdviObservation(
            farm_id=1,
            engine="stac",
            bucket_date=date(2024, 6, 15),
            mean=0.5,
        )
        v2 = build_v2_observation(obs)
        candidate = FusionCandidate(
            v1_observation=obs,
            v2_result=v2,
            degraded_confidence=0.85,
        )
        assert candidate.bucket_date == date(2024, 6, 15)


# ---------------------------------------------------------------------------
# ndvi/raster/service.py -- render_ndwi_png (lines 100-114)
# ---------------------------------------------------------------------------


class TestRenderNdwiPng:
    """render_ndwi_png exercises the NDWI raster path."""

    @pytest.mark.django_db
    @patch("ndvi.raster.service.get_engine")
    def test_render_ndwi_png_returns_content_and_hash(
        self,
        mock_get_engine: MagicMock,
        django_user_model: Any,
    ) -> None:
        mock_engine = MagicMock()
        mock_engine.render_png.return_value = b"fake-png-bytes"
        mock_get_engine.return_value = mock_engine

        user = django_user_model.objects.create_user(
            username="png-ndwi",
            email="png-ndwi@example.com",
            password=PASSWORD,
        )
        farm = Farm.objects.create(
            owner=user,
            name="PNG NDWI",
            slug="png-ndwi",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )

        content, content_hash = render_ndwi_png(
            farm=farm,
            bbox=(0.0, 0.0, 0.2, 0.2),
            day=date(2024, 6, 1),
            size=256,
            max_cloud=30,
        )

        assert content == b"fake-png-bytes"
        assert isinstance(content_hash, str)
        assert len(content_hash) == 64
        mock_get_engine.assert_called_once()


# ---------------------------------------------------------------------------
# ndvi/services.py -- enforce_quota for large areas (line 1342)
# ---------------------------------------------------------------------------


class TestEnforceQuota:
    """enforce_quota raises ValidationError for large areas."""

    @pytest.mark.django_db
    def test_area_too_large_raises_error(
        self,
        django_user_model: Any,
        settings: Any,
    ) -> None:
        settings.NDVI_MAX_AREA_KM2 = 0.001
        user = django_user_model.objects.create_user(
            username="quota-test",
            email="quota-test@example.com",
            password=PASSWORD,
        )
        farm = Farm.objects.create(
            owner=user,
            name="Quota Test",
            slug="quota-test",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=1.0,
            bbox_east=1.0,
            is_active=True,
        )
        bbox = normalize_bbox(farm)
        with pytest.raises(ValidationError, match="NDVI_MAX_AREA_KM2"):
            enforce_quota(farm, bbox)
