"""Tests for Phase 1 STAC Hardening."""

from __future__ import annotations

# ruff: noqa: S101
import secrets
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

import numpy as np
import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from farms.models import Farm
from ndvi.engines.base import BBox, NdviPoint
from ndvi.engines.sentinelhub import SentinelHubEngine
from ndvi.engines.stac import StacEngine
from ndvi.models import NdviObservation
from ndvi.services import (
    _determine_observation_state,
    get_valid_pixel_threshold,
    upsert_observations,
)
from ndvi.stac_client import (
    SCL_MASKED_CLASSES,
    SCL_WATER_CLASS,
    apply_scl_mask,
    compute_ndvi_stats,
)


class FakeClient:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def search(
        self,
        *,
        bbox: BBox,
        start: date,
        end: date,
        max_cloud: int,
    ) -> list[Any]:
        return list(self._items)


def _bbox() -> BBox:
    return BBox(
        south=Decimal("0.0"),
        west=Decimal("0.0"),
        north=Decimal("0.1"),
        east=Decimal("0.1"),
    )


# --- apply_scl_mask ---


def test_apply_scl_mask_masks_cloud_classes() -> None:
    ndvi = np.array([[0.5, 0.6, 0.7]], dtype=np.float32)
    scl = np.array([[3, 8, 4]], dtype=np.uint8)
    masked, vpf, flags = apply_scl_mask(ndvi, scl)
    assert np.isnan(masked[0, 0])
    assert np.isnan(masked[0, 1])
    assert not np.isnan(masked[0, 2])
    assert vpf == pytest.approx(1.0 / 3.0, abs=0.01)
    assert flags["cloud_shadow"] is True
    assert flags["low_valid_pixel_fraction"] is False


def test_apply_scl_mask_preserves_clear_pixels() -> None:
    ndvi = np.array([[0.5, 0.6]], dtype=np.float32)
    scl = np.array([[4, 5]], dtype=np.uint8)
    masked, vpf, flags = apply_scl_mask(ndvi, scl)
    assert masked[0, 0] == pytest.approx(0.5)
    assert masked[0, 1] == pytest.approx(0.6)
    assert vpf == pytest.approx(1.0)
    assert flags["low_valid_pixel_fraction"] is False


def test_apply_scl_mask_water_detection() -> None:
    ndvi = np.array([[0.5, 0.6]], dtype=np.float32)
    scl = np.array([[4, 6]], dtype=np.uint8)
    masked, vpf, flags = apply_scl_mask(ndvi, scl, mask_water=True)
    assert np.isnan(masked[0, 1])
    assert flags["water_detected"] is True
    assert vpf == pytest.approx(0.50)
    assert flags["low_valid_pixel_fraction"] is False


def test_apply_scl_mask_water_ignored_when_disabled() -> None:
    ndvi = np.array([[0.5, 0.6]], dtype=np.float32)
    scl = np.array([[4, 6]], dtype=np.uint8)
    masked, vpf, flags = apply_scl_mask(ndvi, scl, mask_water=False)
    assert masked[0, 1] == pytest.approx(0.6)
    assert flags["water_detected"] is False
    assert vpf == pytest.approx(1.0)


def test_apply_scl_mask_handles_all_masked() -> None:
    ndvi = np.array([[0.5, 0.6]], dtype=np.float32)
    scl = np.array([[3, 8]], dtype=np.uint8)
    masked, vpf, flags = apply_scl_mask(ndvi, scl)
    assert np.isnan(masked[0, 0])
    assert np.isnan(masked[0, 1])
    assert vpf == pytest.approx(0.0)
    assert flags["low_valid_pixel_fraction"] is True


def test_apply_scl_mask_no_scl_data() -> None:
    ndvi = np.array([[0.5, 0.6]], dtype=np.float32)
    scl = np.array([[np.nan, np.nan]], dtype=np.float32)
    masked, vpf, flags = apply_scl_mask(ndvi, scl)
    assert flags.get("no_scl_data") is True


def test_apply_scl_mask_saturated_pixel_detection() -> None:
    ndvi = np.array([[0.5, 0.6]], dtype=np.float32)
    scl = np.array([[1, 4]], dtype=np.uint8)
    _, _, flags = apply_scl_mask(ndvi, scl)
    assert flags["saturated_pixels"] is True


def test_apply_scl_mask_partial_tile_flag() -> None:
    ndvi = np.array([[0.5, 0.6, 0.7, 0.8, 0.9, 0.3]], dtype=np.float32)
    scl = np.array([[4, 4, 4, 3, 8, 9]], dtype=np.uint8)
    _, vpf, flags = apply_scl_mask(ndvi, scl)
    assert vpf == pytest.approx(3.0 / 6.0, abs=0.01)
    assert flags["partial_tile"] is True


# --- compute_ndvi_stats with valid_pixel_fraction ---


