from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from celery import shared_task
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from farms.models import Farm
from ndvi.engines.sentinelhub import SentinelHubAuthError
from ndvi.farm_state import (
    cache_coverage_for_farm,
    compute_coverage_for_farm,
    get_cached_coverage_for_farm,
    get_coverage_threshold,
)
from ndvi.stac_client import StacProcessingError, StacUpstreamError

from .metrics import ndvi_jobs_total
from .models import NdviJob, NdviRasterArtifact
from .raster.service import render_ndvi_png
from .services import (
    acquire_lock,
    dispatch_farm_state_coverage,
    dispatch_ndvi_job,
    enforce_quota,
    enqueue_job,
    get_default_lookback_days,
    get_default_max_cloud,
    get_default_ndvi_engine_name,
    get_default_step_days,
    get_engine,
    get_lock_timeout_seconds,
    normalize_bbox,
    normalize_latest_params,
    normalize_timeseries_params,
    upsert_observations,
)

logger = logging.getLogger(__name__)


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def run_ndvi_job(self: Any, job_id: int) -> str:
    job = NdviJob.objects.select_related("farm", "owner").get(id=job_id)
    lock_timeout = get_lock_timeout_seconds()
    if not acquire_lock(job.request_hash, timeout=lock_timeout):
        logger.info("ndvi.lock.skipped job_id=%s", job.id)
        return "locked"

    try:
        bbox = normalize_bbox(job.farm)
        enforce_quota(job.farm, bbox)

        with transaction.atomic():
            job.mark_running(
                locked_until=timezone.now() + timedelta(seconds=lock_timeout)
            )

        if job.job_type == NdviJob.JobType.REFRESH_LATEST:
            engine = get_engine(job.engine)
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
                upsert_observations(
                    farm=job.farm, engine=job.engine, points=[point]
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
            content, content_hash = render_ndvi_png(
                farm=job.farm,
                bbox=bbox,
                day=raster_date,
                size=raster_size,
                max_cloud=max_cloud,
                engine_name=job.engine,
                job_id=job.id,
            )
            filename = (
                f"ndvi_raster_{job.farm_id}_{raster_date}_{raster_size}_"
                f"{max_cloud}_{content_hash[:8]}.png"
            )
            artifact, _ = NdviRasterArtifact.objects.update_or_create(
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
            artifact.content_hash = content_hash
            artifact.owner_id = job.owner_id
            artifact.last_error = None
            artifact.image.save(filename, ContentFile(content), save=False)
            artifact.save()
        else:
            engine = get_engine(job.engine)
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
                upsert_observations(
                    farm=job.farm, engine=job.engine, points=points
                )
        job.mark_finished(NdviJob.JobStatus.SUCCESS)
        ndvi_jobs_total.labels(
            status=NdviJob.JobStatus.SUCCESS,
            type=job.job_type,
            engine=job.engine,
        ).inc()
        return "ok"
    except SentinelHubAuthError as exc:
        logger.warning("ndvi.job.auth_failed job_id=%s err=%s", job.id, exc)
        job.mark_finished(NdviJob.JobStatus.FAILED, error=str(exc))
        ndvi_jobs_total.labels(
            status=NdviJob.JobStatus.FAILED,
            type=job.job_type,
            engine=job.engine,
        ).inc()
        return "invalid"
    except StacUpstreamError as exc:
        logger.warning("ndvi.job.stac_failed job_id=%s err=%s", job.id, exc)
        job.mark_finished(NdviJob.JobStatus.FAILED, error=str(exc))
        ndvi_jobs_total.labels(
            status=NdviJob.JobStatus.FAILED,
            type=job.job_type,
            engine=job.engine,
        ).inc()
        if exc.retryable:
            raise self.retry(exc=exc) from exc
        return "invalid"
    except StacProcessingError as exc:
        logger.warning(
            "ndvi.job.stac_processing_failed job_id=%s err=%s",
            job.id,
            exc,
        )
        job.mark_finished(NdviJob.JobStatus.FAILED, error=str(exc))
        ndvi_jobs_total.labels(
            status=NdviJob.JobStatus.FAILED,
            type=job.job_type,
            engine=job.engine,
        ).inc()
        return "invalid"
    except ValidationError as exc:
        logger.warning("ndvi.job.invalid job_id=%s err=%s", job.id, exc)
        job.mark_finished(NdviJob.JobStatus.FAILED, error=str(exc))
        ndvi_jobs_total.labels(
            status=NdviJob.JobStatus.FAILED,
            type=job.job_type,
            engine=job.engine,
        ).inc()
        return "invalid"
    except Exception as exc:  # noqa: BLE001
        logger.exception("ndvi.job.failed job_id=%s err=%s", job.id, exc)
        job.mark_finished(NdviJob.JobStatus.FAILED, error=str(exc))
        ndvi_jobs_total.labels(
            status=NdviJob.JobStatus.FAILED,
            type=job.job_type,
            engine=job.engine,
        ).inc()
        raise self.retry(exc=exc) from exc


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
    farm = Farm.objects.filter(is_active=True, id=farm_id).first()
    if farm is None:
        return "missing"
    resolved_engine = engine or get_default_ndvi_engine_name()
    resolved_threshold = (
        float(threshold) if threshold is not None else get_coverage_threshold()
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
        if exc.retryable:
            raise self.retry(exc=exc) from exc
        value = None
    cache_coverage_for_farm(
        farm_id=farm.id,
        engine=resolved_engine,
        target_date=resolved_date,
        threshold=resolved_threshold,
        value=value,
    )
    return "ok"


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
