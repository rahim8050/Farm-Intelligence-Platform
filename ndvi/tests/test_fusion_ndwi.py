"""Tests for ndvi.fusion_ndwi — NDWI fusion and water classification."""

from __future__ import annotations

import secrets
from datetime import UTC, date, datetime
from typing import Any

import pytest
from django.test import override_settings

from farms.models import Farm
from ndvi.fusion_ndwi import classify_ndwi, run_ndwi_fusion
from ndvi.models import NdviObservation

PASSWORD = secrets.token_urlsafe(12)


class TestClassifyNdwi:
    """Unit tests for classify_ndwi()."""

    def test_open_water(self) -> None:
        assert classify_ndwi(0.50) == "open_water"

    def test_wet_soil(self) -> None:
        assert classify_ndwi(0.10) == "wet_soil"

    def test_dry_soil(self) -> None:
        assert classify_ndwi(-0.10) == "dry_soil"

    def test_vegetation_dominated(self) -> None:
        assert classify_ndwi(-0.50) == "vegetation_dominated"

    def test_boundary_wet_soil_at_zero(self) -> None:
        assert classify_ndwi(0.00) == "wet_soil"

    def test_boundary_dry_soil_at_neg_030(self) -> None:
        assert classify_ndwi(-0.30) == "dry_soil"


class TestClassifyNdwiCustomThresholds:
    """Verify that settings overrides change classification."""

    @override_settings(NDWI_WATER_THRESHOLD=0.50)
    def test_custom_water_threshold(self) -> None:
        assert classify_ndwi(0.30) == "wet_soil"
        assert classify_ndwi(0.50) == "open_water"

    @override_settings(NDWI_WET_SOIL_THRESHOLD=-0.10)
    def test_custom_wet_soil_threshold(self) -> None:
        assert classify_ndwi(-0.05) == "wet_soil"
        assert classify_ndwi(-0.10) == "wet_soil"

    @override_settings(NDWI_DRY_SOIL_THRESHOLD=-0.50)
    def test_custom_dry_soil_threshold(self) -> None:
        assert classify_ndwi(-0.40) == "dry_soil"
        assert classify_ndwi(-0.50) == "dry_soil"
        assert classify_ndwi(-0.60) == "vegetation_dominated"


class TestRunNdwiFusionWaterClass:
    """Integration: run_ndwi_fusion sets ndwi_water_class on FusionResult."""

    @pytest.mark.django_db
    @override_settings(
        NDWI_MIN_ROLLING_CONTEXT=0,
        NDWI_MAX_CONFIDENCE_WITHOUT_CONTEXT=0.80,
    )
    def test_water_class_set_on_fusion_result(
        self,
        django_user_model: Any,
    ) -> None:
        user = django_user_model.objects.create_user(
            username="ndwi-fusion-water",
            email="ndwi-fusion-water@example.com",
            password=PASSWORD,
        )
        farm = Farm.objects.create(
            owner=user,
            name="NDWI Fusion Water",
            slug="ndwi-fusion-water",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
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
            index_type="NDWI",
        )
        result = run_ndwi_fusion(
            farm_id=farm.id,
            bucket_date=date(2025, 1, 1),
        )
        assert result.selected is not None
        assert result.ndwi_water_class == "wet_soil"

    @pytest.mark.django_db
    @override_settings(
        NDWI_MIN_ROLLING_CONTEXT=0,
        NDWI_MAX_CONFIDENCE_WITHOUT_CONTEXT=0.80,
    )
    def test_water_class_none_when_no_candidates(
        self,
        django_user_model: Any,
    ) -> None:
        user = django_user_model.objects.create_user(
            username="ndwi-fusion-none",
            email="ndwi-fusion-none@example.com",
            password=PASSWORD,
        )
        farm = Farm.objects.create(
            owner=user,
            name="NDWI Fusion None",
            slug="ndwi-fusion-none",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        result = run_ndwi_fusion(
            farm_id=farm.id,
            bucket_date=date(2025, 1, 1),
        )
        assert result.selected is None
        assert result.ndwi_water_class is None
