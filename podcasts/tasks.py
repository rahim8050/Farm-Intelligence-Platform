"""Podcast Celery tasks.

Worker: Celery worker.
Schedule: ``CELERY_BEAT_SCHEDULE['podcasts-refresh-feeds']`` (default
once per hour). Auth: Celery task isolation; no request context.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

from podcasts.services import (
    get_refresh_timeout_seconds,
    ingest_all_active_podcasts,
)

logger = logging.getLogger("podcasts")


@shared_task(
    bind=True,
    name="podcasts.tasks.refresh_all_feeds",
    max_retries=0,
    time_limit=900,
    soft_time_limit=850,
)
def refresh_all_feeds(self: Any) -> dict[str, int]:
    """Refresh every active podcast's RSS feed.

    Returns:
        Dict with ``refreshed`` (number of feeds that ran), ``ok``
        (number that succeeded), and ``errors`` (number that failed).
    """
    timeout = get_refresh_timeout_seconds()
    reports = ingest_all_active_podcasts(timeout_seconds=timeout)
    refreshed = len(reports)
    ok = sum(1 for r in reports if not r.error)
    errors = refreshed - ok
    logger.info(
        "podcasts_refresh_completed refreshed=%d ok=%d errors=%d",
        refreshed,
        ok,
        errors,
    )
    return {"refreshed": refreshed, "ok": ok, "errors": errors}