def test_compute_ndvi_stats_with_vpf() -> None:
    ndvi = np.array([[0.5, np.nan, 0.7]], dtype=np.float32)
    stats = compute_ndvi_stats(ndvi)
    assert stats is not None
    assert stats.sample_count == 2
    assert stats.mean == pytest.approx(0.6, abs=0.01)


# --- get_valid_pixel_threshold ---


def test_get_valid_pixel_threshold_default() -> None:
    assert get_valid_pixel_threshold() == 0.30


@override_settings(NDVI_VALID_PIXEL_THRESHOLD=0.50)
def test_get_valid_pixel_threshold_from_settings() -> None:
    assert get_valid_pixel_threshold() == 0.50


# --- _determine_observation_state with valid_pixel_fraction ---


def test_determine_state_rejected_when_vpf_below_threshold() -> None:
    state = _determine_observation_state(
        0.10,
        max_cloud=30,
        valid_pixel_fraction=0.20,
    )
    assert state == NdviObservation.ObservationState.REJECTED


def test_determine_state_final_when_vpf_above_threshold() -> None:
    state = _determine_observation_state(
        0.10,
        max_cloud=30,
        valid_pixel_fraction=0.50,
    )
    assert state == NdviObservation.ObservationState.FINAL


def test_determine_state_raw_when_cloud_none_despite_good_vpf() -> None:
    state = _determine_observation_state(
        None,
        max_cloud=30,
        valid_pixel_fraction=0.80,
    )
    assert state == NdviObservation.ObservationState.RAW


def test_determine_state_rejected_when_vpf_none_uses_cloud_only() -> None:
    state = _determine_observation_state(
        0.10,
        max_cloud=30,
        valid_pixel_fraction=None,
    )
    assert state == NdviObservation.ObservationState.FINAL


# --- upsert_observations rejects low valid_pixel_fraction ---


