from __future__ import annotations

import secrets
from datetime import date, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import caches

from farms.models import Farm
from ndvi.farm_state import (
    _compute_max_ndvi,
    _compute_mean_ndvi,
    _coverage_lock_key,
    build_farm_state,
    get_coverage_cache_ttl_seconds,
    get_coverage_lock_seconds,
    get_coverage_threshold,
    get_trend_window_days,
)
from ndvi.models import NdviObservation


@pytest.mark.django_db
def test_farm_state_uses_cached_coverage(settings: Any) -> None:
    caches["default"].clear()
    settings.NDVI_ENGINE = "stac"
    user = get_user_model().objects.create_user(
        username="owner",
        email="owner@example.com",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(
        owner=user,
        name="Farm",
        slug="farm",
        bbox_south=0.0,
        bbox_west=0.0,
        bbox_north=0.2,
        bbox_east=0.2,
        is_active=True,
    )
    target_date = date.today() - timedelta(days=1)
    NdviObservation.objects.create(
        farm=farm,
        engine="stac",
        bucket_date=target_date,
        mean=0.3,
    )
    threshold = get_coverage_threshold()
    cache_key = (
        f"farm_state:coverage:{farm.id}:stac:{target_date}:{threshold:.3f}"
    )
    caches["default"].set(cache_key, {"value": 42.0}, timeout=600)

    with patch(
        "ndvi.farm_state._enqueue_coverage_compute", new=MagicMock()
    ) as mock_enqueue:
        result = build_farm_state(farm=farm, engine="stac")
        assert result.coverage_pct == 42.0
        mock_enqueue.assert_not_called()


@pytest.mark.django_db
def test_farm_state_returns_none_on_coverage_cache_miss(
    settings: Any,
) -> None:
    """When coverage cache misses, coverage_pct is None (no task dispatch).

    Coverage is pre-computed daily by Celery Beat, not lazily on GET.
    """
    caches["default"].clear()
    settings.NDVI_ENGINE = "stac"
    user = get_user_model().objects.create_user(
        username="owner2",
        email="owner2@example.com",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(
        owner=user,
        name="Farm2",
        slug="farm2",
        bbox_south=0.0,
        bbox_west=0.0,
        bbox_north=0.2,
        bbox_east=0.2,
        is_active=True,
    )
    target_date = date.today() - timedelta(days=1)
    NdviObservation.objects.create(
        farm=farm,
        engine="stac",
        bucket_date=target_date,
        mean=0.3,
    )

    with patch(
        "ndvi.farm_state._enqueue_coverage_compute", new=MagicMock()
    ) as mock_enqueue:
        result = build_farm_state(farm=farm, engine="stac")
        assert result.coverage_pct is None
        mock_enqueue.assert_not_called()


def test_coverage_lock_key_format() -> None:
    result = _coverage_lock_key(
        farm_id=1, engine="stac", target_date=date(2024, 1, 1), threshold=0.5
    )
    assert result == "farm_state:coverage:lock:1:stac:2024-01-01:0.500"


def test_coverage_threshold_default() -> None:
    threshold = get_coverage_threshold()
    assert 0.0 <= threshold <= 1.0


def test_coverage_cache_ttl_seconds() -> None:
    ttl = get_coverage_cache_ttl_seconds()
    assert ttl > 0


def test_coverage_lock_seconds() -> None:
    lock_ttl = get_coverage_lock_seconds()
    assert lock_ttl > 0


def test_trend_window_days() -> None:
    window = get_trend_window_days()
    assert 1 <= window <= 365


def test_compute_mean_ndvi_returns_mean() -> None:
    observations = [
        NdviObservation(
            farm_id=1, engine="stac", bucket_date=date(2024, 1, 1), mean=0.2
        ),
        NdviObservation(
            farm_id=1, engine="stac", bucket_date=date(2024, 1, 2), mean=0.4
        ),
        NdviObservation(
            farm_id=1, engine="stac", bucket_date=date(2024, 1, 3), mean=0.6
        ),
    ]
    result = _compute_mean_ndvi(observations)
    assert result is not None
    assert abs(result - 0.4) < 0.001


def test_compute_max_ndvi_returns_max() -> None:
    observations = [
        NdviObservation(
            farm_id=1, engine="stac", bucket_date=date(2024, 1, 1), mean=0.2
        ),
        NdviObservation(
            farm_id=1, engine="stac", bucket_date=date(2024, 1, 2), mean=0.8
        ),
        NdviObservation(
            farm_id=1, engine="stac", bucket_date=date(2024, 1, 3), mean=0.6
        ),
    ]
    result = _compute_max_ndvi(observations)
    assert result == 0.8


def test_compute_mean_ndvi_empty_returns_none() -> None:
    result = _compute_mean_ndvi([])
    assert result is None


def test_compute_max_ndvi_empty_returns_none() -> None:
    result = _compute_max_ndvi([])
    assert result is None
