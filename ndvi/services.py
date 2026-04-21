from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Any

from django.conf import settings
from django.core.cache import caches
from django.db import transaction
from rest_framework.exceptions import ValidationError

from farms.models import Farm

from .engines.base import BBox, NDVIEngine, NdviPoint
from .engines.sentinelhub import SentinelHubEngine
from .metrics import ndvi_cache_hit_total, ndvi_jobs_total
from .models import NdviJob, NdviObservation
from .raster.base import ColormapNormalization
from .raster.registry import resolve_raster_engine_name

logger = logging.getLogger(__name__)

SUPPORTED_ENGINES = ("sentinelhub", "stac")

DEFAULT_NDVI_ENGINE_NAME = "sentinelhub"
DEFAULT_NDVI_QUEUE_BACKEND = "celery"
DEFAULT_MAX_AREA_KM2 = 5000.0
DEFAULT_MAX_DATERANGE_DAYS = 370
DEFAULT_STEP_DAYS = 7
DEFAULT_MAX_CLOUD = 30
DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_LOCK_TIMEOUT_SECONDS = 60
DEFAULT_CACHE_TTL_TIMESERIES_SECONDS = 86400
DEFAULT_CACHE_TTL_LATEST_SECONDS = 21600
DEFAULT_COLORMAP_NORMALIZATION = "histogram"


def _get_int_setting(name: str, default: int) -> int:
    return int(getattr(settings, name, default))


def _get_float_setting(name: str, default: float) -> float:
    return float(getattr(settings, name, default))


def get_default_ndvi_engine_name() -> str:
    return str(
        getattr(settings, "NDVI_ENGINE", DEFAULT_NDVI_ENGINE_NAME)
    ).lower()


def get_ndvi_queue_backend() -> str:
    return str(
        getattr(settings, "NDVI_QUEUE_BACKEND", DEFAULT_NDVI_QUEUE_BACKEND)
    ).lower()


def get_max_area_km2() -> float:
    return _get_float_setting("NDVI_MAX_AREA_KM2", DEFAULT_MAX_AREA_KM2)


def get_max_daterange_days() -> int:
    return _get_int_setting(
        "NDVI_MAX_DATERANGE_DAYS", DEFAULT_MAX_DATERANGE_DAYS
    )


def get_default_step_days() -> int:
    return _get_int_setting("NDVI_DEFAULT_STEP_DAYS", DEFAULT_STEP_DAYS)


def get_default_max_cloud() -> int:
    return _get_int_setting("NDVI_DEFAULT_MAX_CLOUD", DEFAULT_MAX_CLOUD)


def get_default_lookback_days() -> int:
    return _get_int_setting(
        "NDVI_DEFAULT_LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS
    )


def get_default_colormap_normalization() -> ColormapNormalization:
    """Get the default colormap normalization mode from settings."""
    mode_str = str(
        getattr(
            settings,
            "NDVI_COLORMAP_NORMALIZATION",
            DEFAULT_COLORMAP_NORMALIZATION,
        )
    ).lower()
    try:
        return ColormapNormalization(mode_str)
    except ValueError:
        logger.warning(
            "Invalid NDVI_COLORMAP_NORMALIZATION '%s', "
            "using default 'histogram'",
            mode_str,
        )
        return ColormapNormalization.HISTOGRAM


def get_lock_timeout_seconds() -> int:
    return _get_int_setting(
        "NDVI_LOCK_TIMEOUT_SECONDS", DEFAULT_LOCK_TIMEOUT_SECONDS
    )


def get_cache_ttl_timeseries() -> int:
    return _get_int_setting(
        "NDVI_CACHE_TTL_TIMESERIES_SECONDS",
        DEFAULT_CACHE_TTL_TIMESERIES_SECONDS,
    )


def get_cache_ttl_latest() -> int:
    return _get_int_setting(
        "NDVI_CACHE_TTL_LATEST_SECONDS",
        DEFAULT_CACHE_TTL_LATEST_SECONDS,
    )


