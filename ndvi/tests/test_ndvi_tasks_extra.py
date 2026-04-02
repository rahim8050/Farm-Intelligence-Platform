from __future__ import annotations

# ruff: noqa: S101
import secrets
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from farms.models import Farm
from ndvi.engines.base import NdviPoint
from ndvi.models import NdviJob, NdviObservation
from ndvi.raster.sentinelhub_engine import (
    MAX_ERROR_SNIPPET_CHARS,
    SentinelHubRasterError,
)
from ndvi.stac_client import StacProcessingError
from ndvi.tasks import (
    enqueue_daily_refresh,
    enqueue_weekly_gap_fill,
    run_ndvi_job,
)


@pytest.mark.django_db
def test_run_ndvi_job_refresh_latest_creates_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="refresh-owner",
        email="refresh-owner@example.com",
        password=password,
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
    job = NdviJob.objects.create(
        owner=user,
        farm=farm,
        engine="sentinelhub",
        job_type=NdviJob.JobType.REFRESH_LATEST,
        lookback_days=7,
        max_cloud=20,
        request_hash="refresh-hash",
    )
    dummy_engine = MagicMock()
    dummy_engine.get_latest.return_value = NdviPoint(
        date=date(2025, 1, 1), mean=0.3
    )
    monkeypatch.setattr("ndvi.tasks.acquire_lock", lambda *_, **__: True)
    monkeypatch.setattr("ndvi.tasks.get_engine", lambda *_: dummy_engine)

    result = run_ndvi_job.apply(args=[job.id]).get()
    assert result == "ok"
    assert NdviObservation.objects.filter(farm=farm).count() == 1


@pytest.mark.django_db
def test_run_ndvi_job_timeseries_skips_empty_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="gap-owner",
        email="gap-owner@example.com",
        password=password,
    )
    farm = Farm.objects.create(
        owner=user,
        name="Farm",
        slug="farm-gap",
        bbox_south=0.0,
        bbox_west=0.0,
        bbox_north=0.2,
        bbox_east=0.2,
        is_active=True,
    )
    job = NdviJob.objects.create(
        owner=user,
        farm=farm,
        engine="sentinelhub",
        job_type=NdviJob.JobType.GAP_FILL,
        start=date(2025, 1, 1),
        end=date(2025, 1, 8),
        step_days=7,
        max_cloud=30,
        request_hash="gap-hash",
    )
    dummy_engine = MagicMock()
    dummy_engine.get_timeseries.return_value = []
    monkeypatch.setattr("ndvi.tasks.acquire_lock", lambda *_, **__: True)
    monkeypatch.setattr("ndvi.tasks.get_engine", lambda *_: dummy_engine)
    upsert = MagicMock()
    monkeypatch.setattr("ndvi.tasks.upsert_observations", upsert)

    result = run_ndvi_job.apply(args=[job.id]).get()
    assert result == "ok"
    upsert.assert_not_called()


@pytest.mark.django_db
def test_run_ndvi_job_invalid_raster_size_returns_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="raster-owner",
        email="raster-owner@example.com",
        password=password,
    )
    farm = Farm.objects.create(
        owner=user,
        name="Farm",
        slug="farm-raster",
        bbox_south=0.0,
        bbox_west=0.0,
        bbox_north=0.2,
        bbox_east=0.2,
        is_active=True,
    )
    job = NdviJob.objects.create(
        owner=user,
        farm=farm,
        engine="sentinelhub",
        job_type=NdviJob.JobType.RASTER_PNG,
        start=date(2025, 1, 1),
        end=date(2025, 1, 1),
        step_days=64,
        max_cloud=30,
        request_hash="raster-hash",
    )
    monkeypatch.setattr("ndvi.tasks.acquire_lock", lambda *_, **__: True)

    result = run_ndvi_job.apply(args=[job.id]).get()
    assert result == "invalid"
    job.refresh_from_db()
    assert job.status == NdviJob.JobStatus.FAILED


@pytest.mark.django_db
@override_settings(NDVI_RASTER_MAX_SIZE=2048)
def test_run_ndvi_job_raster_pixel_limit_returns_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="raster-big",
        email="raster-big@example.com",
        password=password,
    )
    farm = Farm.objects.create(
        owner=user,
        name="Farm",
        slug="farm-big",
        bbox_south=0.0,
        bbox_west=0.0,
        bbox_north=0.2,
        bbox_east=0.2,
        is_active=True,
    )
    job = NdviJob.objects.create(
        owner=user,
        farm=farm,
        engine="sentinelhub",
        job_type=NdviJob.JobType.RASTER_PNG,
        start=date(2025, 1, 1),
        end=date(2025, 1, 1),
        step_days=2048,
        max_cloud=30,
        request_hash="raster-big-hash",
    )
    monkeypatch.setattr("ndvi.tasks.acquire_lock", lambda *_, **__: True)

    result = run_ndvi_job.apply(args=[job.id]).get()
    assert result == "invalid"
    job.refresh_from_db()
    assert job.status == NdviJob.JobStatus.FAILED


