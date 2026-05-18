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
    compute_provenance_hash,
    detect_anomalies,
    dispatch_farm_state_coverage,
    dispatch_ndvi_job,
    enforce_quota,
    enqueue_job,
    get_cached_latest_response,
    get_default_ndvi_engine_name,
    get_engine,
    get_latest_observations,
    get_max_daterange_days,
    get_ndvi_anomaly_threshold,
    get_ndvi_append_only,
    get_ndvi_queue_backend,
    get_ndvi_queue_name,
    get_ndvi_recompute_backpressure_threshold,
    get_ndvi_recompute_chunk_size,
    get_ndvi_recompute_max_window_days,
    get_ndvi_upsert_max_retries,
    get_ndvi_upsert_retry_delay,
    get_ndvi_upsert_retry_jitter,
    get_ndvi_version,
    get_ndvi_version_registry,
    get_valid_observations_qs,
    is_analytically_valid,
    is_stale,
    normalize_latest_params,
    normalize_timeseries_params,
    recompute_stale_observations,
    resolve_ndvi_engine_name,
    upsert_observations,
    validate_provenance,
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


@pytest.mark.django_db
def test_dispatch_ndvi_job_uses_celery_delay() -> None:
    with patch("ndvi.services.get_ndvi_queue_backend", return_value="celery"):
        with patch("ndvi.tasks.run_ndvi_job.apply_async") as mock_apply:
            dispatch_ndvi_job(123)
            mock_apply.assert_called_once()


@pytest.mark.django_db
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


@pytest.mark.django_db
def test_dispatch_farm_state_coverage_uses_celery_delay() -> None:
    target_date = date(2025, 1, 3)

    with patch("ndvi.services.get_ndvi_queue_backend", return_value="celery"):
        with patch(
            "ndvi.tasks.compute_farm_state_coverage.apply_async"
        ) as mock_apply:
            dispatch_farm_state_coverage(
                farm_id=7,
                engine="stac",
                target_date=target_date,
                threshold=0.4,
            )

            mock_apply.assert_called_once()
            call_kwargs = mock_apply.call_args
            assert call_kwargs.kwargs["kwargs"]["farm_id"] == 7
            assert call_kwargs.kwargs["kwargs"]["engine"] == "stac"
            assert call_kwargs.kwargs["kwargs"]["target_date"] == "2025-01-03"
            assert call_kwargs.kwargs["kwargs"]["threshold"] == 0.4


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


def test_get_ndvi_recompute_max_window_days_default() -> None:
    assert get_ndvi_recompute_max_window_days() == 90


def test_get_ndvi_recompute_chunk_size_default() -> None:
    assert get_ndvi_recompute_chunk_size() == 50


def test_get_ndvi_recompute_backpressure_threshold_default() -> None:
    assert get_ndvi_recompute_backpressure_threshold() == 1000


def test_get_ndvi_anomaly_threshold_default() -> None:
    assert get_ndvi_anomaly_threshold() == 0.30


def test_get_ndvi_version_registry_default() -> None:
    registry = get_ndvi_version_registry()
    assert len(registry) >= 1
    assert "version" in registry[0]
    assert "description" in registry[0]


def test_get_ndvi_version_registry_from_settings(settings: Any) -> None:
    settings.NDVI_VERSION_REGISTRY = [
        {
            "version": "v2.1",
            "description": "Cloud mask v2",
            "release_date": "2026-05-01",
            "author": "team",
        },
    ]
    registry = get_ndvi_version_registry()
    assert registry[0]["version"] == "v2.1"
    assert registry[0]["author"] == "team"


def test_observation_state_transitions() -> None:
    obs = NdviObservation(
        farm_id=1,
        engine="stac",
        bucket_date=date(2025, 1, 1),
        mean=0.5,
        state="RAW",
    )
    assert obs.can_transition_to("FINAL") is True
    assert obs.can_transition_to("SUPERSEDED") is True
    assert obs.can_transition_to("RAW") is False

    obs.state = "FINAL"
    assert obs.can_transition_to("SUPERSEDED") is True
    assert obs.can_transition_to("RAW") is False

    obs.state = "SUPERSEDED"
    assert obs.can_transition_to("FINAL") is False
    assert obs.can_transition_to("RAW") is False


