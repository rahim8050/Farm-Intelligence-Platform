"""Tests for ndvi.quality_ndwi — NDWI-specific V2 quality scoring."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
from django.test import override_settings

from ndvi.quality_ndwi import build_ndwi_v2_observation


class TestNdwiNullConditions:
    """Unit tests for NDWI null conditions."""

    @pytest.fixture
    def ndwi_obs_kwargs(self) -> dict:
        return dict(
            farm_id=1,
            engine="sentinel-2",
            bucket_date=date(2025, 6, 1),
            mean=0.30,
            index_type="NDWI",
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
    @override_settings(NDWI_MIN_ROLLING_CONTEXT=0)
    def test_happy_path_returns_v2_result(self, ndwi_obs_kwargs: dict) -> None:
        obs = self._make_obs(**ndwi_obs_kwargs)
        result = build_ndwi_v2_observation(obs)
        assert result is not None
        assert result.is_null is False
        assert result.selected_ndvi == pytest.approx(0.30, abs=0.01)
        assert result.confidence > 0.0

    @pytest.mark.django_db
    @override_settings(NDWI_MIN_ROLLING_CONTEXT=0)
    def test_null_when_valid_pixel_below_threshold(
        self, ndwi_obs_kwargs: dict
    ) -> None:
        kwargs = dict(ndwi_obs_kwargs)
        kwargs["valid_pixel_fraction"] = 0.20
        obs = self._make_obs(**kwargs)
        result = build_ndwi_v2_observation(obs)
        assert result.is_null
        assert result.null_reason == "low_valid_pixel_fraction"

    @pytest.mark.django_db
    @override_settings(
        NDWI_MIN_ROLLING_CONTEXT=3,
        NDWI_MAX_CONFIDENCE_WITHOUT_CONTEXT=0.30,
    )
    def test_null_when_confidence_below_threshold(
        self, ndwi_obs_kwargs: dict
    ) -> None:
        kwargs = dict(ndwi_obs_kwargs)
        kwargs.update(valid_pixel_fraction=0.30, cloud_fraction=0.95)
        obs = self._make_obs(**kwargs)
        result = build_ndwi_v2_observation(obs)
        assert result.is_null
        assert result.null_reason == "low_confidence"

    @pytest.mark.django_db
    @override_settings(NDWI_MIN_ROLLING_CONTEXT=0)
    def test_null_when_mean_is_none(self, ndwi_obs_kwargs: dict) -> None:
        kwargs = dict(ndwi_obs_kwargs)
        kwargs["mean"] = None
        obs = self._make_obs(**kwargs)
        result = build_ndwi_v2_observation(obs)
        assert result.is_null
        assert result.null_reason == "missing_ndvi_value"


class TestNdwiConfidenceComponents:
    """Test NDWI-specific confidence component weights."""

    def test_source_weight_stac_is_highest(self) -> None:
        from ndvi.v2_quality import _get_source_weight

        weight_stac = _get_source_weight("stac")
        weight_landsat = _get_source_weight("landsat")
        assert weight_stac >= weight_landsat
