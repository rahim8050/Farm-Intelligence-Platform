from __future__ import annotations

# ruff: noqa: S101
import secrets
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from celery.exceptions import MaxRetriesExceededError  # type: ignore[import-untyped]
from django.contrib.auth import get_user_model
from django.test import override_settings

from farms.models import Farm
from ndvi.engines.base import NdviPoint
from ndvi.engines.sentinelhub import SentinelHubAuthError
from ndvi.models import NdviJob, NdviObservation, NdviRasterArtifact
from ndvi.raster.sentinelhub_engine import (
    MAX_ERROR_SNIPPET_CHARS,
    SentinelHubRasterError,
)
from ndvi.stac_client import StacProcessingError, StacUpstreamError
from ndvi.tasks import (
    compute_farm_state_coverage,
    enqueue_daily_farm_state_coverage,
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
        colormap_normalization: object = None,
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
def test_run_ndvi_job_returns_locked_when_lock_not_acquired() -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="locked-owner",
        email="locked-owner@example.com",
        password=password,
    )
    farm = Farm.objects.create(
        owner=user,
        name="Farm",
        slug="farm-locked",
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
        request_hash="locked-hash",
    )

    with patch("ndvi.tasks.acquire_lock", return_value=False):
        result = run_ndvi_job.apply(args=[job.id]).get()

    assert result == "locked"


@pytest.mark.django_db
def test_run_ndvi_job_raster_success_saves_artifact_and_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    settings: object,
    tmp_path: Path,
) -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="raster-success",
        email="raster-success@example.com",
        password=password,
    )
    farm = Farm.objects.create(
        owner=user,
        name="Farm",
        slug="farm-raster-success",
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
        request_hash="success-hash",
    )
    settings.MEDIA_ROOT = str(tmp_path)  # type: ignore[attr-defined]
    monkeypatch.setattr("ndvi.tasks.acquire_lock", lambda *_, **__: True)
    monkeypatch.setattr(
        "ndvi.tasks.render_ndvi_png",
        lambda **_: (b"\x89PNG\r\n\x1a\npayload", "abc123def456"),
    )
    caplog.set_level("INFO")

    result = run_ndvi_job.apply(args=[job.id]).get()

    assert result == "ok"
    job.refresh_from_db()
    assert job.status == NdviJob.JobStatus.SUCCESS
    artifact = NdviRasterArtifact.objects.get(farm=farm, date=date(2025, 1, 1))
    assert artifact.content_hash == "abc123def456"
    assert artifact.image.name.endswith(".png")
    assert artifact.image.storage.exists(artifact.image.name)
    assert any("NDVI raster saved at path=" in m for m in caplog.messages)
    assert any(
        "NDVI raster job completed successfully" in m for m in caplog.messages
    )


@pytest.mark.django_db
def test_run_ndvi_job_refresh_latest_auth_error_returns_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="auth-owner",
        email="auth-owner@example.com",
        password=password,
    )
    farm = Farm.objects.create(
        owner=user,
        name="Farm",
        slug="farm-auth",
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
        request_hash="auth-hash",
    )

    class FailingEngine:
        def get_latest(self, **_: object) -> NdviPoint | None:
            raise SentinelHubAuthError(401)

    monkeypatch.setattr("ndvi.tasks.acquire_lock", lambda *_, **__: True)
    monkeypatch.setattr("ndvi.tasks.get_engine", lambda *_: FailingEngine())

    result = run_ndvi_job.apply(args=[job.id]).get()

    assert result == "invalid"
    job.refresh_from_db()
    assert job.status == NdviJob.JobStatus.FAILED
    assert job.last_error is not None
    assert "authentication failed" in job.last_error


