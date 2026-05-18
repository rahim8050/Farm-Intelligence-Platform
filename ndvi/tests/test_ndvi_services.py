from __future__ import annotations

# ruff: noqa: S101
import secrets
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Protocol
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import caches
from rest_framework.exceptions import ValidationError

from farms.models import Farm
from ndvi.engines.base import BBox
from ndvi.models import NdviJob, NdviObservation
from ndvi.services import (
    NDVI_LATEST_CACHE_VERSION,
    LatestParams,
    TimeseriesParams,
    _determine_observation_state,
    cache_latest_response,
    dispatch_farm_state_coverage,
    dispatch_ndvi_job,
    enforce_quota,
    enqueue_job,
    get_cached_latest_response,
    get_default_ndvi_engine_name,
    get_engine,
    get_max_daterange_days,
    get_ndvi_append_only,
    get_ndvi_queue_backend,
    get_ndvi_version,
    is_stale,
    normalize_latest_params,
    normalize_timeseries_params,
    resolve_ndvi_engine_name,
    upsert_observations,
)


class SettingsLike(Protocol):
    NDVI_ENGINE: str
    NDVI_QUEUE_BACKEND: str
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


def test_get_ndvi_queue_backend_reads_settings(
    settings: SettingsLike,
) -> None:
    settings.NDVI_QUEUE_BACKEND = "stream"
    assert get_ndvi_queue_backend() == "stream"


def test_no_default_engine_constant() -> None:
    import ndvi.services as services

    assert not hasattr(services, "DEFAULT_ENGINE")


def test_dispatch_ndvi_job_uses_celery_delay() -> None:
    with patch("ndvi.tasks.run_ndvi_job.delay") as mock_delay:
        dispatch_ndvi_job(123)

    mock_delay.assert_called_once_with(123)


def test_dispatch_ndvi_job_uses_stream_when_backend_is_stream(
    settings: Any,
) -> None:
    settings.NDVI_QUEUE_BACKEND = "stream"

    with patch("ndvi.streams.publish_ndvi_job") as mock_publish:
        mock_publish.return_value = "1713000000000-0"

        with patch.object(NdviJob, "objects") as mock_objects:
            mock_qs = MagicMock()
            mock_qs.get.return_value = MagicMock(id=123)
            mock_objects.select_related.return_value = mock_qs

            dispatch_ndvi_job(123)

        mock_qs.get.assert_called_once_with(id=123)
        mock_publish.assert_called_once()


def test_dispatch_farm_state_coverage_uses_celery_delay() -> None:
    target_date = date(2025, 1, 3)

    with patch("ndvi.tasks.compute_farm_state_coverage.delay") as mock_delay:
        dispatch_farm_state_coverage(
            farm_id=7,
            engine="stac",
            target_date=target_date,
            threshold=0.4,
        )

    mock_delay.assert_called_once_with(
        farm_id=7,
        engine="stac",
        target_date="2025-01-03",
        threshold=0.4,
    )


def test_dispatch_farm_state_coverage_uses_stream_when_backend_is_stream(
    settings: Any,
) -> None:
    settings.NDVI_QUEUE_BACKEND = "stream"
    target_date = date(2025, 1, 3)

    with patch("ndvi.streams.publish_farm_state_coverage") as mock_publish:
        mock_publish.return_value = "1713000000000-0"

        dispatch_farm_state_coverage(
            farm_id=7,
            engine="stac",
            target_date=target_date,
            threshold=0.4,
        )

    mock_publish.assert_called_once_with(
        farm_id=7,
        engine="stac",
        target_date=target_date,
        threshold=0.4,
    )


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
        f"ndvi:cache:v{NDVI_LATEST_CACHE_VERSION}:latest:1:2:{default_engine}:7:30"
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


def test_get_ndvi_version_default() -> None:
    assert get_ndvi_version() == "v1-legacy"


def test_get_ndvi_version_from_settings(settings: Any) -> None:
    settings.NDVI_VERSION = "v2.1-cloud-mask"
    assert get_ndvi_version() == "v2.1-cloud-mask"


def test_get_ndvi_append_only_default() -> None:
    assert get_ndvi_append_only() is False


def test_get_ndvi_append_only_from_settings(settings: Any) -> None:
    settings.NDVI_APPEND_ONLY = True
    assert get_ndvi_append_only() is True


def test_determine_observation_state_final() -> None:
    state = _determine_observation_state(0.15, max_cloud=30)
    assert state == "FINAL"


def test_determine_observation_state_raw_when_cloud_exceeds_limit() -> None:
    state = _determine_observation_state(0.80, max_cloud=30)
    assert state == "RAW"


def test_determine_observation_state_raw_when_cloud_none() -> None:
    state = _determine_observation_state(None, max_cloud=30)
    assert state == "RAW"


