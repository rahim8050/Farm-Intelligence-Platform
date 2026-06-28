"""Dead letter queue (DLQ) for spectral index jobs.

Failed jobs (after all retries) are pushed to a Redis set keyed by
``dead_letter:{queue_name}``. A periodic Celery Beat task replays
eligible jobs and monitors for stale dead letters.

Usage::

    from ndvi.queues.dead_letter import push_dead_letter, replay_dead_letters

    # After all retries exhausted
    push_dead_letter("ndvi_ingestion", job.id)

    # Scheduled every 6 hours
    replay_dead_letters.delay()
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from django.core.cache import caches

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# TTL for dead letter entries (72 hours)
DEAD_LETTER_TTL_SECONDS = 259200  # 72 hours

# Dead letter redi s key pattern
DEAD_LETTER_KEY_PATTERN = "dead_letter:{queue_name}"

# How often the replay task runs (6 hours)
REPLAY_INTERVAL_SECONDS = 21600

# Age threshold for alerting on stale dead letters (72 hours)
ALERT_AGE_THRESHOLD = timedelta(hours=72)


# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------


def _get_redis_client() -> Any:
    """Get the underlying Redis client from Django's cache backend.

    Falls back to ``None`` if the cache backend is not Redis
    (e.g. LocMemCache in tests).
    """
    cache = caches["default"]
    client = getattr(cache, "client", None)
    if client and hasattr(client, "get_client"):
        try:
            return client.get_client(write=True)
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# Dead letter queue operations
# ---------------------------------------------------------------------------


def _dead_letter_key(queue_name: str) -> str:
    """Build the Redis key for a dead letter queue."""
    return f"dead_letter:{queue_name}"


def push_dead_letter(
    queue_name: str,
    job_id: int,
    *,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Push a failed job to the dead letter queue.

    The entry is stored as a JSON blob in a Redis set with a 72-hour
    TTL. After 72 hours the entry expires automatically.

    Args:
        queue_name: Queue name (e.g. ``"ndvi_ingestion"``).
        job_id: ID of the failed job.
        metadata: Optional metadata dict (error info, timestamp, etc.).
    """
    redis_client = _get_redis_client()
    if redis_client is None:
        logger.warning(
            "Redis not available — skipping dead letter push queue=%s job=%d",
            queue_name,
            job_id,
        )
        return

    key = _dead_letter_key(queue_name)
    entry = {
        "job_id": job_id,
        "queue": queue_name,
        "failed_at": datetime.now(UTC).isoformat(),
        "metadata": metadata or {},
    }
    import json as _json

    redis_client.sadd(key, _json.dumps(entry, default=str))
    redis_client.expire(key, DEAD_LETTER_TTL_SECONDS)

    from ndvi.metrics import spectral_job_dead_letter_total

    spectral_job_dead_letter_total.labels(queue=queue_name).inc()
    logger.info("Dead letter pushed queue=%s job=%d", queue_name, job_id)


def get_dead_letters(
    queue_name: str,
) -> list[dict[str, Any]]:
    """Retrieve all dead letter entries for a queue.

    Args:
        queue_name: Queue name.

    Returns:
        List of entry dicts (each containing job_id, queue, failed_at, etc.).
    """
    redis_client = _get_redis_client()
    if redis_client is None:
        return []

    key = _dead_letter_key(queue_name)
    import json as _json

    entries: list[dict[str, Any]] = []
    for raw in redis_client.smembers(key):
        try:
            entry = _json.loads(raw)  # type: ignore[arg-type]
            entries.append(entry)
        except (_json.JSONDecodeError, TypeError):
            continue
    return entries


def remove_dead_letter(queue_name: str, job_id: int) -> None:
    """Remove a specific dead letter entry from the queue.

    Args:
        queue_name: Queue name.
        job_id: Job ID to remove.
    """
    redis_client = _get_redis_client()
    if redis_client is None:
        return

    key = _dead_letter_key(queue_name)
    import json as _json

    entries = redis_client.smembers(key)
    for raw in entries:
        try:
            entry = _json.loads(raw)  # type: ignore[arg-type]
            if entry.get("job_id") == job_id:
                redis_client.srem(key, raw)
                break
        except (_json.JSONDecodeError, TypeError):
            continue


def clear_queue(queue_name: str) -> int:
    """Clear all dead letter entries for a queue.

    Args:
        queue_name: Queue name.

    Returns:
        Number of entries removed.
    """
    redis_client = _get_redis_client()
    if redis_client is None:
        return 0

    key = _dead_letter_key(queue_name)
    count = redis_client.scard(key)
    redis_client.delete(key)
    return count