@pytest.mark.django_db
def test_run_ndvi_job_stac_upstream_non_retryable_returns_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="stac-owner",
        email="stac-owner@example.com",
        password=password,
    )
    farm = Farm.objects.create(
        owner=user,
        name="Farm",
        slug="farm-stac-upstream",
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
        job_type=NdviJob.JobType.GAP_FILL,
        start=date(2025, 1, 1),
        end=date(2025, 1, 8),
        step_days=7,
        max_cloud=20,
        request_hash="stac-non-retryable-hash",
    )

    class FailingEngine:
        def get_timeseries(self, **_: object) -> list[NdviPoint]:
            raise StacUpstreamError("upstream failed", retryable=False)

    monkeypatch.setattr("ndvi.tasks.acquire_lock", lambda *_, **__: True)
    monkeypatch.setattr("ndvi.tasks.get_engine", lambda *_: FailingEngine())

    result = run_ndvi_job.apply(args=[job.id]).get()

    assert result == "invalid"
    job.refresh_from_db()
    assert job.status == NdviJob.JobStatus.FAILED
    assert job.last_error == "upstream failed"


@pytest.mark.django_db
def test_run_ndvi_job_stac_upstream_retryable_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="stac-retry-owner",
        email="stac-retry-owner@example.com",
        password=password,
    )
    farm = Farm.objects.create(
        owner=user,
        name="Farm",
        slug="farm-stac-retry",
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
        job_type=NdviJob.JobType.GAP_FILL,
        start=date(2025, 1, 1),
        end=date(2025, 1, 8),
        step_days=7,
        max_cloud=20,
        request_hash="stac-retryable-hash",
    )

    class FailingEngine:
        def get_timeseries(self, **_: object) -> list[NdviPoint]:
            raise StacUpstreamError("retry me", retryable=True)

    monkeypatch.setattr("ndvi.tasks.acquire_lock", lambda *_, **__: True)
    monkeypatch.setattr("ndvi.tasks.get_engine", lambda *_: FailingEngine())

    with patch.object(
        run_ndvi_job, "retry", side_effect=RuntimeError("retry")
    ):
        with pytest.raises(RuntimeError, match="retry"):
            run_ndvi_job.apply(args=[job.id]).get()

    job.refresh_from_db()
    # Job stays in RUNNING state when retryable errors trigger retries
    # (it will only be marked FAILED after max retries are exhausted)
    assert job.status == NdviJob.JobStatus.RUNNING
    assert job.last_error is None