@pytest.mark.django_db
def test_upsert_observations_sets_version_and_state(
    settings: Any,
) -> None:
    settings.NDVI_ENGINE = "sentinelhub"
    user = get_user_model().objects.create_user(
        username="upsert-owner",
        email="upsert-owner@example.com",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(owner=user, name="Farm", slug="farm-upsert")
    from ndvi.engines.base import NdviPoint

    points = [
        NdviPoint(
            date=date(2025, 3, 1),
            mean=0.5,
            min=0.3,
            max=0.7,
            sample_count=100,
            cloud_fraction=0.10,
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
    assert obs.version == "v1-legacy"
    assert obs.state == "FINAL"
    assert obs.is_latest is True


@pytest.mark.django_db
def test_upsert_observations_marks_cloudy_as_raw(
    settings: Any,
) -> None:
    settings.NDVI_ENGINE = "sentinelhub"
    user = get_user_model().objects.create_user(
        username="upsert-raw",
        email="upsert-raw@example.com",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(owner=user, name="Farm", slug="farm-raw")
    from ndvi.engines.base import NdviPoint

    points = [
        NdviPoint(
            date=date(2025, 3, 1),
            mean=0.5,
            min=0.3,
            max=0.7,
            sample_count=100,
            cloud_fraction=None,
        ),
    ]
    saved = upsert_observations(
        farm=farm,
        engine="sentinelhub",
        max_cloud=30,
        points=points,
    )
    assert len(saved) == 1
    assert saved[0].state == "RAW"


@pytest.mark.django_db
def test_upsert_observations_append_only_creates_new_row(
    settings: Any,
) -> None:
    settings.NDVI_ENGINE = "sentinelhub"
    settings.NDVI_VERSION = "v2.0-test"
    settings.NDVI_APPEND_ONLY = True
    user = get_user_model().objects.create_user(
        username="append-owner",
        email="append-owner@example.com",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(owner=user, name="Farm", slug="farm-append")
    from ndvi.engines.base import NdviPoint

    points = [
        NdviPoint(
            date=date(2025, 3, 1),
            mean=0.5,
            min=0.3,
            max=0.7,
            sample_count=100,
            cloud_fraction=0.10,
        ),
    ]
    first = upsert_observations(
        farm=farm,
        engine="sentinelhub",
        max_cloud=30,
        points=points,
    )
    assert len(first) == 1
    assert first[0].is_latest is True

    settings.NDVI_VERSION = "v2.1-updated"
    points2 = [
        NdviPoint(
            date=date(2025, 3, 1),
            mean=0.6,
            min=0.4,
            max=0.8,
            sample_count=120,
            cloud_fraction=0.05,
        ),
    ]
    second = upsert_observations(
        farm=farm,
        engine="sentinelhub",
        max_cloud=30,
        points=points2,
    )
    assert len(second) == 1
    assert second[0].is_latest is True
    assert second[0].id != first[0].id

    first[0].refresh_from_db()
    assert first[0].is_latest is False

    total = NdviObservation.objects.filter(
        farm=farm, engine="sentinelhub", bucket_date=date(2025, 3, 1)
    ).count()
    assert total == 2


@pytest.mark.django_db
def test_upsert_observations_append_only_idempotent_same_version(
    settings: Any,
) -> None:
    settings.NDVI_ENGINE = "sentinelhub"
    settings.NDVI_VERSION = "v2.0-test"
    settings.NDVI_APPEND_ONLY = True
    user = get_user_model().objects.create_user(
        username="idempotent-owner",
        email="idempotent-owner@example.com",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(owner=user, name="Farm", slug="farm-idempotent")
    from ndvi.engines.base import NdviPoint

    points = [
        NdviPoint(
            date=date(2025, 3, 1),
            mean=0.5,
            min=0.3,
            max=0.7,
            sample_count=100,
            cloud_fraction=0.10,
        ),
    ]
    first = upsert_observations(
        farm=farm,
        engine="sentinelhub",
        max_cloud=30,
        points=points,
    )
    second = upsert_observations(
        farm=farm,
        engine="sentinelhub",
        max_cloud=30,
        points=points,
    )
    assert first[0].id == second[0].id
    total = NdviObservation.objects.filter(
        farm=farm, engine="sentinelhub", bucket_date=date(2025, 3, 1)
    ).count()
    assert total == 1


@pytest.mark.django_db
def test_upsert_observations_skips_overly_cloudy(
    settings: Any,
) -> None:
    settings.NDVI_ENGINE = "sentinelhub"
    user = get_user_model().objects.create_user(
        username="cloudy-skip",
        email="cloudy-skip@example.com",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(owner=user, name="Farm", slug="farm-cloudy")
    from ndvi.engines.base import NdviPoint

    points = [
        NdviPoint(
            date=date(2025, 3, 1),
            mean=0.5,
            min=0.3,
            max=0.7,
            sample_count=100,
            cloud_fraction=0.90,
        ),
    ]
    saved = upsert_observations(
        farm=farm,
        engine="sentinelhub",
        max_cloud=30,
        points=points,
    )
    assert len(saved) == 0
