"""Tests for NDWI: colormap, farm state, quality engine, and fusion."""

from __future__ import annotations

import secrets
from datetime import UTC, date, timedelta
from io import BytesIO
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from django.contrib.auth import get_user_model
from django.core.cache import caches
from django.test import override_settings
from PIL import Image
from rest_framework import status
from rest_framework.test import APITestCase

from farms.models import Farm
from ndvi.farm_state_ndwi import (
    STATE_DECLINING,
    STATE_DRY,
    STATE_MOIST,
    STATE_SATURATED,
    STATE_UNKNOWN,
    STATE_WATER,
    _classify_ndwi_state,
    _get_dry_threshold,
    _get_saturated_threshold,
    _get_water_threshold,
    compute_ndwi_farm_state,
)
from ndvi.fusion_ndwi import run_ndwi_fusion
from ndvi.models import NdviObservation
from ndvi.quality_ndwi import build_ndwi_v2_observation
from ndvi.raster import png as png_module
from ndvi.raster.base import ColormapNormalization
from ndvi.raster.png import ndwi_to_png_bytes, ndwi_to_rgb
from ndvi.v2_quality import ConfidenceComponents, V2Result

# ── Colormap Tests ─────────────────────────────────────────────


def _decode_png(png_bytes: bytes) -> np.ndarray:
    with Image.open(BytesIO(png_bytes)) as image:
        rgb_image = image.convert("RGB")
        return np.array(rgb_image, dtype=np.uint8)


def test_ndwi_to_rgb_maps_extremes_to_expected_color_directions() -> None:
    ndwi = np.array([[-1.0, 0.0, 1.0]], dtype=np.float32)

    rgb = ndwi_to_rgb(ndwi, ColormapNormalization.FIXED)

    assert rgb.dtype == np.uint8
    assert rgb.shape == (1, 3, 3)

    neg_pixel = rgb[0, 0]
    mid_pixel = rgb[0, 1]
    pos_pixel = rgb[0, 2]

    assert neg_pixel[0] > neg_pixel[2]
    assert neg_pixel[1] > neg_pixel[2]
    assert mid_pixel[0] >= 200
    assert mid_pixel[1] >= 200
    assert pos_pixel[1] > pos_pixel[0]
    assert pos_pixel[2] > pos_pixel[0]


def test_ndwi_to_png_bytes_returns_valid_png() -> None:
    ndwi = np.array([[0.0, 0.5], [1.0, -1.0]], dtype=np.float32)

    png_bytes = ndwi_to_png_bytes(ndwi, ColormapNormalization.FIXED)

    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    decoded = _decode_png(png_bytes)
    assert decoded.shape == (2, 2, 3)


def test_ndwi_to_png_bytes_not_all_white() -> None:
    ndwi = np.array([[-1.0, -0.2], [0.4, 1.0]], dtype=np.float32)

    decoded = _decode_png(ndwi_to_png_bytes(ndwi, ColormapNormalization.FIXED))

    assert not np.all(decoded == 255)


def test_ndwi_to_rgb_maps_nan_to_zero() -> None:
    ndwi = np.array([[np.nan, 0.1]], dtype=np.float32)

    rgb = ndwi_to_rgb(ndwi, ColormapNormalization.FIXED)
    try:
        colormaps = png_module._load_matplotlib_colormaps()
    except ImportError:
        zero_color = png_module._fallback_brbg_bytes(
            np.array([[0.5]], dtype=np.float32)
        )[0, 0]
    else:
        zero_color = colormaps[png_module.NDWI_COLORMAP_NAME](
            np.array([[0.5]], dtype=np.float32),
            bytes=True,
        )[0, 0, :3]

    assert np.array_equal(rgb[0, 0], zero_color)
    assert not np.array_equal(rgb[0, 0], rgb[0, 1])


def test_ndwi_to_rgb_rejects_non_float_input() -> None:
    with pytest.raises(TypeError, match="float32 or float64"):
        ndwi_to_rgb(np.array([[0, 1]], dtype=np.int32))


def test_ndwi_to_rgb_rejects_non_2d_input() -> None:
    with pytest.raises(ValueError, match="two-dimensional"):
        ndwi_to_rgb(np.array([0.1, 0.2], dtype=np.float32))


def test_ndwi_to_rgb_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="empty"):
        ndwi_to_rgb(np.array([[]], dtype=np.float32))


def test_ndwi_to_rgb_rejects_constant_histogram() -> None:
    with pytest.raises(ValueError, match="no variation"):
        ndwi_to_rgb(np.array([[0.5, 0.5], [0.5, 0.5]], dtype=np.float32))


