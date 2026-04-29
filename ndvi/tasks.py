from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any, TypeVar

from celery import shared_task
from celery.exceptions import (  # type: ignore[import-untyped]
    MaxRetriesExceededError,
)
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import OperationalError, close_old_connections, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from farms.models import Farm
from ndvi.engines.sentinelhub import SentinelHubAuthError
from ndvi.farm_state import (
    cache_coverage_for_farm,
    compute_coverage_for_farm,
    get_cached_coverage_for_farm,
    get_coverage_threshold,
    invalidate_farm_state_cache,
)
from ndvi.raster.sentinelhub_engine import SentinelHubRasterError
from ndvi.retry_policy import RetryDecision, should_retry
from ndvi.stac_client import (
    StacProcessingError,
    StacUpstreamError,
    StacWafBlockedError,
)

from .metrics import ndvi_jobs_total, ndvi_task_runtime_seconds
from .models import NdviJob, NdviRasterArtifact
from .raster.service import render_ndvi_png
from .services import (
    acquire_lock,
    dispatch_farm_state_coverage,
    dispatch_ndvi_job,
    enforce_quota,
    enqueue_job,
    get_default_colormap_normalization,
    get_default_lookback_days,
    get_default_max_cloud,
    get_default_ndvi_engine_name,
    get_default_step_days,
    get_engine,
    get_lock_timeout_seconds,
    normalize_bbox,
    normalize_latest_params,
    normalize_timeseries_params,
    release_lock,
    upsert_observations,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _with_fresh_connection(func: Callable[[], T]) -> T:
    """Run a DB-bound callback after refreshing stale Django connections.

    Celery workers are long-lived, so a MySQL connection can go stale while
    the task spends time in upstream NDVI processing. Refresh before the write
    path, and retry once if the first DB operation still hits a dead socket.
    """
    close_old_connections()
    try:
        return func()
    except OperationalError:
        close_old_connections()
        return func()


def _safe_error_message(status_error: Exception | str) -> str:
    """Return a non-sensitive error code for persistence/user flows."""
    if isinstance(status_error, str):
        safe_string_codes = {
            "auth_failed",
            "waf_blocked",
            "upstream_error",
            "processing_error",
            "raster_error",
            "validation_error",
            "max_retries_exceeded",
            "no_items",
            "no_best_item",
            "missing_assets",
        }
        if status_error in safe_string_codes:
            return status_error
        return "internal_error"
    if isinstance(status_error, SentinelHubAuthError):
        return "auth_failed"
    if isinstance(status_error, StacWafBlockedError):
        return "waf_blocked"
    if isinstance(status_error, StacUpstreamError):
        return "upstream_error"
    if isinstance(status_error, StacProcessingError):
        return "processing_error"
    if isinstance(status_error, SentinelHubRasterError):
        return "raster_error"
    if isinstance(status_error, ValidationError):
        return "validation_error"
    return "internal_error"


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


def _mark_job_failed(job: NdviJob, status_error: Exception | str) -> None:
    """Persist a failed job state with the provided error message."""
    _with_fresh_connection(
        lambda: job.mark_finished(
            NdviJob.JobStatus.FAILED,
            error=_safe_error_message(status_error),
        )
    )


def _handle_retryable_task_failure(
    *,
    self: Any,
    job: NdviJob,
    exc: Exception,
    log_prefix: str,
) -> str:
    """Apply the shared retry policy for a task failure."""

    decision: RetryDecision = should_retry(exc)
    logger.info(
        "%s retry_decision retry=%s delay=%s reason=%s job_id=%s err=%s",
        log_prefix,
        decision.retry,
        decision.delay,
        decision.reason,
        job.id,
        exc,
    )
    if decision.retry:
        retry_kwargs: dict[str, Any] = {}
        if decision.delay is not None:
            retry_kwargs["countdown"] = decision.delay
        try:
            raise self.retry(exc=exc, **retry_kwargs) from exc
        except MaxRetriesExceededError as retry_exc:
            logger.error(
                "%s max_retries_exceeded job_id=%s err=%s",
                log_prefix,
                job.id,
                retry_exc,
            )
            _mark_job_failed(job, "max_retries_exceeded")
            ndvi_jobs_total.labels(
                status=NdviJob.JobStatus.FAILED,
                type=job.job_type,
                engine=job.engine,
            ).inc()
            return "invalid"
    _mark_job_failed(job, exc)
    ndvi_jobs_total.labels(
        status=NdviJob.JobStatus.FAILED,
        type=job.job_type,
        engine=job.engine,
    ).inc()
    return "invalid"


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def run_ndvi_job(self: Any, job_id: int) -> str:
    job = NdviJob.objects.select_related("farm", "owner").get(id=job_id)
    # Refresh to catch status changes from concurrent task execution
    job.refresh_from_db()
    started_at = time.monotonic()
    task_engine = job.engine
    lock_timeout = get_lock_timeout_seconds()
    lock_key = f"{job.id}:{job.request_hash}"
    lock_token: str | None = None
    try:
        if job.status == NdviJob.JobStatus.SUCCESS:
            logger.info("ndvi.job.already_successful job_id=%s", job.id)
            return "ok"
        lock_token = acquire_lock(lock_key, timeout=lock_timeout)
        if not lock_token:
            logger.info(
                "ndvi.lock.skipped job_id=%s lock_key=%s", job.id, lock_key
            )
            return "locked"

        # Check success again after locking
        job.refresh_from_db()
        if job.status == NdviJob.JobStatus.SUCCESS:
            logger.info(
                "ndvi.job.already_successful_post_lock job_id=%s", job.id
            )
            return "ok"
        bbox = normalize_bbox(job.farm)
        enforce_quota(job.farm, bbox)

        with transaction.atomic():
            job.mark_running(
                locked_until=timezone.now() + timedelta(seconds=lock_timeout)
            )

        if job.job_type == NdviJob.JobType.REFRESH_LATEST:
            engine = get_engine(job.engine)
            task_engine = job.engine
            latest_params = normalize_latest_params(
                lookback_days=job.lookback_days or get_default_lookback_days(),
                max_cloud=job.max_cloud or get_default_max_cloud(),
            )
            point = engine.get_latest(
                bbox=bbox,
                lookback_days=latest_params.lookback_days,
                max_cloud=latest_params.max_cloud,
            )
            if point:
                _with_fresh_connection(
                    lambda: upsert_observations(
                        farm=job.farm,
                        engine=job.engine,
                        max_cloud=latest_params.max_cloud,
                        points=[point],
                    )
                )
        elif job.job_type == NdviJob.JobType.RASTER_PNG:
            raster_date = job.start or job.end or date.today()
            default_size = int(
                getattr(settings, "NDVI_RASTER_DEFAULT_SIZE", 512)
            )
            raster_size = job.step_days or default_size
            size_max = int(getattr(settings, "NDVI_RASTER_MAX_SIZE", 1024))
            if raster_size < 128 or raster_size > size_max:
                raise ValidationError(
                    f"Raster size must be between 128 and {size_max}."
                )
            if raster_size * raster_size > 1024 * 1024:
                raise ValidationError("Raster size exceeds pixel limit.")
            max_cloud = job.max_cloud or get_default_max_cloud()
            colormap_norm = get_default_colormap_normalization()
            content, content_hash = render_ndvi_png(
                farm=job.farm,
                bbox=bbox,
                day=raster_date,
                size=raster_size,
                max_cloud=max_cloud,
                engine_name=job.engine,
                job_id=job.id,
                colormap_normalization=colormap_norm,
            )
            filename = (
                f"ndvi_raster_{job.farm_id}_{raster_date}_{raster_size}_"
                f"{max_cloud}_{content_hash[:8]}.png"
            )
            artifact, _ = _with_fresh_connection(
                lambda: NdviRasterArtifact.objects.update_or_create(
                    farm=job.farm,
                    engine=job.engine,
                    date=raster_date,
                    size=raster_size,
                    max_cloud=max_cloud,
                    defaults={
                        "owner_id": job.owner_id,
                        "content_hash": content_hash,
                    },
                )
            )
            artifact.content_hash = content_hash
            artifact.owner_id = job.owner_id
            artifact.last_error = None
            artifact.image.save(filename, ContentFile(content), save=False)
            logger.info(
                "NDVI raster saved at path=%s",
                artifact.image.path or artifact.image.name,
            )
            _with_fresh_connection(lambda: artifact.save())
        else:
            engine = get_engine(job.engine)
            task_engine = job.engine
            timeseries_params = normalize_timeseries_params(
                start=job.start
                or date.today() - timedelta(days=get_default_step_days()),
                end=job.end or date.today(),
                step_days=job.step_days or get_default_step_days(),
                max_cloud=job.max_cloud or get_default_max_cloud(),
            )
            points = engine.get_timeseries(
                bbox=bbox,
                start=timeseries_params.start,
                end=timeseries_params.end,
                step_days=timeseries_params.step_days,
                max_cloud=timeseries_params.max_cloud,
            )
            if points:
                _with_fresh_connection(
                    lambda: upsert_observations(
                        farm=job.farm,
                        engine=job.engine,
                        max_cloud=timeseries_params.max_cloud,
                        points=points,
                    )
                )
        _with_fresh_connection(
            lambda: job.mark_finished(NdviJob.JobStatus.SUCCESS)
        )
        logger.info(
            "NDVI raster job completed successfully for farm_id=%s",
            job.farm_id,
        )
        ndvi_jobs_total.labels(
            status=NdviJob.JobStatus.SUCCESS,
            type=job.job_type,
            engine=job.engine,
        ).inc()
        return "ok"
    except SentinelHubAuthError as exc:
        logger.warning("ndvi.job.auth_failed job_id=%s err=%s", job.id, exc)
        job.mark_finished(
            NdviJob.JobStatus.FAILED, error=_safe_error_message(exc)
        )
        ndvi_jobs_total.labels(
            status=NdviJob.JobStatus.FAILED,
            type=job.job_type,
            engine=job.engine,
        ).inc()
        return "invalid"
    except StacWafBlockedError as exc:
        logger.error(
            "ndvi.job.waf_blocked job_id=%s support_id=%s err=%s",
            job.id,
            exc.support_id,
            exc,
        )
        return _handle_retryable_task_failure(
            self=self,
            job=job,
            exc=exc,
            log_prefix="ndvi.job",
        )
    except StacUpstreamError as exc:
        return _handle_retryable_task_failure(
            self=self,
            job=job,
            exc=exc,
            log_prefix="ndvi.job",
        )
    except StacProcessingError as exc:
        logger.warning(
            "ndvi.job.stac_processing_failed job_id=%s err=%s",
            job.id,
            exc,
        )
        logger.exception(
            "NDVI raster job failed for farm_id=%s",
            job.farm_id,
        )
        return _handle_retryable_task_failure(
            self=self,
            job=job,
            exc=exc,
            log_prefix="ndvi.job",
        )
    except SentinelHubRasterError as exc:
        logger.warning("ndvi.job.raster_failed job_id=%s err=%s", job.id, exc)
        logger.exception(
            "NDVI raster job failed for farm_id=%s",
            job.farm_id,
        )
        return _handle_retryable_task_failure(
            self=self,
            job=job,
            exc=exc,
            log_prefix="ndvi.job",
        )
    except ValidationError as exc:
        logger.warning("ndvi.job.invalid job_id=%s err=%s", job.id, exc)
        logger.exception(
            "NDVI raster job failed for farm_id=%s",
            job.farm_id,
        )
        return _handle_retryable_task_failure(
            self=self,
            job=job,
            exc=exc,
            log_prefix="ndvi.job",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("ndvi.job.failed job_id=%s err=%s", job.id, exc)
        logger.exception(
            "NDVI raster job failed for farm_id=%s",
            job.farm_id,
        )
        _mark_job_failed(job, exc)
        ndvi_jobs_total.labels(
            status=NdviJob.JobStatus.FAILED,
            type=job.job_type,
            engine=job.engine,
        ).inc()
        raise
    finally:
        if lock_token:
            release_lock(lock_key, lock_token)
        ndvi_task_runtime_seconds.labels(
            task="run_ndvi_job",
            engine=task_engine,
        ).observe(max(time.monotonic() - started_at, 0.0))
        ndvi_task_runtime_seconds.labels(
            task="run_ndvi_job",
            engine=job.engine,
        ).observe(max(time.monotonic() - started_at, 0.0))


@shared_task
def enqueue_daily_refresh() -> int:
    count = 0
    for farm in Farm.objects.filter(is_active=True):
        if (
            farm.bbox_south is None
            or farm.bbox_west is None
            or farm.bbox_north is None
            or farm.bbox_east is None
        ):
            continue
        job = enqueue_job(
            owner_id=farm.owner_id,
            farm=farm,
            engine_name=None,
            job_type=NdviJob.JobType.REFRESH_LATEST,
            params={
                "lookback_days": get_default_lookback_days(),
                "max_cloud": get_default_max_cloud(),
            },
        )
        dispatch_ndvi_job(job)
        count += 1
    return count


@shared_task(bind=True, max_retries=2, default_retry_delay=120)
def compute_farm_state_coverage(
    self: Any,
    *,
    farm_id: int,
    engine: str | None = None,
    target_date: str | None = None,
    threshold: float | None = None,
) -> str:
    resolved_engine = engine or get_default_ndvi_engine_name()
    started_at = time.monotonic()
    try:
        farm = Farm.objects.filter(is_active=True, id=farm_id).first()
        if farm is None:
            return "missing"
        resolved_threshold = (
            float(threshold)
            if threshold is not None
            else get_coverage_threshold()
        )
        resolved_date = _parse_date(target_date)
        if resolved_date is None:
            latest = (
                farm.ndvi_observations.filter(engine=resolved_engine)
                .order_by("-bucket_date")
                .first()
            )
            if not latest:
                return "no_observations"
            resolved_date = latest.bucket_date

        cached, _ = get_cached_coverage_for_farm(
            farm_id=farm.id,
            engine=resolved_engine,
            target_date=resolved_date,
            threshold=resolved_threshold,
        )
        if cached:
            return "cached"

        try:
            value = compute_coverage_for_farm(
                farm=farm,
                engine=resolved_engine,
                target_date=resolved_date,
                threshold=resolved_threshold,
            )
        except StacUpstreamError as exc:
            decision = should_retry(exc)
            logger.info(
                "ndvi.coverage retry_decision retry=%s delay=%s reason=%s "
                "farm_id=%s err=%s",
                decision.retry,
                decision.delay,
                decision.reason,
                farm.id,
                exc,
            )
            if decision.retry:
                retry_kwargs: dict[str, Any] = {}
                if decision.delay is not None:
                    retry_kwargs["countdown"] = decision.delay
                raise self.retry(exc=exc, **retry_kwargs) from exc
            value = None
        cache_coverage_for_farm(
            farm_id=farm.id,
            engine=resolved_engine,
            target_date=resolved_date,
            threshold=resolved_threshold,
            value=value,
        )
        invalidate_farm_state_cache(farm_id=farm.id, engine=resolved_engine)
        return "ok"
    finally:
        ndvi_task_runtime_seconds.labels(
            task="compute_farm_state_coverage",
            engine=resolved_engine,
        ).observe(max(time.monotonic() - started_at, 0.0))


@shared_task
def enqueue_daily_farm_state_coverage() -> int:
    count = 0
    engine = get_default_ndvi_engine_name()
    threshold = get_coverage_threshold()
    for farm in Farm.objects.filter(is_active=True):
        if (
            farm.bbox_south is None
            or farm.bbox_west is None
            or farm.bbox_north is None
            or farm.bbox_east is None
        ):
            continue
        latest = (
            farm.ndvi_observations.filter(engine=engine)
            .order_by("-bucket_date")
            .first()
        )
        if not latest:
            continue
        cached, _ = get_cached_coverage_for_farm(
            farm_id=farm.id,
            engine=engine,
            target_date=latest.bucket_date,
            threshold=threshold,
        )
        if cached:
            continue
        dispatch_farm_state_coverage(
            farm_id=farm.id,
            engine=engine,
            target_date=latest.bucket_date,
            threshold=threshold,
        )
        count += 1
    return count


@shared_task
def enqueue_weekly_gap_fill() -> int:
    count = 0
    end = date.today()
    start = end - timedelta(days=120)
    for farm in Farm.objects.filter(is_active=True):
        if (
            farm.bbox_south is None
            or farm.bbox_west is None
            or farm.bbox_north is None
            or farm.bbox_east is None
        ):
            continue
        job = enqueue_job(
            owner_id=farm.owner_id,
            farm=farm,
            engine_name=None,
            job_type=NdviJob.JobType.GAP_FILL,
            params={
                "start": start,
                "end": end,
                "step_days": 7,
                "max_cloud": get_default_max_cloud(),
            },
        )
        dispatch_ndvi_job(job)
        count += 1
    return count
