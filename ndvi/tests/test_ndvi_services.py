from __future__ import annotations

# ruff: noqa: S101
import secrets
from datetime import date, timedelta
from decimal import Decimal
from typing import Protocol

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import caches
from rest_framework.exceptions import ValidationError

from farms.models import Farm
from ndvi.engines.base import BBox
from ndvi.models import NdviJob, NdviObservation
from ndvi.services import (
    LatestParams,
    TimeseriesParams,
    cache_latest_response,
    enforce_quota,
    enqueue_job,
    get_cached_latest_response,
    get_default_ndvi_engine_name,
    get_engine,
    get_max_daterange_days,
    is_stale,
    normalize_latest_params,
    normalize_timeseries_params,
    resolve_ndvi_engine_name,
)


class SettingsLike(Protocol):
    NDVI_ENGINE: str
    NDVI_STAC_COLLECTION: str


@pytest.mark.django_db
def test_get_engine_invalid_name_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported NDVI engine"):
        get_engine("bogus")


def test_get_engine_stac_returns_engine(
    settings: SettingsLike,
) -> None:
    settings.NDVI_STAC_COLLECTION = "collection"
    engine = get_engine("stac")
    assert engine is not None


def test_resolve_ndvi_engine_name_reads_settings_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from django.conf import settings

    monkeypatch.setattr(settings, "NDVI_ENGINE", "sentinelhub")
    assert resolve_ndvi_engine_name(None) == "sentinelhub"
    monkeypatch.setattr(settings, "NDVI_ENGINE", "stac")
    assert resolve_ndvi_engine_name(None) == "stac"


def test_no_default_engine_constant() -> None:
    import ndvi.services as services

    assert not hasattr(services, "DEFAULT_ENGINE")


def test_normalize_timeseries_params_validation() -> None:
    with pytest.raises(ValidationError):
        normalize_timeseries_params(
            start=date(2025, 1, 2),
            end=date(2025, 1, 1),
            step_days=7,
            max_cloud=20,
        )

    start = date(2020, 1, 1)
    end = start + timedelta(days=get_max_daterange_days() + 1)
    with pytest.raises(ValidationError):
        normalize_timeseries_params(
            start=start,
            end=end,
            step_days=7,
            max_cloud=20,
        )


def test_normalize_latest_params_clamps_values() -> None:
    params = normalize_latest_params(
        lookback_days=get_max_daterange_days() + 10, max_cloud=200
    )
    assert params.lookback_days == get_max_daterange_days()
    assert params.max_cloud == 100


@pytest.mark.django_db
def test_cache_latest_response_round_trip() -> None:
    caches["default"].clear()
    payload = {"ok": True}
    params = LatestParams(lookback_days=7, max_cloud=30)
    default_engine = get_default_ndvi_engine_name()
    cache_latest_response(
        owner_id=1,
        farm_id=2,
        engine=default_engine,
        params=params,
        payload=payload,
    )
    cached = get_cached_latest_response(
        owner_id=1,
        farm_id=2,
        engine=default_engine,
        params=params,
    )
    assert cached == payload

    # Ensure cache entry respects the TTL path (coverage for cache set).
    assert caches["default"].get(
        f"ndvi:cache:latest:1:2:{default_engine}:7:30"
    )


def test_enforce_quota_raises_for_large_bbox() -> None:
    huge = BBox(
        south=Decimal("-90"),
        west=Decimal("-180"),
        north=Decimal("90"),
        east=Decimal("180"),
    )
    farm = Farm(
        owner=get_user_model()(username="owner"),
        name="Farm",
        slug="farm",
    )
    with pytest.raises(ValidationError):
        enforce_quota(farm, huge)


@pytest.mark.django_db
def test_enqueue_job_returns_existing() -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="job-owner",
        email="job-owner@example.com",
        password=password,
    )
    farm = Farm.objects.create(owner=user, name="Farm", slug="farm")
    params = {
        "start": date(2025, 1, 1),
        "end": date(2025, 1, 2),
        "step_days": 7,
        "max_cloud": 30,
    }
    default_engine = get_default_ndvi_engine_name()
    first = enqueue_job(
        owner_id=user.id,
        farm=farm,
        engine_name=default_engine,
        job_type=NdviJob.JobType.GAP_FILL,
        params=params,
    )
    second = enqueue_job(
        owner_id=user.id,
        farm=farm,
        engine_name=default_engine,
        job_type=NdviJob.JobType.GAP_FILL,
        params=params,
    )
    assert first.id == second.id


@pytest.mark.django_db
def test_is_stale_checks_observation_age() -> None:
    assert is_stale(None, lookback_days=7)

    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="obs-owner",
        email="obs-owner@example.com",
        password=password,
    )
    farm = Farm.objects.create(owner=user, name="Farm", slug="farm-obs")
    default_engine = get_default_ndvi_engine_name()
    observation = NdviObservation.objects.create(
        farm=farm,
        engine=default_engine,
        bucket_date=date.today(),
        mean=0.2,
    )
    assert not is_stale(observation, lookback_days=7)


@pytest.mark.django_db
def test_enqueue_job_defaults_to_settings_engine(
    settings: SettingsLike,
) -> None:
    settings.NDVI_ENGINE = "stac"
    user = get_user_model().objects.create_user(
        username="default-engine",
        email="default-engine@example.com",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(owner=user, name="Farm", slug="farm-default")
    params = {
        "start": date(2025, 1, 1),
        "end": date(2025, 1, 2),
        "step_days": 7,
        "max_cloud": 30,
    }
    job = enqueue_job(
        owner_id=user.id,
        farm=farm,
        engine_name=None,
        job_type=NdviJob.JobType.GAP_FILL,
        params=params,
    )
    assert job.engine == "stac"
    assert job.engine != "sentinelhub"


@pytest.mark.django_db
def test_enqueue_job_override_engine_persists(
    settings: SettingsLike,
) -> None:
    settings.NDVI_ENGINE = "stac"
    user = get_user_model().objects.create_user(
        username="override-engine",
        email="override-engine@example.com",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(owner=user, name="Farm", slug="farm-override")
    params = {
        "start": date(2025, 1, 1),
        "end": date(2025, 1, 2),
        "step_days": 7,
        "max_cloud": 30,
    }
    job = enqueue_job(
        owner_id=user.id,
        farm=farm,
        engine_name="sentinelhub",
        job_type=NdviJob.JobType.GAP_FILL,
        params=params,
    )
    assert job.engine == "sentinelhub"


def test_timeseries_params_dataclass() -> None:
    params = TimeseriesParams(
        start=date(2025, 1, 1),
        end=date(2025, 1, 2),
        step_days=7,
        max_cloud=30,
    )
    assert params.step_days == 7
    assert params.max_cloud == 30


def test_latest_params_dataclass() -> None:
    params = LatestParams(lookback_days=7, max_cloud=30)
    assert params.lookback_days == 7
    assert params.max_cloud == 30