@pytest.mark.django_db
def test_run_ndvi_job_raster_size_and_error_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="raster-error",
        email="raster-error@example.com",
        password=password,
    )
    farm = Farm.objects.create(
        owner=user,
        name="Farm",
        slug="farm-error",
        bbox_south=0.0,
        bbox_west=0.0,
        bbox_north=0.2,
        bbox_east=0.2,
        is_active=True,
    )
    job = NdviJob.objects.create(
        owner=user,
        farm=farm,
        engine="sentinelhub",
        job_type=NdviJob.JobType.RASTER_PNG,
        start=date(2025, 1, 1),
        end=date(2025, 1, 1),
        step_days=256,
        max_cloud=20,
        request_hash="raster-error-hash",
    )
    captured: dict[str, int] = {}

    snippet_text = "upstream bad request snippet..."

    def fake_render_png(
        *,
        farm: object,
        bbox: object,
        day: object,
        size: int,
        max_cloud: object,
        engine_name: object,
        job_id: int | None = None,
    ) -> tuple[bytes, str]:
        captured["size"] = size
        raise SentinelHubRasterError(
            status_code=400,
            snippet=snippet_text,
        )

    monkeypatch.setattr("ndvi.tasks.acquire_lock", lambda *_, **__: True)
    monkeypatch.setattr("ndvi.tasks.render_ndvi_png", fake_render_png)

    with patch.object(
        run_ndvi_job, "retry", side_effect=RuntimeError("retry")
    ):
        with pytest.raises(RuntimeError, match="retry"):
            run_ndvi_job.apply(args=[job.id]).get()

    job.refresh_from_db()
    assert job.status == NdviJob.JobStatus.FAILED
    assert captured["size"] == 256
    assert job.last_error is not None
    assert "status=400" in job.last_error
    body = job.last_error.split("body=", 1)[1]
    assert body == snippet_text
    assert body.endswith("...")
    assert len(body) <= MAX_ERROR_SNIPPET_CHARS + 3


@pytest.mark.django_db
def test_run_ndvi_job_raster_stac_error_records_last_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="raster-stac-error",
        email="raster-stac-error@example.com",
        password=password,
    )
    farm = Farm.objects.create(
        owner=user,
        name="Farm",
        slug="farm-stac-error",
        bbox_south=0.0,
        bbox_west=0.0,
        bbox_north=0.2,
        bbox_east=0.2,
        is_active=True,
    )
    job = NdviJob.objects.create(
        owner=user,
        farm=farm,
        engine="stac",
        job_type=NdviJob.JobType.RASTER_PNG,
        start=date(2025, 1, 1),
        end=date(2025, 1, 1),
        step_days=256,
        max_cloud=20,
        request_hash="stac-raster-error-hash",
    )

    def fake_render_png(**_: object) -> tuple[bytes, str]:
        raise StacProcessingError("stac raster failed")

    monkeypatch.setattr("ndvi.tasks.acquire_lock", lambda *_, **__: True)
    monkeypatch.setattr("ndvi.tasks.render_ndvi_png", fake_render_png)

    result = run_ndvi_job.apply(args=[job.id]).get()
    assert result == "invalid"
    job.refresh_from_db()
    assert job.status == NdviJob.JobStatus.FAILED
    assert job.last_error is not None
    assert "stac raster failed" in job.last_error


@pytest.mark.django_db
def test_run_ndvi_job_exception_triggers_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="error-owner",
        email="error-owner@example.com",
        password=password,
    )
    farm = Farm.objects.create(
        owner=user,
        name="Farm",
        slug="farm-error",
        bbox_south=0.0,
        bbox_west=0.0,
        bbox_north=0.2,
        bbox_east=0.2,
        is_active=True,
    )
    job = NdviJob.objects.create(
        owner=user,
        farm=farm,
        engine="sentinelhub",
        job_type=NdviJob.JobType.GAP_FILL,
        start=date(2025, 1, 1),
        end=date(2025, 1, 8),
        step_days=7,
        max_cloud=30,
        request_hash="error-hash",
    )

    class DummyEngine:
        def get_timeseries(self, **_: object) -> list[NdviPoint]:
            raise RuntimeError("boom")

        def get_latest(self, **_: object) -> NdviPoint | None:
            return None

    monkeypatch.setattr("ndvi.tasks.acquire_lock", lambda *_, **__: True)
    monkeypatch.setattr("ndvi.tasks.get_engine", lambda *_: DummyEngine())

    with patch.object(
        run_ndvi_job, "retry", side_effect=RuntimeError("retry")
    ):
        with pytest.raises(RuntimeError, match="retry"):
            run_ndvi_job.apply(args=[job.id]).get()
    job.refresh_from_db()
    assert job.status == NdviJob.JobStatus.FAILED


@pytest.mark.django_db
def test_enqueue_daily_refresh_only_bbox_farms() -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="queue-owner",
        email="queue-owner@example.com",
        password=password,
    )
    Farm.objects.create(
        owner=user,
        name="Active",
        slug="active",
        bbox_south=0.0,
        bbox_west=0.0,
        bbox_north=0.2,
        bbox_east=0.2,
        is_active=True,
    )
    Farm.objects.create(owner=user, name="No bbox", slug="nobbox")
    with patch("ndvi.tasks.dispatch_ndvi_job") as mock_delay:
        count = enqueue_daily_refresh()
    assert count == 1
    assert (
        NdviJob.objects.filter(job_type=NdviJob.JobType.REFRESH_LATEST).count()
        == 1
    )
    mock_delay.assert_called_once()


@pytest.mark.django_db
def test_enqueue_weekly_gap_fill_only_bbox_farms() -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="queue-weekly",
        email="queue-weekly@example.com",
        password=password,
    )
    Farm.objects.create(
        owner=user,
        name="Active",
        slug="active-weekly",
        bbox_south=0.0,
        bbox_west=0.0,
        bbox_north=0.2,
        bbox_east=0.2,
        is_active=True,
    )
    Farm.objects.create(owner=user, name="No bbox", slug="nobbox-weekly")
    with patch("ndvi.tasks.dispatch_ndvi_job") as mock_delay:
        count = enqueue_weekly_gap_fill()
    assert count == 1
    assert (
        NdviJob.objects.filter(job_type=NdviJob.JobType.GAP_FILL).count() == 1
    )
    mock_delay.assert_called_once()