def _build_sentinelhub_engine() -> NDVIEngine:
    return SentinelHubEngine()


@lru_cache(maxsize=1)
def _build_stac_engine() -> NDVIEngine:
    from .engines.stac import StacEngine

    return StacEngine()


ENGINE_FACTORIES: dict[str, Callable[[], NDVIEngine]] = {
    "sentinelhub": _build_sentinelhub_engine,
    "stac": _build_stac_engine,
}


@dataclass(frozen=True)
class TimeseriesParams:
    start: date
    end: date
    step_days: int
    max_cloud: int


@dataclass(frozen=True)
class LatestParams:
    lookback_days: int
    max_cloud: int


def resolve_ndvi_engine_name(
    engine_name: str | None,
    *,
    default_engine: str | None = None,
) -> str:
    resolved = (
        engine_name
        if engine_name is not None
        else default_engine or get_default_ndvi_engine_name()
    )
    engine = str(resolved).lower()
    if engine not in SUPPORTED_ENGINES:
        raise ValueError(f"Unsupported NDVI engine: {engine}")
    return engine


def get_engine(engine_name: str | None = None) -> NDVIEngine:
    engine = resolve_ndvi_engine_name(engine_name)
    factory = ENGINE_FACTORIES.get(engine)
    if not factory:
        raise ValueError(f"Unsupported NDVI engine: {engine}")
    return factory()


def normalize_bbox(farm: Farm) -> BBox:
    if (
        farm.bbox_south is None
        or farm.bbox_west is None
        or farm.bbox_north is None
        or farm.bbox_east is None
    ):
        raise ValidationError("Farm must include a bounding box for NDVI.")
    bbox = BBox(
        south=Decimal(farm.bbox_south),
        west=Decimal(farm.bbox_west),
        north=Decimal(farm.bbox_north),
        east=Decimal(farm.bbox_east),
    )
    if bbox.west >= bbox.east or bbox.south >= bbox.north:
        raise ValidationError(
            "Farm bounding box must have west < east and south < north."
        )
    return bbox


def _approx_area_km2(bbox: BBox) -> float:
    mean_lat = (bbox.north + bbox.south) / Decimal(2)
    lat_km = (bbox.north - bbox.south) * Decimal("111.32")
    lon_km = (
        (bbox.east - bbox.west)
        * Decimal(math.cos(math.radians(float(mean_lat))))
        * Decimal("111.32")
    )
    area = abs(lat_km * lon_km)
    return float(area)


def normalize_timeseries_params(
    start: date,
    end: date,
    step_days: int | None,
    max_cloud: int | None,
    *,
    default_max_cloud: int | None = None,
) -> TimeseriesParams:
    if start > end:
        raise ValidationError("start must be on or before end.")

    delta_days = (end - start).days
    if delta_days > get_max_daterange_days():
        raise ValidationError(
            "Requested date range exceeds NDVI_MAX_DATERANGE_DAYS."
        )

    step = step_days or get_default_step_days()
    step = max(1, min(step, 30))

    cloud_default = (
        default_max_cloud
        if default_max_cloud is not None
        else get_default_max_cloud()
    )
    cloud = max_cloud if max_cloud is not None else cloud_default
    cloud = max(0, min(cloud, 100))

    return TimeseriesParams(
        start=start, end=end, step_days=step, max_cloud=cloud
    )


def normalize_latest_params(
    lookback_days: int | None,
    max_cloud: int | None,
    *,
    default_max_cloud: int | None = None,
) -> LatestParams:
    lookback = lookback_days or get_default_lookback_days()
    lookback = max(1, min(lookback, get_max_daterange_days()))

    cloud_default = (
        default_max_cloud
        if default_max_cloud is not None
        else get_default_max_cloud()
    )
    cloud = max_cloud if max_cloud is not None else cloud_default
    cloud = max(0, min(cloud, 100))

    return LatestParams(lookback_days=lookback, max_cloud=cloud)


