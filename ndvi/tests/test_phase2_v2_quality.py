"""Tests for Phase 2 V2 Quality Engine."""

from __future__ import annotations

# ruff: noqa: S101
import secrets
from datetime import UTC, date, datetime
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from farms.models import Farm
from ndvi.models import NdviDerivedObservation, NdviObservation
from ndvi.v2_quality import (
    V2Result,
    _check_null_conditions,
    _check_outlier,
    _clamp,
    _compute_cloud_weight,
    _compute_recency_weight,
    _compute_smoothed,
    _compute_temporal_consistency_weight,
    _compute_valid_pixel_weight,
    _get_source_weight,
    _median,
    build_v2_observation,
    get_prior_v2_values,
    persist_v2_observation,
    process_v1_to_v2,
)


def _make_v1(
    *,
    farm_id: int = 1,
    engine: str = "sentinelhub",
    bucket_date: date | None = None,
    mean: float = 0.5,
    cloud_fraction: float | None = 0.10,
    valid_pixel_fraction: float | None = 0.80,
    acquired_at: datetime | None = None,
    quality_flags: dict[str, Any] | None = None,
) -> NdviObservation:
    if bucket_date is None:
        bucket_date = date(2025, 3, 15)
    if acquired_at is None:
        acquired_at = datetime(
            bucket_date.year,
            bucket_date.month,
            bucket_date.day,
            10,
            0,
            tzinfo=UTC,
        )
    return NdviObservation(
        farm_id=farm_id,
        engine=engine,
        bucket_date=bucket_date,
        mean=mean,
        cloud_fraction=cloud_fraction,
        valid_pixel_fraction=valid_pixel_fraction,
        acquired_at=acquired_at,
        quality_flags=quality_flags or {},
    )


# --- Utility functions ---


def test_clamp_bounds() -> None:
    assert _clamp(0.5) == 0.5
    assert _clamp(-0.1) == 0.0
    assert _clamp(1.5) == 1.0
    assert _clamp(0.0) == 0.0
    assert _clamp(1.0) == 1.0


def test_median_odd() -> None:
    assert _median([1.0, 3.0, 2.0]) == 2.0


def test_median_even() -> None:
    assert _median([1.0, 2.0, 3.0, 4.0]) == 2.5


def test_median_empty() -> None:
    assert _median([]) is None


def test_median_single() -> None:
    assert _median([5.0]) == 5.0


# --- Confidence component weights ---


def test_source_weight_sentinel2() -> None:
    assert _get_source_weight("sentinel-2") == 1.00
    assert _get_source_weight("sentinelhub") == 1.00
    assert _get_source_weight("stac") == 1.00


def test_source_weight_landsat() -> None:
    assert _get_source_weight("landsat") == 0.80


def test_source_weight_modis() -> None:
    assert _get_source_weight("modis") == 0.60


def test_source_weight_unknown() -> None:
    assert _get_source_weight("unknown") == 0.60


def test_cloud_weight_none() -> None:
    assert _compute_cloud_weight(None) == 0.5


def test_cloud_weight_zero() -> None:
    assert _compute_cloud_weight(0.0) == 1.0


def test_cloud_weight_full() -> None:
    assert _compute_cloud_weight(1.0) == 0.0


def test_cloud_weight_percent() -> None:
    assert _compute_cloud_weight(0.30) == pytest.approx(0.70)


def test_valid_pixel_weight_none() -> None:
    assert _compute_valid_pixel_weight(None) == 0.5


def test_valid_pixel_weight_full() -> None:
    assert _compute_valid_pixel_weight(1.0) == 1.0


def test_valid_pixel_weight_zero() -> None:
    assert _compute_valid_pixel_weight(0.0) == 0.0


def test_recency_weight_same_day() -> None:
    d = date(2025, 3, 15)
    dt = datetime(d.year, d.month, d.day, 10, 0, tzinfo=UTC)
    assert _compute_recency_weight(dt, d) == 1.0


def test_recency_weight_old() -> None:
    d = date(2025, 3, 15)
    old = datetime(2025, 2, 1, 10, 0, tzinfo=UTC)
    weight = _compute_recency_weight(old, d)
    assert 0.0 <= weight < 1.0


def test_recency_weight_none_acquisition() -> None:
    d = date(2025, 3, 15)
    assert _compute_recency_weight(None, d) == 1.0


