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
    build_farm_state,
    get_coverage_threshold,
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