@pytest.mark.django_db
def test_upsert_skips_low_valid_pixel_fraction(
    settings: Any,
) -> None:
    settings.NDVI_ENGINE = "sentinelhub"
    user = get_user_model().objects.create_user(
        username="vpf-skip",
        email="vpf-skip@example.com",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(owner=user, name="Farm", slug="farm-vpf-skip")
    points = [
        NdviPoint(
            date=date(2025, 3, 1),
            mean=0.5,
            min=0.3,
            max=0.7,
            sample_count=100,
            cloud_fraction=0.10,
            valid_pixel_fraction=0.20,
        ),
    ]
    saved = upsert_observations(
        farm=farm,
        engine="sentinelhub",
        max_cloud=30,
        points=points,
    )
    assert len(saved) == 0


@pytest.mark.django_db
def test_upsert_accepts_good_valid_pixel_fraction(
    settings: Any,
) -> None:
    settings.NDVI_ENGINE = "sentinelhub"
    user = get_user_model().objects.create_user(
        username="vpf-ok",
        email="vpf-ok@example.com",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(owner=user, name="Farm", slug="farm-vpf-ok")
    points = [
        NdviPoint(
            date=date(2025, 3, 1),
            mean=0.5,
            min=0.3,
            max=0.7,
            sample_count=100,
            cloud_fraction=0.10,
            valid_pixel_fraction=0.80,
            quality_flags={"cloud_heavy": False},
        ),
    ]
    saved = upsert_observations(
        farm=farm,
        engine="sentinelhub",
        max_cloud=30,
        points=points,
    )
    assert len(saved) == 1
    obs = saved[0]
    assert obs.valid_pixel_fraction == pytest.approx(0.80)
    assert obs.quality_flags == {"cloud_heavy": False}
    assert obs.state == NdviObservation.ObservationState.FINAL


@pytest.mark.django_db
def test_upsert_persists_quality_flags(
    settings: Any,
) -> None:
    settings.NDVI_ENGINE = "sentinelhub"
    user = get_user_model().objects.create_user(
        username="qf-persist",
        email="qf-persist@example.com",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(
        owner=user,
        name="Farm",
        slug="farm-qf-persist",
    )
    flags = {
        "cloud_heavy": False,
        "partial_tile": True,
        "water_detected": False,
    }
    points = [
        NdviPoint(
            date=date(2025, 3, 1),
            mean=0.5,
            cloud_fraction=0.10,
            valid_pixel_fraction=0.50,
            quality_flags=flags,
        ),
    ]
    saved = upsert_observations(
        farm=farm,
        engine="sentinelhub",
        max_cloud=30,
        points=points,
    )
    assert len(saved) == 1
    assert saved[0].quality_flags == flags
    assert saved[0].state == NdviObservation.ObservationState.FINAL


# --- STAC engine propagates valid_pixel_fraction ---


def test_stac_engine_propagates_vpf_and_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ndvi.stac_client import NdviStats, StacItem

    items = [
        StacItem(
            id="item-1",
            datetime=datetime(2025, 1, 2, tzinfo=UTC),
            assets={"B04": "red.tif", "B08": "nir.tif", "SCL": "scl.tif"},
            cloud_cover=5.0,
        ),
    ]
    engine = StacEngine(
        client=cast(Any, FakeClient(items)),
        date_window_days=2,
    )

    def fake_compute_stats(
        item: StacItem,
        bbox: BBox,
    ) -> NdviStats:
        return NdviStats(
            mean=0.5,
            min=0.3,
            max=0.7,
            sample_count=100,
            valid_pixel_fraction=0.75,
            quality_flags={"cloud_heavy": False, "partial_tile": False},
        )

    monkeypatch.setattr(engine, "_compute_stats", fake_compute_stats)
    points = engine.get_timeseries(
        bbox=_bbox(),
        start=date(2025, 1, 1),
        end=date(2025, 1, 3),
        step_days=2,
        max_cloud=20,
    )
    assert len(points) == 2
    assert points[0].valid_pixel_fraction == pytest.approx(0.75)
    assert points[0].quality_flags == {
        "cloud_heavy": False,
        "partial_tile": False,
    }


def test_stac_engine_latest_propagates_vpf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ndvi.stac_client import NdviStats, StacItem

    today = date.today()
    dt = datetime(today.year, today.month, today.day, tzinfo=UTC)
    items = [
        StacItem(
            id="item-1",
            datetime=dt,
            assets={"B04": "red.tif", "B08": "nir.tif"},
            cloud_cover=5.0,
        ),
    ]
    engine = StacEngine(
        client=cast(Any, FakeClient(items)),
    )

    def fake_compute_stats(
        item: StacItem,
        bbox: BBox,
    ) -> NdviStats:
        return NdviStats(
            mean=0.5,
            min=0.3,
            max=0.7,
            sample_count=100,
            valid_pixel_fraction=0.60,
            quality_flags={"cloud_heavy": False},
        )

    monkeypatch.setattr(engine, "_compute_stats", fake_compute_stats)
    point = engine.get_latest(
        bbox=_bbox(),
        lookback_days=7,
        max_cloud=20,
    )
    assert point is not None
    assert point.valid_pixel_fraction == pytest.approx(0.60)
    assert point.quality_flags == {"cloud_heavy": False}


# --- SentinelHub engine quality flags ---


def test_sentinelhub_build_quality_flags() -> None:
    engine = SentinelHubEngine.__new__(SentinelHubEngine)
    flags = engine._build_quality_flags(0.80, 0.10)
    assert flags["cloud_heavy"] is False
    assert flags["partial_tile"] is False
    assert flags["low_valid_pixel_fraction"] is False


def test_sentinelhub_build_quality_flags_cloudy() -> None:
    engine = SentinelHubEngine.__new__(SentinelHubEngine)
    flags = engine._build_quality_flags(0.50, 0.40)
    assert flags["cloud_heavy"] is True
    assert flags["partial_tile"] is True
    assert flags["low_valid_pixel_fraction"] is False


def test_sentinelhub_build_quality_flags_low_vpf() -> None:
    engine = SentinelHubEngine.__new__(SentinelHubEngine)
    flags = engine._build_quality_flags(0.20, 0.10)
    assert flags["cloud_heavy"] is False
    assert flags["partial_tile"] is False
    assert flags["low_valid_pixel_fraction"] is True


def test_sentinelhub_build_quality_flags_partial_tile() -> None:
    engine = SentinelHubEngine.__new__(SentinelHubEngine)
    flags = engine._build_quality_flags(0.50, 0.10)
    assert flags["partial_tile"] is True
    assert flags["low_valid_pixel_fraction"] is False


def test_sentinelhub_build_quality_flags_missing_vpf() -> None:
    engine = SentinelHubEngine.__new__(SentinelHubEngine)
    flags = engine._build_quality_flags(None, 0.10)
    assert flags["partial_tile"] is False
    assert flags["low_valid_pixel_fraction"] is False


def test_sentinelhub_compute_valid_pixel_fraction() -> None:
    engine = SentinelHubEngine.__new__(SentinelHubEngine)
    vpf = engine._compute_valid_pixel_fraction(
        {"mean": 0.75, "sampleCount": 1000}
    )
    assert vpf == pytest.approx(0.75)


def test_sentinelhub_compute_valid_pixel_fraction_missing() -> None:
    engine = SentinelHubEngine.__new__(SentinelHubEngine)
    assert engine._compute_valid_pixel_fraction({}) is None
    assert engine._compute_valid_pixel_fraction({"mean": None}) is None


# --- SCL constants ---


def test_scl_masked_classes_contains_expected_values() -> None:
    assert 0 in SCL_MASKED_CLASSES
    assert 1 in SCL_MASKED_CLASSES
    assert 3 in SCL_MASKED_CLASSES
    assert 8 in SCL_MASKED_CLASSES
    assert 9 in SCL_MASKED_CLASSES
    assert 10 in SCL_MASKED_CLASSES
    assert 11 in SCL_MASKED_CLASSES
    assert 4 not in SCL_MASKED_CLASSES
    assert 5 not in SCL_MASKED_CLASSES


def test_scl_water_class() -> None:
    assert SCL_WATER_CLASS == 6