def hash_request(
    *,
    engine: str,
    owner_id: int,
    farm_id: int,
    params: dict[str, Any],
) -> str:
    normalized = json.dumps(
        {
            "engine": engine,
            "owner": owner_id,
            "farm": farm_id,
            "params": params,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def expected_buckets(start: date, end: date, step_days: int) -> list[date]:
    buckets: list[date] = []
    cursor = start
    while cursor <= end:
        buckets.append(cursor)
        cursor = cursor + timedelta(days=step_days)
    return buckets


def detect_gaps(
    existing_dates: set[date], expected: Iterable[date]
) -> list[date]:
    missing: list[date] = []
    for bucket in expected:
        if bucket not in existing_dates:
            missing.append(bucket)
    return missing


def acquire_lock(request_hash: str, *, timeout: int | None = None) -> bool:
    ttl = timeout or get_lock_timeout_seconds()
    cache = caches["default"]
    key = f"ndvi:lock:{request_hash}"
    acquired = cache.add(key, "1", ttl)
    return bool(acquired)


def release_lock(request_hash: str) -> None:
    cache = caches["default"]
    key = f"ndvi:lock:{request_hash}"
    cache.delete(key)


def cache_timeseries_response(
    owner_id: int,
    farm_id: int,
    engine: str,
    params: TimeseriesParams,
    payload: dict[str, Any],
) -> None:
    cache = caches["default"]
    key = (
        f"ndvi:cache:ts:{owner_id}:{farm_id}:{engine}:"
        f"{params.start}:{params.end}:{params.step_days}:{params.max_cloud}"
    )
    cache.set(key, payload, get_cache_ttl_timeseries())


def get_cached_timeseries_response(
    owner_id: int,
    farm_id: int,
    engine: str,
    params: TimeseriesParams,
) -> dict[str, Any] | None:
    cache = caches["default"]
    key = (
        f"ndvi:cache:ts:{owner_id}:{farm_id}:{engine}:"
        f"{params.start}:{params.end}:{params.step_days}:{params.max_cloud}"
    )
    cached = cache.get(key)
    if cached:
        ndvi_cache_hit_total.labels(layer="timeseries").inc()
    return cached


def cache_latest_response(
    owner_id: int,
    farm_id: int,
    engine: str,
    params: LatestParams,
    payload: dict[str, Any],
) -> None:
    cache = caches["default"]
    key = (
        "ndvi:cache:latest:"
        f"{owner_id}:{farm_id}:{engine}:"
        f"{params.lookback_days}:{params.max_cloud}"
    )
    cache.set(key, payload, get_cache_ttl_latest())


def get_cached_latest_response(
    owner_id: int,
    farm_id: int,
    engine: str,
    params: LatestParams,
) -> dict[str, Any] | None:
    cache = caches["default"]
    key = (
        "ndvi:cache:latest:"
        f"{owner_id}:{farm_id}:{engine}:"
        f"{params.lookback_days}:{params.max_cloud}"
    )
    cached = cache.get(key)
    if cached:
        ndvi_cache_hit_total.labels(layer="latest").inc()
    return cached


def enforce_quota(farm: Farm, bbox: BBox) -> None:
    area_km2 = _approx_area_km2(bbox)
    if area_km2 > get_max_area_km2():
        raise ValidationError("Requested area exceeds NDVI_MAX_AREA_KM2.")


def upsert_observations(
    *,
    farm: Farm,
    engine: str,
    points: Iterable[NdviPoint],
) -> list[NdviObservation]:
    saved: list[NdviObservation] = []
    with transaction.atomic():
        for point in points:
            obj, _ = NdviObservation.objects.update_or_create(
                farm=farm,
                engine=engine,
                bucket_date=point.date,
                defaults={
                    "mean": point.mean,
                    "min": point.min,
                    "max": point.max,
                    "sample_count": point.sample_count,
                    "cloud_fraction": point.cloud_fraction,
                },
            )
            saved.append(obj)
    return saved


def enqueue_job(
    *,
    owner_id: int,
    farm: Farm,
    engine_name: str | None = None,
    job_type: str,
    params: dict[str, Any],
) -> NdviJob:
    if job_type == NdviJob.JobType.RASTER_PNG:
        metrics_engine = resolve_ndvi_engine_name(None)
        resolved_engine = resolve_raster_engine_name(engine_name)
        raster_engine = resolved_engine
    else:
        resolved_engine = resolve_ndvi_engine_name(engine_name)
        metrics_engine = resolved_engine
        raster_engine = "-"
    engine_source = "override" if engine_name is not None else "default"
    request_hash = hash_request(
        engine=resolved_engine,
        owner_id=owner_id,
        farm_id=farm.id,
        params=params,
    )
    existing = NdviJob.objects.filter(
        owner_id=owner_id,
        farm=farm,
        engine=resolved_engine,
        request_hash=request_hash,
        status__in=[NdviJob.JobStatus.QUEUED, NdviJob.JobStatus.RUNNING],
    ).first()
    if existing:
        return existing

    job = NdviJob.objects.create(
        owner_id=owner_id,
        farm=farm,
        engine=resolved_engine,
        job_type=job_type,
        request_hash=request_hash,
        status=NdviJob.JobStatus.QUEUED,
        start=params.get("start"),
        end=params.get("end"),
        step_days=params.get("step_days"),
        max_cloud=params.get("max_cloud"),
        lookback_days=params.get("lookback_days"),
    )
    ndvi_jobs_total.labels(
        status=job.status, type=job_type, engine=resolved_engine
    ).inc()
    logger.info(
        "ndvi.job.enqueued job_id=%s type=%s metrics_engine=%s "
        "raster_engine=%s engine_source=%s",
        job.id,
        job_type,
        metrics_engine,
        raster_engine,
        engine_source,
    )
    return job


def dispatch_ndvi_job(job: NdviJob | int) -> None:
    """Dispatch an NDVI job to the configured queue backend.

    When NDVI_QUEUE_BACKEND="stream", publishes to Redis stream.
    When NDVI_QUEUE_BACKEND="celery" (default), enqueues directly to Celery.

    Args:
        job: NdviJob instance or job ID to dispatch.

    Raises:
        redis.ConnectionError: If stream backend and Redis is unavailable.
    """
    backend = get_ndvi_queue_backend()

    if backend == "stream":
        from .streams import publish_ndvi_job

        job_obj = (
            job
            if isinstance(job, NdviJob)
            else NdviJob.objects.select_related("farm", "owner").get(id=job)
        )
        publish_ndvi_job(job_obj)
        return

    # Celery backend (default)
    from .tasks import run_ndvi_job

    job_id = job.id if isinstance(job, NdviJob) else int(job)
    run_ndvi_job.delay(job_id)


def dispatch_farm_state_coverage(
    *,
    farm_id: int,
    engine: str | None = None,
    target_date: date,
    threshold: float,
) -> None:
    """Dispatch a farm state coverage job to the configured queue backend.

    When NDVI_QUEUE_BACKEND="stream", publishes to Redis stream.
    When NDVI_QUEUE_BACKEND="celery" (default), enqueues directly to Celery.

    Args:
        farm_id: ID of the farm to compute coverage for.
        engine: NDVI engine to use (default from settings).
        target_date: Target date for coverage computation.
        threshold: Coverage threshold for state classification.

    Raises:
        redis.ConnectionError: If stream backend and Redis is unavailable.
    """
    backend = get_ndvi_queue_backend()

    if backend == "stream":
        from .streams import publish_farm_state_coverage as _publish_coverage

        _publish_coverage(
            farm_id=farm_id,
            engine=engine,
            target_date=target_date,
            threshold=threshold,
        )
        return

    # Celery backend (default)
    from .tasks import compute_farm_state_coverage

    compute_farm_state_coverage.delay(
        farm_id=farm_id,
        engine=engine,
        target_date=target_date.isoformat(),
        threshold=threshold,
    )


def is_stale(observation: NdviObservation | None, lookback_days: int) -> bool:
    if observation is None:
        return True
    today = date.today()
    return (today - observation.bucket_date).days > lookback_days