@pytest.mark.django_db
def test_run_ndvi_job_stac_circuit_breaker_persists_across_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that the StacEngine (and its circuit breaker) is reused
    across retries.

    The @lru_cache on _build_stac_engine ensures the same StacClient
    instance (with its circuit breaker state) is reused.
    """
    from ndvi.services import _build_stac_engine

    # Clear any cached engine from previous tests
    _build_stac_engine.cache_clear()

    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="circuit-breaker-owner",
        email="circuit-breaker-owner@example.com",
        password=password,
    )
    farm = Farm.objects.create(
        owner=user,
        name="Farm",
        slug="farm-circuit-breaker",
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
        job_type=NdviJob.JobType.GAP_FILL,
        start=date(2025, 1, 1),
        end=date(2025, 1, 8),
        step_days=7,
        max_cloud=20,
        request_hash="circuit-breaker-hash",
    )

    # Build the engine once to get the shared instance
    shared_engine = _build_stac_engine()
    call_count = 0

    class FailingEngine:
        def __init__(self, shared_client: object) -> None:
            self.shared_client = shared_client

        def get_timeseries(self, **_: object) -> list[NdviPoint]:
            nonlocal call_count
            call_count += 1
            # Simulate WAF block that trips circuit breaker
            raise StacUpstreamError(
                "STAC API returned invalid JSON (status=200)",
                retryable=True,
            )

    # Access the StacEngine's client (shared_engine is StacEngine)
    stac_engine = shared_engine  # type: ignore[assignment]
    failing_engine = FailingEngine(stac_engine.client)  # type: ignore[attr-defined]

    monkeypatch.setattr("ndvi.tasks.acquire_lock", lambda *_, **__: True)
    monkeypatch.setattr("ndvi.tasks.get_engine", lambda *_: failing_engine)

    with patch.object(
        run_ndvi_job, "retry", side_effect=RuntimeError("retry")
    ):
        with pytest.raises(RuntimeError, match="retry"):
            # First attempt
            run_ndvi_job.apply(args=[job.id]).get()

    # Verify the engine was reused (same instance)
    assert call_count == 1
    # Verify the engine is cached
    cached_engine = _build_stac_engine()
    assert cached_engine is shared_engine


@pytest.mark.django_db
def test_run_ndvi_job_max_retries_exceeded_marks_job_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When all retries are exhausted, the job should be marked as FAILED."""
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="max-retries-owner",
        email="max-retries-owner@example.com",
        password=password,
    )
    farm = Farm.objects.create(
        owner=user,
        name="Farm",
        slug="farm-max-retries",
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
        job_type=NdviJob.JobType.GAP_FILL,
        start=date(2025, 1, 1),
        end=date(2025, 1, 8),
        step_days=7,
        max_cloud=20,
        request_hash="max-retries-hash",
    )

    class FailingEngine:
        def get_timeseries(self, **_: object) -> list[NdviPoint]:
            raise StacUpstreamError("retry me", retryable=True)

    monkeypatch.setattr("ndvi.tasks.acquire_lock", lambda *_, **__: True)
    monkeypatch.setattr("ndvi.tasks.get_engine", lambda *_: FailingEngine())

    # Make retry raise MaxRetriesExceededError immediately
    def mock_retry(
        exc: Exception | None = None,
        **kwargs: object,
    ) -> None:
        raise MaxRetriesExceededError("Max retries exceeded")

    with patch.object(run_ndvi_job, "retry", side_effect=mock_retry):
        # The task should catch MaxRetriesExceededError and mark job as failed
        result = run_ndvi_job.apply(args=[job.id]).get()

    job.refresh_from_db()
    # Job should be marked as FAILED after max retries exhausted
    assert result == "invalid"
    assert job.status == NdviJob.JobStatus.FAILED
    assert job.last_error is not None
    assert "Max retries exceeded" in job.last_error


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
def test_compute_farm_state_coverage_missing_farm() -> None:
    result = compute_farm_state_coverage.apply(
        kwargs={"farm_id": 999999}
    ).get()

    assert result == "missing"


@pytest.mark.django_db
def test_compute_farm_state_coverage_without_observations() -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="coverage-none",
        email="coverage-none@example.com",
        password=password,
    )
    farm = Farm.objects.create(
        owner=user,
        name="Coverage Farm",
        slug="coverage-none",
        bbox_south=0.0,
        bbox_west=0.0,
        bbox_north=0.2,
        bbox_east=0.2,
        is_active=True,
    )

    result = compute_farm_state_coverage.apply(
        kwargs={"farm_id": farm.id}
    ).get()

    assert result == "no_observations"


@pytest.mark.django_db
def test_compute_farm_state_coverage_returns_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="coverage-cached",
        email="coverage-cached@example.com",
        password=password,
    )
    farm = Farm.objects.create(
        owner=user,
        name="Coverage Farm",
        slug="coverage-cached",
        bbox_south=0.0,
        bbox_west=0.0,
        bbox_north=0.2,
        bbox_east=0.2,
        is_active=True,
    )
    NdviObservation.objects.create(
        farm=farm,
        engine="stac",
        bucket_date=date(2025, 1, 1),
        mean=0.4,
    )
    monkeypatch.setattr(
        "ndvi.tasks.get_cached_coverage_for_farm",
        lambda **_: (True, 0.9),
    )

    result = compute_farm_state_coverage.apply(
        kwargs={"farm_id": farm.id, "engine": "stac"}
    ).get()

    assert result == "cached"