def test_temporal_consistency_exact_match() -> None:
    assert _compute_temporal_consistency_weight(0.5, 0.5) == 1.0


def test_temporal_consistency_no_median() -> None:
    assert _compute_temporal_consistency_weight(0.5, None) == 0.5


def test_temporal_consistency_large_deviation() -> None:
    weight = _compute_temporal_consistency_weight(0.9, 0.5)
    assert weight < 0.5


# --- Outlier detection ---


def test_outlier_no_median() -> None:
    assert _check_outlier(0.5, None, 0.70, 0.80) is False


def test_outlier_within_threshold() -> None:
    assert _check_outlier(0.50, 0.45, 0.70, 0.80) is False


def test_outlier_all_conditions_met() -> None:
    assert _check_outlier(0.80, 0.50, 0.60, 0.50) is True


def test_outlier_high_confidence_not_outlier() -> None:
    assert _check_outlier(0.80, 0.50, 0.80, 0.50) is False


def test_outlier_good_valid_pixels_not_outlier() -> None:
    assert _check_outlier(0.80, 0.50, 0.60, 0.80) is False


def test_outlier_none_vpf_not_outlier() -> None:
    assert _check_outlier(0.80, 0.50, 0.60, None) is False


# --- Null conditions ---


def test_null_low_valid_pixel() -> None:
    is_null, reason = _check_null_conditions(
        valid_pixel_fraction=0.20,
        confidence=0.80,
        raw_ndvi=0.5,
        acquisition_at=datetime.now(tz=UTC),
        engine="sentinelhub",
        prior_v2_count=5,
        is_outlier=False,
    )
    assert is_null is True
    assert reason == "low_valid_pixel_fraction"


def test_null_low_confidence() -> None:
    is_null, reason = _check_null_conditions(
        valid_pixel_fraction=0.80,
        confidence=0.30,
        raw_ndvi=0.5,
        acquisition_at=datetime.now(tz=UTC),
        engine="sentinelhub",
        prior_v2_count=5,
        is_outlier=False,
    )
    assert is_null is True
    assert reason == "low_confidence"


def test_null_missing_ndvi() -> None:
    is_null, reason = _check_null_conditions(
        valid_pixel_fraction=0.80,
        confidence=0.80,
        raw_ndvi=None,
        acquisition_at=datetime.now(tz=UTC),
        engine="sentinelhub",
        prior_v2_count=5,
        is_outlier=False,
    )
    assert is_null is True
    assert reason == "missing_ndvi_value"


def test_null_missing_acquisition() -> None:
    is_null, reason = _check_null_conditions(
        valid_pixel_fraction=0.80,
        confidence=0.80,
        raw_ndvi=0.5,
        acquisition_at=None,
        engine="sentinelhub",
        prior_v2_count=5,
        is_outlier=False,
    )
    assert is_null is True
    assert reason == "missing_acquisition_time"


def test_null_insufficient_context_non_sentinel() -> None:
    is_null, reason = _check_null_conditions(
        valid_pixel_fraction=0.80,
        confidence=0.80,
        raw_ndvi=0.5,
        acquisition_at=datetime.now(tz=UTC),
        engine="landsat",
        prior_v2_count=1,
        is_outlier=False,
    )
    assert is_null is True
    assert reason == "insufficient_rolling_context"


def test_null_outlier_rejected() -> None:
    is_null, reason = _check_null_conditions(
        valid_pixel_fraction=0.80,
        confidence=0.80,
        raw_ndvi=0.5,
        acquisition_at=datetime.now(tz=UTC),
        engine="sentinelhub",
        prior_v2_count=5,
        is_outlier=True,
    )
    assert is_null is True
    assert reason == "outlier_rejected"


def test_not_null_all_good() -> None:
    is_null, reason = _check_null_conditions(
        valid_pixel_fraction=0.80,
        confidence=0.80,
        raw_ndvi=0.5,
        acquisition_at=datetime.now(tz=UTC),
        engine="sentinelhub",
        prior_v2_count=5,
        is_outlier=False,
    )
    assert is_null is False
    assert reason is None


def test_sentinel2_no_context_required() -> None:
    is_null, reason = _check_null_conditions(
        valid_pixel_fraction=0.80,
        confidence=0.80,
        raw_ndvi=0.5,
        acquisition_at=datetime.now(tz=UTC),
        engine="sentinel-2",
        prior_v2_count=0,
        is_outlier=False,
    )
    assert is_null is False


