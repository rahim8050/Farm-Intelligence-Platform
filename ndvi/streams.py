"""Redis Stream producer for NDVI jobs.

This module provides the producer side of the Redis Streams transport
for NDVI job dispatch. When NDVI_QUEUE_BACKEND="stream", dispatch helpers
publish to Redis streams instead of directly enqueueing to Celery.

Architecture:
    Producer (this module) → Redis Stream → Consumer (Stage 4)
    → Celery Queue → Worker

Stream payload schema includes all fields needed to reconstruct and
execute an NDVI job, including the request_hash for idempotency at
the consumer/DB level.
"""

from __future__ import annotations

import logging
import time
from datetime import date

import redis
from django.conf import settings
from django.core.cache import caches

from .models import NdviJob
from .services import get_default_colormap_normalization

logger = logging.getLogger(__name__)


def _get_stream_redis_client() -> redis.Redis:
    """Return a raw Redis client for stream operations.

    Uses the existing 'default' cache connection to avoid creating
    separate Redis connections. The django_redis client exposes
    the underlying connection pool via get_client().
    """
    cache = caches["default"]
    # django-redis specific: cache.client is the DefaultClient
    # which has get_client() to return the raw redis-py client.
    return cache.client.get_client()  # type: ignore[attr-defined]


def build_stream_payload(job: NdviJob) -> dict[str, str]:
    """Serialize an NdviJob into a stream entry payload.

    Args:
        job: The NdviJob instance to serialize.

    Returns:
        Dict containing all fields needed to reconstruct the job
        at the consumer side. All values are strings (Redis stream
        entries require string values).
    """
    colormap_norm = get_default_colormap_normalization()

    return {
        "job_id": str(job.id),
        "request_hash": job.request_hash,
        "farm_id": str(job.farm_id),
        "owner_id": str(job.owner_id),
        "engine": job.engine,
        "job_type": job.job_type,
        "index_type": job.index_type or "NDVI",
        "start": job.start.isoformat() if job.start else "",
        "end": job.end.isoformat() if job.end else "",
        "step_days": str(job.step_days) if job.step_days else "",
        "max_cloud": str(job.max_cloud) if job.max_cloud else "",
        "lookback_days": str(job.lookback_days) if job.lookback_days else "",
        "colormap_normalization": colormap_norm.value,
        "enqueue_timestamp": str(time.time()),
    }


def publish_ndvi_job(job: NdviJob) -> str:
    """Publish an NDVI job to the Redis stream.

    Args:
        job: The NdviJob instance to publish.

    Returns:
        The Redis stream entry ID (format: "<timestamp>-<sequence>").

    Raises:
        redis.ConnectionError: If Redis is unavailable.
        redis.ResponseError: If the stream operation fails.
    """
    payload = build_stream_payload(job)
    stream_name = settings.NDVI_STREAM_NAME
    maxlen = settings.NDVI_STREAM_MAXLEN

    client = _get_stream_redis_client()
    # xadd returns entry ID as string in sync mode, but mypy sees Awaitable
    entry_id: str = client.xadd(  # type: ignore[assignment]
        stream_name,
        payload,  # type: ignore[arg-type]
        maxlen=maxlen,
        approximate=True,
    )

    logger.info(
        "Published NDVI job %s to stream %s as %s",
        job.id,
        stream_name,
        entry_id,
    )
    return entry_id


def build_farm_state_coverage_payload(
    *,
    farm_id: int,
    engine: str | None,
    target_date: date,
    threshold: float,
) -> dict[str, str]:
    """Serialize farm state coverage parameters into a stream entry payload.

    Args:
        farm_id: ID of the farm.
        engine: NDVI engine name (or None for default).
        target_date: Target date for coverage computation.
        threshold: Coverage threshold for state classification.

    Returns:
        Dict containing all fields needed to reconstruct the coverage job.
    """
    from .services import get_default_ndvi_engine_name

    resolved_engine = engine or get_default_ndvi_engine_name()

    return {
        "farm_id": str(farm_id),
        "engine": resolved_engine,
        "target_date": target_date.isoformat(),
        "threshold": str(threshold),
        "job_type": "farm_state_coverage",
        "enqueue_timestamp": str(time.time()),
    }


def publish_farm_state_coverage(
    *,
    farm_id: int,
    engine: str | None = None,
    target_date: date,
    threshold: float,
) -> str:
    """Publish a farm state coverage job to the Redis stream.

    Args:
        farm_id: ID of the farm.
        engine: NDVI engine name (or None for default).
        target_date: Target date for coverage computation.
        threshold: Coverage threshold for state classification.

    Returns:
        The Redis stream entry ID (format: "<timestamp>-<sequence>").

    Raises:
        redis.ConnectionError: If Redis is unavailable.
        redis.ResponseError: If the stream operation fails.
    """
    payload = build_farm_state_coverage_payload(
        farm_id=farm_id,
        engine=engine,
        target_date=target_date,
        threshold=threshold,
    )
    stream_name = settings.NDVI_STREAM_NAME
    maxlen = settings.NDVI_STREAM_MAXLEN

    client = _get_stream_redis_client()
    # xadd returns entry ID as string in sync mode, but mypy sees Awaitable
    entry_id: str = client.xadd(  # type: ignore[assignment]
        stream_name,
        payload,  # type: ignore[arg-type]
        maxlen=maxlen,
        approximate=True,
    )

    logger.info(
        "Published farm state coverage for farm %s to stream %s as %s",
        farm_id,
        stream_name,
        entry_id,
    )
    return entry_id