def test_observation_transition_state_raises_on_invalid() -> None:
    obs = NdviObservation(
        farm_id=1,
        engine="stac",
        bucket_date=date(2025, 1, 1),
        mean=0.5,
        state="FINAL",
    )
    with pytest.raises(ValueError, match="Cannot transition"):
        obs.transition_state("RAW")


@pytest.mark.django_db
def test_get_latest_observations_filters_and_orders(
    settings: Any,
) -> None:
    settings.NDVI_ENGINE = "sentinelhub"
    user = get_user_model().objects.create_user(
        username="latest-owner",
        email="latest-owner@example.com",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(owner=user, name="Farm", slug="farm-latest")
    NdviObservation.objects.create(
        farm=farm,
        engine="sentinelhub",
        bucket_date=date(2025, 3, 1),
        mean=0.5,
        is_latest=True,
        state="FINAL",
        version="v1",
    )
    NdviObservation.objects.create(
        farm=farm,
        engine="sentinelhub",
        bucket_date=date(2025, 3, 2),
        mean=0.6,
        is_latest=True,
        state="FINAL",
        version="v1",
    )
    NdviObservation.objects.create(
        farm=farm,
        engine="sentinelhub",
        bucket_date=date(2025, 3, 3),
        mean=0.7,
        is_latest=False,
        state="FINAL",
        version="v1",
    )
    NdviObservation.objects.create(
        farm=farm,
        engine="sentinelhub",
        bucket_date=date(2025, 3, 4),
        mean=0.8,
        is_latest=True,
        state="RAW",
        version="v1",
    )

    results = get_latest_observations(farm=farm, engine="sentinelhub")
    assert len(results) == 2
    assert results[0].bucket_date == date(2025, 3, 1)
    assert results[1].bucket_date == date(2025, 3, 2)


def test_detect_anomalies_identifies_spikes() -> None:
    obs = [
        NdviObservation(
            farm_id=1,
            engine="stac",
            bucket_date=date(2025, 1, d),
            mean=0.5,
        )
        for d in range(1, 6)
    ]
    obs[2].mean = 0.95

    anomalies = detect_anomalies(obs, threshold=0.30)
    assert len(anomalies) == 1
    assert anomalies[0][1] == "spike"
    assert abs(anomalies[0][2] - 0.45) < 0.01


def test_detect_anomalies_identifies_drops() -> None:
    obs = [
        NdviObservation(
            farm_id=1,
            engine="stac",
            bucket_date=date(2025, 1, d),
            mean=0.5,
        )
        for d in range(1, 6)
    ]
    obs[3].mean = 0.10

    anomalies = detect_anomalies(obs, threshold=0.30)
    assert len(anomalies) == 1
    assert anomalies[0][1] == "drop"


def test_detect_anomalies_empty_when_few_observations() -> None:
    obs = [
        NdviObservation(
            farm_id=1,
            engine="stac",
            bucket_date=date(2025, 1, 1),
            mean=0.5,
        ),
    ]
    assert detect_anomalies(obs) == []


@pytest.mark.django_db
def test_recompute_stale_observations_finds_mismatched(
    settings: Any,
) -> None:
    settings.NDVI_ENGINE = "sentinelhub"
    settings.NDVI_VERSION = "v2.0-current"
    user = get_user_model().objects.create_user(
        username="recompute-owner",
        email="recompute-owner@example.com",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(owner=user, name="Farm", slug="farm-recompute")
    NdviObservation.objects.create(
        farm=farm,
        engine="sentinelhub",
        bucket_date=date(2025, 3, 1),
        mean=0.5,
        is_latest=True,
        state="FINAL",
        version="v1-legacy",
    )
    NdviObservation.objects.create(
        farm=farm,
        engine="sentinelhub",
        bucket_date=date(2025, 3, 2),
        mean=0.6,
        is_latest=True,
        state="FINAL",
        version="v2.0-current",
    )

    results = recompute_stale_observations(
        engine="sentinelhub",
        start_date=date(2025, 3, 1),
        end_date=date(2025, 3, 31),
    )
    assert len(results) == 1
    assert results[0]["farm_id"] == farm.id
    assert results[0]["current_version"] == "v1-legacy"


@pytest.mark.django_db
def test_recompute_stale_observations_respects_max_window(
    settings: Any,
) -> None:
    settings.NDVI_ENGINE = "sentinelhub"
    settings.NDVI_RECOMPUTE_MAX_WINDOW_DAYS = 30
    with pytest.raises(ValueError, match="exceeds"):
        recompute_stale_observations(
            engine="sentinelhub",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 6, 1),
        )


@pytest.mark.django_db
def test_upsert_observations_with_source_scene_id(
    settings: Any,
) -> None:
    settings.NDVI_ENGINE = "sentinelhub"
    settings.NDVI_VERSION = "v2.0-test"
    settings.NDVI_APPEND_ONLY = True
    user = get_user_model().objects.create_user(
        username="scene-owner",
        email="scene-owner@example.com",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(owner=user, name="Farm", slug="farm-scene")
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
        source_scene_ids={date(2025, 3, 1): "S2A_20250301_T32TLS"},
        provenance={"scl_mask": True, "engine_version": "2.0"},
    )
    assert len(saved) == 1
    assert saved[0].source_scene_id == "S2A_20250301_T32TLS"
    assert saved[0].provenance == {"scl_mask": True, "engine_version": "2.0"}
    assert saved[0].acquired_at is not None
    assert saved[0].computed_at is not None
    assert saved[0].ingested_at is not None


@pytest.mark.django_db
def test_upsert_observations_scene_idempotent(
    settings: Any,
) -> None:
    settings.NDVI_ENGINE = "sentinelhub"
    settings.NDVI_APPEND_ONLY = True
    user = get_user_model().objects.create_user(
        username="scene-idem",
        email="scene-idem@example.com",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(owner=user, name="Farm", slug="farm-scene-idem")
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
    scene_id = "S2B_20250301_T32TLS"
    first = upsert_observations(
        farm=farm,
        engine="sentinelhub",
        max_cloud=30,
        points=points,
        source_scene_ids={date(2025, 3, 1): scene_id},
    )
    points2 = [
        NdviPoint(
            date=date(2025, 3, 1),
            mean=0.9,
            min=0.7,
            max=0.99,
            sample_count=200,
            cloud_fraction=0.01,
        ),
    ]
    second = upsert_observations(
        farm=farm,
        engine="sentinelhub",
        max_cloud=30,
        points=points2,
        source_scene_ids={date(2025, 3, 1): scene_id},
    )
    assert first[0].id == second[0].id
    assert second[0].mean == 0.5


def test_compute_provenance_hash_deterministic() -> None:
    prov = {"engine_version": "2.0", "scl_mask": True}
    h1 = compute_provenance_hash(prov)
    h2 = compute_provenance_hash(prov)
    assert h1 == h2
    assert len(h1) == 16


def test_compute_provenance_hash_order_independent() -> None:
    prov_a = {"engine_version": "2.0", "scl_mask": True}
    prov_b = {"scl_mask": True, "engine_version": "2.0"}
    assert compute_provenance_hash(prov_a) == compute_provenance_hash(prov_b)


def test_validate_provenance_accepts_valid_keys() -> None:
    prov = {
        "engine_version": "2.0",
        "scl_mask": True,
        "schema_version": "1",
    }
    result = validate_provenance(prov)
    assert result == prov


def test_validate_provenance_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError, match="Unrecognized provenance keys"):
        validate_provenance({"unknown_key": "value"})


def test_validate_provenance_rejects_bad_schema_version() -> None:
    with pytest.raises(ValueError, match="Unsupported provenance schema"):
        validate_provenance({"schema_version": "99"})


def test_validate_provenance_empty_returns_empty() -> None:
    assert validate_provenance({}) == {}


def test_observation_state_invalidated_rejected_transitions() -> None:
    obs = NdviObservation(
        farm_id=1,
        engine="stac",
        bucket_date=date(2025, 1, 1),
        mean=0.5,
        state="RAW",
    )
    assert obs.can_transition_to("REJECTED") is True

    obs.state = "FINAL"
    assert obs.can_transition_to("INVALIDATED") is True

    obs.state = "INVALIDATED"
    assert obs.can_transition_to("FINAL") is False
    assert obs.can_transition_to("SUPERSEDED") is False

    obs.state = "REJECTED"
    assert obs.can_transition_to("FINAL") is False


def test_get_ndvi_queue_name_defaults() -> None:
    assert get_ndvi_queue_name("ingestion") == "ndvi_ingestion"
    assert get_ndvi_queue_name("recompute") == "ndvi_recompute"
    assert get_ndvi_queue_name("analysis") == "ndvi_analysis"


def test_get_ndvi_queue_name_from_settings(settings: Any) -> None:
    settings.NDVI_QUEUE_INGESTION = "custom_ingest"
    assert get_ndvi_queue_name("ingestion") == "custom_ingest"


def test_get_ndvi_upsert_max_retries_default() -> None:
    assert get_ndvi_upsert_max_retries() == 3


def test_get_ndvi_upsert_retry_delay_default() -> None:
    assert get_ndvi_upsert_retry_delay() == 0.1


@pytest.mark.django_db
def test_dispatch_ndvi_job_uses_queue_isolation() -> None:
    with patch("ndvi.services.get_ndvi_queue_backend", return_value="celery"):
        with patch("ndvi.tasks.run_ndvi_job.apply_async") as mock_apply:
            dispatch_ndvi_job(123)
            mock_apply.assert_called_once()
            call_kwargs = mock_apply.call_args
            assert call_kwargs.kwargs.get("queue") == "ndvi_ingestion"


@pytest.mark.django_db
def test_dispatch_farm_state_coverage_uses_analysis_queue(
    settings: Any,
) -> None:
    settings.NDVI_QUEUE_BACKEND = "celery"
    target_date = date(2025, 1, 3)
    with patch(
        "ndvi.tasks.compute_farm_state_coverage.apply_async"
    ) as mock_apply:
        dispatch_farm_state_coverage(
            farm_id=7,
            engine="stac",
            target_date=target_date,
            threshold=0.4,
        )
        mock_apply.assert_called_once()
        call_kwargs = mock_apply.call_args
        assert call_kwargs.kwargs.get("queue") == "ndvi_analysis"


@pytest.mark.django_db
def test_upsert_observations_sets_provenance_hash(
    settings: Any,
) -> None:
    settings.NDVI_ENGINE = "sentinelhub"
    settings.NDVI_VERSION = "v2.0-test"
    user = get_user_model().objects.create_user(
        username="prov-hash",
        email="prov-hash@example.com",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(owner=user, name="Farm", slug="farm-prov-hash")
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
    prov = {"engine_version": "2.0", "scl_mask": True}
    saved = upsert_observations(
        farm=farm,
        engine="sentinelhub",
        max_cloud=30,
        points=points,
        provenance=prov,
    )
    assert len(saved) == 1
    expected_hash = compute_provenance_hash(prov)
    assert saved[0].provenance_hash == expected_hash
    assert saved[0].provenance == prov


def test_compute_provenance_hash_canonical_json() -> None:
    """Provenance hash uses strict canonical JSON (no whitespace)."""
    prov = {"engine_version": "2.0", "scl_mask": True}
    h = compute_provenance_hash(prov)
    assert len(h) == 16
    assert h == compute_provenance_hash(prov)


def test_compute_provenance_hash_unicode_safe() -> None:
    """Provenance hash handles unicode with ensure_ascii."""
    prov = {"description": "café"}
    h1 = compute_provenance_hash(prov)
    h2 = compute_provenance_hash({"description": "caf\u00e9"})
    assert h1 == h2


def test_get_ndvi_upsert_retry_jitter_default() -> None:
    assert get_ndvi_upsert_retry_jitter() == 0.05


@pytest.mark.django_db
def test_is_analytically_valid_final_latest() -> None:
    obs = NdviObservation(
        farm_id=1,
        engine="stac",
        bucket_date=date(2025, 1, 1),
        mean=0.5,
        is_latest=True,
        state="FINAL",
    )
    assert is_analytically_valid(obs) is True


@pytest.mark.django_db
def test_is_analytically_valid_rejects_raw() -> None:
    obs = NdviObservation(
        farm_id=1,
        engine="stac",
        bucket_date=date(2025, 1, 1),
        mean=0.5,
        is_latest=True,
        state="RAW",
    )
    assert is_analytically_valid(obs) is False


@pytest.mark.django_db
def test_is_analytically_valid_rejects_invalidated() -> None:
    obs = NdviObservation(
        farm_id=1,
        engine="stac",
        bucket_date=date(2025, 1, 1),
        mean=0.5,
        is_latest=True,
        state="INVALIDATED",
    )
    assert is_analytically_valid(obs) is False


@pytest.mark.django_db
def test_is_analytically_valid_rejects_rejected() -> None:
    obs = NdviObservation(
        farm_id=1,
        engine="stac",
        bucket_date=date(2025, 1, 1),
        mean=0.5,
        is_latest=True,
        state="REJECTED",
    )
    assert is_analytically_valid(obs) is False


@pytest.mark.django_db
def test_is_analytically_valid_rejects_not_latest() -> None:
    obs = NdviObservation(
        farm_id=1,
        engine="stac",
        bucket_date=date(2025, 1, 1),
        mean=0.5,
        is_latest=False,
        state="FINAL",
    )
    assert is_analytically_valid(obs) is False


@pytest.mark.django_db
def test_is_analytically_valid_rejects_null_mean() -> None:
    obs = NdviObservation(
        farm_id=1,
        engine="stac",
        bucket_date=date(2025, 1, 1),
        mean=None,  # type: ignore[misc]
        is_latest=True,
        state="FINAL",
    )
    assert is_analytically_valid(obs) is False


@pytest.mark.django_db
def test_get_valid_observations_qs_excludes_invalid(
    settings: Any,
) -> None:
    settings.NDVI_ENGINE = "sentinelhub"
    user = get_user_model().objects.create_user(
        username="valid-qs",
        email="valid-qs@example.com",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(owner=user, name="Farm", slug="farm-valid-qs")
    NdviObservation.objects.create(
        farm=farm,
        engine="sentinelhub",
        bucket_date=date(2025, 3, 1),
        mean=0.5,
        is_latest=True,
        state="FINAL",
        version="v1",
    )
    NdviObservation.objects.create(
        farm=farm,
        engine="sentinelhub",
        bucket_date=date(2025, 3, 2),
        mean=0.6,
        is_latest=True,
        state="INVALIDATED",
        version="v1",
    )
    NdviObservation.objects.create(
        farm=farm,
        engine="sentinelhub",
        bucket_date=date(2025, 3, 3),
        mean=0.7,
        is_latest=False,
        state="FINAL",
        version="v1",
    )

    qs = get_valid_observations_qs(farm=farm, engine="sentinelhub")
    assert qs.count() == 1
    first_obs = qs.first()
    assert first_obs is not None
    assert first_obs.bucket_date == date(2025, 3, 1)


@pytest.mark.django_db
def test_recompute_stale_observations_includes_dispatch_key(
    settings: Any,
) -> None:
    settings.NDVI_ENGINE = "sentinelhub"
    settings.NDVI_VERSION = "v2.0-current"
    user = get_user_model().objects.create_user(
        username="recompute-key",
        email="recompute-key@example.com",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(
        owner=user, name="Farm", slug="farm-recompute-key"
    )
    NdviObservation.objects.create(
        farm=farm,
        engine="sentinelhub",
        bucket_date=date(2025, 3, 1),
        mean=0.5,
        is_latest=True,
        state="FINAL",
        version="v1-legacy",
    )

    results = recompute_stale_observations(
        engine="sentinelhub",
        start_date=date(2025, 3, 1),
        end_date=date(2025, 3, 31),
    )
    assert len(results) == 1
    assert "dispatch_key" in results[0]
    assert len(results[0]["dispatch_key"]) == 16