# --- Smoothing ---


def test_smoothed_enough_values() -> None:
    result = _compute_smoothed(0.5, [0.4, 0.6])
    assert result is not None
    assert result == pytest.approx(0.5)


def test_smoothed_not_enough_values() -> None:
    result = _compute_smoothed(0.5, [0.4])
    assert result is None


def test_smoothed_no_prior() -> None:
    result = _compute_smoothed(0.5, [])
    assert result is None


# --- build_v2_observation ---


def test_build_v2_high_quality() -> None:
    v1 = _make_v1(
        mean=0.5,
        cloud_fraction=0.10,
        valid_pixel_fraction=0.90,
    )
    result = build_v2_observation(
        v1,
        prior_v2_values=[0.48, 0.50, 0.52, 0.49, 0.51],
    )
    assert result.is_null is False
    assert result.selected_ndvi == 0.5
    assert result.confidence > 0.75
    assert result.smoothed_ndvi is not None
    assert result.null_reason is None


def test_build_v2_low_valid_pixel() -> None:
    v1 = _make_v1(
        valid_pixel_fraction=0.20,
    )
    result = build_v2_observation(v1, prior_v2_values=[0.5] * 5)
    assert result.is_null is True
    assert result.null_reason == "low_valid_pixel_fraction"
    assert result.selected_ndvi is None


def test_build_v2_low_confidence() -> None:
    v1 = _make_v1(
        cloud_fraction=0.95,
        valid_pixel_fraction=0.20,
    )
    result = build_v2_observation(v1, prior_v2_values=[0.5] * 5)
    assert result.is_null is True
    assert result.null_reason in (
        "low_confidence",
        "low_valid_pixel_fraction",
    )


def test_build_v2_outlier() -> None:
    v1 = _make_v1(
        mean=0.90,
        cloud_fraction=0.40,
        valid_pixel_fraction=0.50,
    )
    result = build_v2_observation(
        v1,
        prior_v2_values=[0.50, 0.50, 0.50, 0.50, 0.50],
    )
    assert result.is_null is True
    assert result.null_reason == "outlier_rejected"


def test_build_v2_confidence_capped_without_context() -> None:
    v1 = _make_v1(
        engine="landsat",
        mean=0.5,
        cloud_fraction=0.10,
        valid_pixel_fraction=0.90,
    )
    result = build_v2_observation(v1, prior_v2_values=[])
    assert result.confidence <= 0.49


def test_build_v2_quality_flags() -> None:
    v1 = _make_v1(
        cloud_fraction=0.10,
        valid_pixel_fraction=0.90,
        quality_flags={"cloud_heavy": False},
    )
    result = build_v2_observation(
        v1,
        prior_v2_values=[0.48, 0.50, 0.52, 0.49, 0.51],
    )
    assert result.quality_flags["low_confidence"] is False
    assert result.quality_flags["outlier_removed"] is False


def test_build_v2_smoothed_with_prior() -> None:
    v1 = _make_v1(mean=0.50)
    result = build_v2_observation(
        v1,
        prior_v2_values=[0.40, 0.45, 0.55, 0.60, 0.50],
    )
    assert result.smoothed_ndvi is not None


def test_build_v2_no_smoothing_without_enough_prior() -> None:
    v1 = _make_v1(mean=0.50)
    result = build_v2_observation(
        v1,
        prior_v2_values=[0.40],
    )
    assert result.smoothed_ndvi is None


# --- get_prior_v2_values ---