def test_ndwi_to_rgb_uses_fallback_when_matplotlib_unavailable() -> None:
    ndwi = np.array([[0.0, 1.0]], dtype=np.float32)
    cmap_patch = patch.object(
        png_module, "_load_matplotlib_colormaps", side_effect=ImportError
    )
    with cmap_patch:
        rgb = ndwi_to_rgb(ndwi, ColormapNormalization.FIXED)
    assert rgb.dtype == np.uint8
    assert rgb.shape == (1, 2, 3)


# ── Farm State Classification Tests ────────────────────────────


class TestClassifyNdwiState:
    def test_water_state(self) -> None:
        threshold = _get_water_threshold()
        state, _, _ = _classify_ndwi_state(threshold + 0.1, None)
        assert state == STATE_WATER

    def test_saturated_state(self) -> None:
        water = _get_water_threshold()
        sat = _get_saturated_threshold()
        mid = (sat + water) / 2
        state, _, _ = _classify_ndwi_state(mid, None)
        assert state == STATE_SATURATED

    def test_dry_state(self) -> None:
        dry = _get_dry_threshold()
        state, _, _ = _classify_ndwi_state(dry - 0.1, None)
        assert state == STATE_DRY

    def test_moist_state(self) -> None:
        sat = _get_saturated_threshold()
        dry = _get_dry_threshold()
        mid = (dry + sat) / 2
        state, _, _ = _classify_ndwi_state(mid, 0.0)
        assert state == STATE_MOIST

    def test_declining_state(self) -> None:
        sat = _get_saturated_threshold()
        dry = _get_dry_threshold()
        mid = (dry + sat) / 2
        state, _, _ = _classify_ndwi_state(mid, -0.02)
        assert state == STATE_DECLINING

    def test_unknown_when_none(self) -> None:
        state, _, _ = _classify_ndwi_state(None, None)
        assert state == STATE_UNKNOWN


# ── API Tests ──────────────────────────────────────────────────