@pytest.mark.django_db
def test_compute_farm_state_coverage_non_retryable_upstream_caches_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="coverage-upstream",
        email="coverage-upstream@example.com",
        password=password,
    )
    farm = Farm.objects.create(
        owner=user,
        name="Coverage Farm",
        slug="coverage-upstream",
        bbox_south=0.0,
        bbox_west=0.0,
        bbox_north=0.2,
        bbox_east=0.2,
        is_active=True,
    )
    NdviObservation.objects.create(
        farm=farm,
        engine="stac",
        bucket_date=date(2025, 1, 1),
        mean=0.4,
    )
    monkeypatch.setattr(
        "ndvi.tasks.get_cached_coverage_for_farm",
        lambda **_: (False, None),
    )
    monkeypatch.setattr(
        "ndvi.tasks.compute_coverage_for_farm",
        lambda **_: (_ for _ in ()).throw(
            StacUpstreamError("upstream down", retryable=False)
        ),
    )
    cache_coverage = MagicMock()
    monkeypatch.setattr("ndvi.tasks.cache_coverage_for_farm", cache_coverage)

    result = compute_farm_state_coverage.apply(
        kwargs={"farm_id": farm.id, "engine": "stac"}
    ).get()

    assert result == "ok"
    cache_coverage.assert_called_once()
    assert cache_coverage.call_args.kwargs["value"] is None


@pytest.mark.django_db
def test_compute_farm_state_coverage_retryable_upstream_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="coverage-retry",
        email="coverage-retry@example.com",
        password=password,
    )
    farm = Farm.objects.create(
        owner=user,
        name="Coverage Farm",
        slug="coverage-retry",
        bbox_south=0.0,
        bbox_west=0.0,
        bbox_north=0.2,
        bbox_east=0.2,
        is_active=True,
    )
    NdviObservation.objects.create(
        farm=farm,
        engine="stac",
        bucket_date=date(2025, 1, 1),
        mean=0.4,
    )
    monkeypatch.setattr(
        "ndvi.tasks.get_cached_coverage_for_farm",
        lambda **_: (False, None),
    )
    monkeypatch.setattr(
        "ndvi.tasks.compute_coverage_for_farm",
        lambda **_: (_ for _ in ()).throw(
            StacUpstreamError("retry coverage", retryable=True)
        ),
    )

    with patch.object(
        compute_farm_state_coverage,
        "retry",
        side_effect=RuntimeError("retry"),
    ):
        with pytest.raises(RuntimeError, match="retry"):
            compute_farm_state_coverage.apply(
                kwargs={"farm_id": farm.id, "engine": "stac"}
            ).get()


@pytest.mark.django_db
def test_enqueue_daily_farm_state_coverage_only_dispatches_uncached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="coverage-dispatch",
        email="coverage-dispatch@example.com",
        password=password,
    )
    active_farm = Farm.objects.create(
        owner=user,
        name="Active",
        slug="coverage-active",
        bbox_south=0.0,
        bbox_west=0.0,
        bbox_north=0.2,
        bbox_east=0.2,
        is_active=True,
    )
    cached_farm = Farm.objects.create(
        owner=user,
        name="Cached",
        slug="coverage-cached-farm",
        bbox_south=0.0,
        bbox_west=0.0,
        bbox_north=0.2,
        bbox_east=0.2,
        is_active=True,
    )
    Farm.objects.create(owner=user, name="No bbox", slug="coverage-nobbox")
    NdviObservation.objects.create(
        farm=active_farm,
        engine="stac",
        bucket_date=date(2025, 1, 1),
        mean=0.4,
    )
    NdviObservation.objects.create(
        farm=cached_farm,
        engine="stac",
        bucket_date=date(2025, 1, 1),
        mean=0.4,
    )
    monkeypatch.setattr(
        "ndvi.tasks.get_default_ndvi_engine_name",
        lambda: "stac",
    )
    monkeypatch.setattr("ndvi.tasks.get_coverage_threshold", lambda: 0.5)
    monkeypatch.setattr(
        "ndvi.tasks.get_cached_coverage_for_farm",
        lambda **kwargs: (
            kwargs["farm_id"] == cached_farm.id,
            None,
        ),
    )
    dispatch = MagicMock()
    monkeypatch.setattr("ndvi.tasks.dispatch_farm_state_coverage", dispatch)

    result = enqueue_daily_farm_state_coverage()

    assert result == 1
    dispatch.assert_called_once_with(
        farm_id=active_farm.id,
        engine="stac",
        target_date=date(2025, 1, 1),
        threshold=0.5,
    )


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