@pytest.mark.django_db
def test_get_prior_v2_values_returns_values(
    settings: Any,
) -> None:
    settings.NDVI_ENGINE = "sentinelhub"
    user = get_user_model().objects.create_user(
        username="v2-prior",
        email="v2-prior@example.com",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(
        owner=user,
        name="Farm",
        slug="farm-v2-prior",
    )
    for i in range(1, 8):
        v1 = NdviObservation.objects.create(
            farm=farm,
            engine="sentinelhub",
            bucket_date=date(2025, 3, i),
            mean=0.40 + i * 0.05,
            cloud_fraction=0.10,
            valid_pixel_fraction=0.90,
            state="FINAL",
            acquired_at=datetime(2025, 3, i, 10, 0, tzinfo=UTC),
        )
        NdviDerivedObservation.objects.create(
            farm=farm,
            v1_observation=v1,
            engine="sentinelhub",
            bucket_date=date(2025, 3, i),
            source="sentinelhub",
            selected_ndvi=0.40 + i * 0.05,
            confidence=0.85,
            is_null=False,
        )

    NdviObservation.objects.create(
        farm=farm,
        engine="sentinelhub",
        bucket_date=date(2025, 3, 15),
        mean=0.5,
        cloud_fraction=0.10,
        valid_pixel_fraction=0.90,
        state="FINAL",
        acquired_at=datetime(2025, 3, 15, 10, 0, tzinfo=UTC),
    )

    prior = get_prior_v2_values(
        farm.id,
        "sentinelhub",
        date(2025, 3, 15),
    )
    assert len(prior) == 5
    assert prior == sorted(prior)


@pytest.mark.django_db
def test_get_prior_v2_values_excludes_nulls(
    settings: Any,
) -> None:
    settings.NDVI_ENGINE = "sentinelhub"
    user = get_user_model().objects.create_user(
        username="v2-nulls",
        email="v2-nulls@example.com",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(
        owner=user,
        name="Farm",
        slug="farm-v2-nulls",
    )
    for i in range(1, 4):
        v1 = NdviObservation.objects.create(
            farm=farm,
            engine="sentinelhub",
            bucket_date=date(2025, 3, i),
            mean=0.5,
            cloud_fraction=0.10,
            valid_pixel_fraction=0.90,
            state="FINAL",
            acquired_at=datetime(2025, 3, i, 10, 0, tzinfo=UTC),
        )
        NdviDerivedObservation.objects.create(
            farm=farm,
            v1_observation=v1,
            engine="sentinelhub",
            bucket_date=date(2025, 3, i),
            source="sentinelhub",
            selected_ndvi=None,
            confidence=0.30,
            is_null=True,
        )

    prior = get_prior_v2_values(
        farm.id,
        "sentinelhub",
        date(2025, 3, 15),
    )
    assert len(prior) == 0


# --- persist_v2_observation ---


@pytest.mark.django_db
def test_persist_v2_creates_record(
    settings: Any,
) -> None:
    settings.NDVI_ENGINE = "sentinelhub"
    user = get_user_model().objects.create_user(
        username="v2-persist",
        email="v2-persist@example.com",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(
        owner=user,
        name="Farm",
        slug="farm-v2-persist",
    )
    v1 = NdviObservation.objects.create(
        farm=farm,
        engine="sentinelhub",
        bucket_date=date(2025, 3, 15),
        mean=0.5,
        cloud_fraction=0.10,
        valid_pixel_fraction=0.90,
        state="FINAL",
    )

    result = V2Result(
        selected_ndvi=0.5,
        smoothed_ndvi=0.5,
        confidence=0.85,
        confidence_components=__import__(
            "ndvi.v2_quality",
            fromlist=["ConfidenceComponents"],
        ).ConfidenceComponents(
            source_weight=1.0,
            cloud_weight=0.9,
            valid_pixel_weight=0.9,
            recency_weight=1.0,
            temporal_consistency_weight=1.0,
        ),
        quality_flags={"cloud_heavy": False},
        is_null=False,
        null_reason=None,
    )

    persisted = persist_v2_observation(v1, result)
    assert persisted.v1_observation_id == v1.id
    assert persisted.confidence == 0.85
    assert persisted.selected_ndvi == 0.5
    assert persisted.is_null is False


@pytest.mark.django_db
def test_persist_v2_idempotent(
    settings: Any,
) -> None:
    settings.NDVI_ENGINE = "sentinelhub"
    user = get_user_model().objects.create_user(
        username="v2-idem",
        email="v2-idem@example.com",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(
        owner=user,
        name="Farm",
        slug="farm-v2-idem",
    )
    v1 = NdviObservation.objects.create(
        farm=farm,
        engine="sentinelhub",
        bucket_date=date(2025, 3, 15),
        mean=0.5,
        cloud_fraction=0.10,
        valid_pixel_fraction=0.90,
        state="FINAL",
    )

    cc_mod = __import__(
        "ndvi.v2_quality",
        fromlist=["ConfidenceComponents"],
    )

    result1 = V2Result(
        selected_ndvi=0.5,
        smoothed_ndvi=0.5,
        confidence=0.85,
        confidence_components=cc_mod.ConfidenceComponents(
            source_weight=1.0,
            cloud_weight=0.9,
            valid_pixel_weight=0.9,
            recency_weight=1.0,
            temporal_consistency_weight=1.0,
        ),
        quality_flags={},
        is_null=False,
        null_reason=None,
    )

    first = persist_v2_observation(v1, result1)

    result2 = V2Result(
        selected_ndvi=0.6,
        smoothed_ndvi=0.55,
        confidence=0.90,
        confidence_components=cc_mod.ConfidenceComponents(
            source_weight=1.0,
            cloud_weight=0.95,
            valid_pixel_weight=0.95,
            recency_weight=1.0,
            temporal_consistency_weight=1.0,
        ),
        quality_flags={},
        is_null=False,
        null_reason=None,
    )

    second = persist_v2_observation(v1, result2)
    assert first.id == second.id
    assert (
        NdviDerivedObservation.objects.filter(v1_observation=v1).count() == 1
    )
    second.refresh_from_db()
    assert second.confidence == 0.90


# --- process_v1_to_v2 ---


@pytest.mark.django_db
def test_process_v1_to_v2_full_pipeline(
    settings: Any,
) -> None:
    settings.NDVI_ENGINE = "sentinelhub"
    user = get_user_model().objects.create_user(
        username="v2-pipeline",
        email="v2-pipeline@example.com",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(
        owner=user,
        name="Farm",
        slug="farm-v2-pipeline",
    )

    for i in range(1, 6):
        v1 = NdviObservation.objects.create(
            farm=farm,
            engine="sentinelhub",
            bucket_date=date(2025, 3, i),
            mean=0.45 + i * 0.02,
            cloud_fraction=0.10,
            valid_pixel_fraction=0.90,
            state="FINAL",
        )
        NdviDerivedObservation.objects.create(
            farm=farm,
            v1_observation=v1,
            engine="sentinelhub",
            bucket_date=date(2025, 3, i),
            source="sentinelhub",
            selected_ndvi=0.45 + i * 0.02,
            confidence=0.85,
            is_null=False,
        )

    v1 = NdviObservation.objects.create(
        farm=farm,
        engine="sentinelhub",
        bucket_date=date(2025, 3, 15),
        mean=0.5,
        cloud_fraction=0.10,
        valid_pixel_fraction=0.90,
        state="FINAL",
        acquired_at=datetime(2025, 3, 15, 10, 0, tzinfo=UTC),
    )

    result, persisted = process_v1_to_v2(v1, persist=True)
    assert result.is_null is False
    assert persisted is not None
    assert persisted.v1_observation_id == v1.id


@pytest.mark.django_db
def test_process_v1_to_v2_no_persist(
    settings: Any,
) -> None:
    settings.NDVI_ENGINE = "sentinelhub"
    user = get_user_model().objects.create_user(
        username="v2-no-persist",
        email="v2-no-persist@example.com",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(
        owner=user,
        name="Farm",
        slug="farm-v2-no-persist",
    )
    v1 = NdviObservation(
        farm=farm,
        engine="sentinelhub",
        bucket_date=date(2025, 3, 15),
        mean=0.5,
        cloud_fraction=0.10,
        valid_pixel_fraction=0.90,
    )

    result, persisted = process_v1_to_v2(v1, persist=False)
    assert result is not None
    assert persisted is None


# --- Settings overrides ---


@override_settings(NDVI_V2_ROLLING_WINDOW=3)
def test_rolling_window_from_settings() -> None:
    from ndvi.v2_quality import get_rolling_window_size

    assert get_rolling_window_size() == 3


@override_settings(NDVI_V2_OUTLIER_THRESHOLD=0.10)
def test_outlier_threshold_from_settings() -> None:
    from ndvi.v2_quality import get_outlier_threshold

    assert get_outlier_threshold() == 0.10


@override_settings(NDVI_V2_LOW_CONFIDENCE_THRESHOLD=0.60)
def test_low_confidence_threshold_from_settings() -> None:
    from ndvi.v2_quality import get_low_confidence_threshold

    assert get_low_confidence_threshold() == 0.60


@override_settings(NDVI_V2_MIN_SMOOTH_VALUES=5)
def test_min_smooth_values_from_settings() -> None:
    from ndvi.v2_quality import get_min_smooth_values

    assert get_min_smooth_values() == 5