class NdwiFarmStateApiTests(APITestCase):
    def setUp(self) -> None:
        caches["default"].clear()
        password = secrets.token_urlsafe(16)
        self.user = get_user_model().objects.create_user(
            username="owner",
            password=password,
            email="owner@example.com",
        )
        self.farm = Farm.objects.create(
            owner=self.user,
            name="Farm NDWI",
            slug="farm-ndwi",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        self.url = f"/api/v1/farms/{self.farm.id}/ndwi/farm-state/"
        self.client.force_authenticate(user=self.user)

    def _seed_observations(
        self, mean: float = 0.5, count: int = 5
    ) -> list[NdviObservation]:
        today = date.today()
        obs = []
        for i in range(count):
            o = NdviObservation.objects.create(
                farm=self.farm,
                engine="stac",
                bucket_date=today - timedelta(days=i),
                mean=mean,
                min=mean - 0.1,
                max=mean + 0.1,
                sample_count=100,
                cloud_fraction=0.05,
                index_type="NDWI",
                state=NdviObservation.ObservationState.FINAL,
            )
            obs.append(o)
        return obs

    def test_farm_state_returns_200(self) -> None:
        self._seed_observations(mean=0.5)
        response = self.client.get(self.url)
        assert response.status_code == status.HTTP_200_OK

    def test_farm_state_has_envelope(self) -> None:
        self._seed_observations(mean=-0.3)
        response = self.client.get(self.url)
        data: dict[str, Any] = response.json()
        assert "data" in data
        assert "message" in data
        assert "errors" in data or "success" in data

    def test_farm_state_moisture_classification(self) -> None:
        self._seed_observations(mean=0.5)
        response = self.client.get(self.url)
        data = response.json()
        payload = data.get("data", {})
        assert payload.get("mean_ndwi") == 0.5
        assert payload.get("state") is not None
        assert payload.get("interpretation") is not None
        assert payload.get("action") is not None
        assert payload.get("farm_id") == self.farm.id

    def test_farm_state_dry_moisture(self) -> None:
        self._seed_observations(mean=-0.3)
        response = self.client.get(self.url)
        data = response.json()
        assert data["data"]["state"] == "dry"

    def test_farm_state_returns_404_for_other_user(self) -> None:
        pw = secrets.token_urlsafe(16)
        other = get_user_model().objects.create_user(
            username="other", password=pw, email="other@example.com"
        )
        self.client.force_authenticate(user=other)
        response = self.client.get(self.url)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_farm_state_no_observations(self) -> None:
        response = self.client.get(self.url)
        data = response.json()
        assert data["data"]["state"] in (STATE_UNKNOWN, STATE_MOIST)

    def test_farm_state_returns_min_ndwi(self) -> None:
        self._seed_observations(mean=0.3)
        response = self.client.get(self.url)
        data = response.json()
        payload = data["data"]
        assert payload.get("min_ndwi") is not None


class ComputeNdwiFarmStateTests(APITestCase):
    def setUp(self) -> None:
        caches["default"].clear()
        pw = secrets.token_urlsafe(16)
        self.user = get_user_model().objects.create_user(
            username="owner2", password=pw, email="owner2@example.com"
        )
        self.farm = Farm.objects.create(
            owner=self.user,
            name="Farm NDWI 2",
            slug="farm-ndwi-2",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )

    def test_returns_result_for_empty_farm(self) -> None:
        result = compute_ndwi_farm_state(farm=self.farm)
        assert result.farm_id == self.farm.id
        assert result.mean_ndwi is None

    def test_uses_only_ndwi_observations(self) -> None:
        NdviObservation.objects.create(
            farm=self.farm,
            engine="stac",
            bucket_date=date.today(),
            mean=0.8,
            min=0.7,
            max=0.9,
            sample_count=100,
            cloud_fraction=0.05,
            index_type="NDVI",
            state=NdviObservation.ObservationState.FINAL,
        )
        result = compute_ndwi_farm_state(farm=self.farm)
        assert result.mean_ndwi is None


# ── Quality Engine Tests ────────────────────────────────────────


@pytest.mark.django_db
class TestBuildNdwiV2Observation:
    """Tests for ndvi.quality_ndwi.build_ndwi_v2_observation."""

    def _make_observation(
        self,
        farm: Farm,
        mean: float = 0.3,
        cloud_fraction: float = 0.1,
        valid_pixel_fraction: float = 0.8,
        engine: str = "stac",
    ) -> NdviObservation:
        from datetime import datetime as dt

        return NdviObservation.objects.create(
            farm=farm,
            engine=engine,
            bucket_date=date(2025, 1, 1),
            mean=mean,
            min=mean - 0.1,
            max=mean + 0.1,
            sample_count=100,
            cloud_fraction=cloud_fraction,
            valid_pixel_fraction=valid_pixel_fraction,
            index_type="NDWI",
            acquired_at=dt(2025, 1, 1, tzinfo=UTC),
            state=NdviObservation.ObservationState.FINAL,
        )

    def test_happy_path(self) -> None:
        pw = secrets.token_urlsafe(12)
        user = get_user_model().objects.create_user(
            username="ndwi-quality", password=pw, email="q@example.com"
        )
        farm = Farm.objects.create(
            owner=user,
            name="NDWI Quality",
            slug="ndwi-quality",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        v1 = self._make_observation(farm=farm, mean=0.35)
        result = build_ndwi_v2_observation(
            v1,
            prior_v2_values=[0.3, 0.32, 0.34],
        )
        assert result.is_null is False
        assert result.selected_ndvi == 0.35
        assert result.confidence > 0.5
        assert result.confidence <= 1.0

    def test_insufficient_context_caps_confidence(
        self,
    ) -> None:
        pw = secrets.token_urlsafe(12)
        user = get_user_model().objects.create_user(
            username="ndwi-context", password=pw, email="ctx@example.com"
        )
        farm = Farm.objects.create(
            owner=user,
            name="NDWI Context",
            slug="ndwi-context",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        v1 = self._make_observation(farm=farm, mean=0.35)
        result = build_ndwi_v2_observation(v1, prior_v2_values=[])
        max_no_context = 0.49
        assert result.confidence <= max_no_context

    @override_settings(NDWI_MIN_ROLLING_CONTEXT=0)
    def test_with_sufficient_context(self) -> None:
        pw = secrets.token_urlsafe(12)
        user = get_user_model().objects.create_user(
            username="ndwi-suff", password=pw, email="suff@example.com"
        )
        farm = Farm.objects.create(
            owner=user,
            name="NDWI Suff",
            slug="ndwi-suff",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        from datetime import datetime as dt

        NdviObservation.objects.create(
            farm=farm,
            engine="stac",
            bucket_date=date(2025, 1, 2),
            mean=0.3,
            min=0.2,
            max=0.4,
            sample_count=100,
            cloud_fraction=0.05,
            index_type="NDWI",
            acquired_at=dt(2025, 1, 2, tzinfo=UTC),
            state=NdviObservation.ObservationState.FINAL,
        )
        v1 = self._make_observation(farm=farm, mean=0.35)
        result = build_ndwi_v2_observation(v1)
        assert result.is_null is False
        assert result.confidence > 0.0

    def test_null_on_low_valid_pixels(self) -> None:
        pw = secrets.token_urlsafe(12)
        user = get_user_model().objects.create_user(
            username="ndwi-null", password=pw, email="null@example.com"
        )
        farm = Farm.objects.create(
            owner=user,
            name="NDWI Null",
            slug="ndwi-null",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        v1 = self._make_observation(
            farm=farm,
            mean=0.35,
            valid_pixel_fraction=0.05,
        )
        result = build_ndwi_v2_observation(v1)
        assert result.is_null is True
        assert result.null_reason is not None


# ── Fusion Tests ────────────────────────────────────────────────


@pytest.mark.django_db
class TestRunNdwiFusion:
    """Tests for ndvi.fusion_ndwi.run_ndwi_fusion."""

    def _make_observation(
        self,
        farm: Farm,
        mean: float = 0.3,
        engine: str = "stac",
        cloud_fraction: float = 0.1,
        valid_pixel_fraction: float = 0.8,
    ) -> NdviObservation:
        from datetime import datetime as dt

        return NdviObservation.objects.create(
            farm=farm,
            engine=engine,
            bucket_date=date(2025, 1, 1),
            mean=mean,
            min=mean - 0.1,
            max=mean + 0.1,
            sample_count=100,
            cloud_fraction=cloud_fraction,
            valid_pixel_fraction=valid_pixel_fraction,
            index_type="NDWI",
            acquired_at=dt(2025, 1, 1, tzinfo=UTC),
            is_latest=True,
            state=NdviObservation.ObservationState.FINAL,
        )

    def test_no_observations(self) -> None:
        result = run_ndwi_fusion(farm_id=99999, bucket_date=date(2025, 1, 1))
        assert result.selected is None
        assert result.candidates_evaluated == 0
        assert "No NDWI" in result.decision_reason

    def test_all_candidates_discarded(self) -> None:
        pw = secrets.token_urlsafe(12)
        user = get_user_model().objects.create_user(
            username="ndwi-discard", password=pw, email="discard@example.com"
        )
        farm = Farm.objects.create(
            owner=user,
            name="NDWI Discard",
            slug="ndwi-discard",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        self._make_observation(
            farm=farm, mean=0.3, cloud_fraction=0.9, valid_pixel_fraction=0.1
        )
        result = run_ndwi_fusion(farm_id=farm.id, bucket_date=date(2025, 1, 1))
        assert result.selected is None
        assert result.candidates_evaluated >= 1
        assert result.candidates_discarded >= 1

    @patch("ndvi.fusion_ndwi.build_ndwi_v2_observation")
    def test_successful_fusion(
        self,
        mock_build: MagicMock,
        django_db_blocker: object,
    ) -> None:
        pw = secrets.token_urlsafe(12)
        user = get_user_model().objects.create_user(
            username="ndwi-fuse", password=pw, email="fuse@example.com"
        )
        farm = Farm.objects.create(
            owner=user,
            name="NDWI Fuse",
            slug="ndwi-fuse",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        self._make_observation(farm=farm, mean=0.35, engine="sentinelhub")
        mock_build.return_value = V2Result(
            selected_ndvi=0.35,
            smoothed_ndvi=0.35,
            confidence=0.85,
            confidence_components=ConfidenceComponents(
                source_weight=1.0,
                cloud_weight=0.9,
                valid_pixel_weight=0.8,
                recency_weight=1.0,
                temporal_consistency_weight=0.9,
            ),
            quality_flags={},
            is_null=False,
            null_reason=None,
        )
        result = run_ndwi_fusion(farm_id=farm.id, bucket_date=date(2025, 1, 1))
        assert result.selected is not None
        assert result.selected.source == "sentinelhub"
        assert "selected" in result.decision_reason.lower()

    @patch("ndvi.fusion_ndwi.build_ndwi_v2_observation")
    def test_discards_null_candidates(
        self,
        mock_build: MagicMock,
        django_db_blocker: object,
    ) -> None:
        pw = secrets.token_urlsafe(12)
        user = get_user_model().objects.create_user(
            username="ndwi-null-fuse",
            password=pw,
            email="nullfuse@example.com",
        )
        farm = Farm.objects.create(
            owner=user,
            name="NDWI Null Fuse",
            slug="ndwi-null-fuse",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        self._make_observation(farm=farm, mean=0.3)
        mock_build.return_value = V2Result(
            selected_ndvi=None,
            smoothed_ndvi=None,
            confidence=0.0,
            confidence_components=ConfidenceComponents(
                source_weight=0.0,
                cloud_weight=0.0,
                valid_pixel_weight=0.0,
                recency_weight=0.0,
                temporal_consistency_weight=0.0,
            ),
            quality_flags={},
            is_null=True,
            null_reason="low_valid_pixels",
        )
        result = run_ndwi_fusion(farm_id=farm.id, bucket_date=date(2025, 1, 1))
        assert result.selected is None
        assert result.candidates_discarded >= 1