# ---------------------------------------------------------------------------
# Replay logic
# ---------------------------------------------------------------------------


def _extract_job_id(entry: dict[str, Any]) -> int | None:
    """Extract job ID from a dead letter entry dict."""
    raw = entry.get("job_id")
    if raw is not None:
        return int(raw)
    return None


def _is_stale(
    entry: dict[str, Any],
    *,
    max_age: timedelta = ALERT_AGE_THRESHOLD,
) -> bool:
    """Check if a dead letter entry is older than ``max_age``."""
    failed_at_str = entry.get("failed_at")
    if not failed_at_str:
        return False
    try:
        failed_at = datetime.fromisoformat(failed_at_str)
        if failed_at.tzinfo is None:
            failed_at = failed_at.replace(tzinfo=UTC)
        age = datetime.now(UTC) - failed_at
        return age >= max_age
    except (ValueError, TypeError):
        return False


def replay_dead_letter_entry(
    queue_name: str,
    entry: dict[str, Any],
) -> bool:
    """Re-queue a single dead letter job.

    The job is re-queued only if:
    - The original job (NdviJob) no longer exists or is still in FAILED
      state, indicating the downstream issue may have resolved.
    - The job is not older than 72 hours (stale jobs are alerted instead).

    Args:
        queue_name: Queue name.
        entry: Dead letter entry dict.

    Returns:
        ``True`` if the job was re-queued, ``False`` otherwise.
    """
    from ndvi.models import NdviJob  # noqa: PLC0415

    job_id = _extract_job_id(entry)
    if job_id is None:
        return False

    # Skip stale jobs (alerted separately)
    if _is_stale(entry):
        logger.warning(
            "Dead letter stale queue=%s job=%d — alerting",
            queue_name,
            job_id,
        )
        return False

    try:
        job = NdviJob.objects.get(id=job_id)
        if job.status == NdviJob.JobStatus.FAILED:
            # Re-queue: reset to QUEUED
            job.status = NdviJob.JobStatus.QUEUED
            job.attempts = 0
            job.last_error = None
            job.save(update_fields=["status", "attempts", "last_error"])
            remove_dead_letter(queue_name, job_id)

            from ndvi.tasks import run_ndvi_job  # noqa: PLC0415

            run_ndvi_job.delay(job_id)
            logger.info(
                "Dead letter re-queued queue=%s job=%d",
                queue_name,
                job_id,
            )
            return True
    except NdviJob.DoesNotExist:
        logger.warning(
            "Dead letter job gone queue=%s job=%d",
            queue_name,
            job_id,
        )
        remove_dead_letter(queue_name, job_id)

    return False


# ---------------------------------------------------------------------------
# Celery tasks
# ---------------------------------------------------------------------------

from celery import shared_task  # noqa: PLC0415, E402


@shared_task
def replay_dead_letters() -> dict[str, int]:
    """Replay eligible dead letter entries across all queues.

    Scheduled every 6 hours via ``CELERY_BEAT_SCHEDULE``.
    - Re-queues jobs that may succeed now (external recovery).
    - For stale jobs (> 72h), alerts via logger and keeps them in
      the DLQ for manual inspection.

    Returns:
        Dict with keys ``replayed``, ``stale``, ``failed`` counts.
    """

    # Queues to scan
    queue_names = [
        "ndvi_ingestion",
        "ndvi_recompute",
        "ndvi_analysis",
    ]

    result: dict[str, int] = {
        "replayed": 0,
        "stale": 0,
        "failed": 0,
    }

    for queue_name in queue_names:
        entries = get_dead_letters(queue_name)
        for entry in entries:
            job_id = _extract_job_id(entry)
            if job_id is None:
                result["failed"] += 1
                continue

            if _is_stale(entry):
                result["stale"] += 1
                logger.warning(
                    "Dead letter stale >72h queue=%s job=%d",
                    queue_name,
                    job_id,
                )
                continue

            try:
                replayed = replay_dead_letter_entry(queue_name, entry)
                if replayed:
                    result["replayed"] += 1
                else:
                    result["failed"] += 1
            except Exception:
                logger.exception(
                    "Dead letter replay failed queue=%s job=%d",
                    queue_name,
                    job_id,
                )
                result["failed"] += 1

    logger.info(
        "Dead letter replay complete replayed=%d stale=%d failed=%d",
        result["replayed"],
        result["stale"],
        result["failed"],
    )
    return result
