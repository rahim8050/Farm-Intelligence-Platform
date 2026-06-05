"""Podcast Celery tasks.

Per ``prompts/p4-staff-engineer-review.md`` #3 the refresh is
parallelised: a beat task (:func:`dispatch_refresh_batch`) fans out
one :func:`refresh_one_podcast` task per active show, and a
:func:`summarise_refresh_run` chord callback aggregates the
results. The per-feed task honours the per-podcast
``next_retry_at`` backoff column, so a misbehaving upstream cannot
stall the rest of the catalogue.

The legacy :func:`refresh_all_feeds` task is kept for backwards
compatibility (older beat schedules) and as a synchronous
fallback for one-off management commands.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from celery import chord, shared_task
from django.db.models import Q
from django.utils import timezone as django_timezone

from podcasts.services import (
    get_refresh_timeout_seconds,
    ingest_all_active_podcasts,
    ingest_podcast,
)

logger = logging.getLogger("podcasts")

# Soft cap on the size of a single fan-out batch. With more feeds
# than this the dispatcher will still schedule them all, but the
# per-batch work is bounded so a single worker is never asked to
# juggle more than this many tasks at once.
_MAX_BATCH_SIZE = 200


@shared_task(
    bind=True,
    name="podcasts.tasks.refresh_all_feeds",
    max_retries=0,
    time_limit=900,
    soft_time_limit=850,
)
def refresh_all_feeds(self: Any) -> dict[str, int]:
    """Refresh every active podcast's RSS feed (synchronous fallback).

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


def _due_podcast_ids() -> list[str]:
    """Return the ids of active podcasts whose ``next_retry_at`` is due.

    A podcast with ``next_retry_at`` in the future is skipped; a
    podcast with ``next_retry_at`` in the past (or null) is
    eligible. The list is ordered by ``id`` so the fan-out is
    deterministic across runs.
    """
    from podcasts.models import Podcast

    now = django_timezone.now()
    qs = (
        Podcast.objects.filter(is_active=True)
        .filter(Q(next_retry_at__isnull=True) | Q(next_retry_at__lte=now))
        .order_by("id")
        .values_list("id", flat=True)
    )
    return list(qs[:_MAX_BATCH_SIZE])


@shared_task(
    bind=True,
    name="podcasts.tasks.dispatch_refresh_batch",
    ignore_result=True,
)
def dispatch_refresh_batch(self: Any) -> dict[str, int]:
    """Beat entry point: fan out one task per due podcast.

    Returns a small dict for visibility in beat / flower output.
    """
    from podcasts import metrics

    ids = _due_podcast_ids()
    if not ids:
        metrics.set_refresh_stale(0)
        return {"dispatched": 0, "remaining": 0}
    header = [refresh_one_podcast.s(pid) for pid in ids]
    callback = summarise_refresh_run.s()
    chord(header)(callback)
    return {"dispatched": len(ids)}


@shared_task(
    bind=True,
    name="podcasts.tasks.refresh_one_podcast",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=2,
    time_limit=120,
    soft_time_limit=110,
    ignore_result=False,
)
def refresh_one_podcast(self: Any, podcast_id: str) -> dict[str, Any]:
    """Refresh a single podcast's RSS feed.

    The body delegates to :func:`podcasts.services.ingest_podcast`
    which already handles per-feed backoff / error recording. The
    per-task metrics are recorded here so a single
    ``podcasts_refresh_total{result}`` / histogram observation
    matches the worker-level task lifecycle.

    A retry is only triggered for unexpected exceptions (network
    blip, OOM, ...). :class:`podcasts.services.PodcastIngestionError`
    is *not* retried because ``ingest_podcast`` already records
    the failure and the next batch will pick the feed up after
    the backoff window expires.
    """
    from podcasts import metrics
    from podcasts.models import Podcast

    timeout = get_refresh_timeout_seconds()
    try:
        podcast = Podcast.objects.get(id=podcast_id, is_active=True)
    except Podcast.DoesNotExist:
        metrics.refresh_total(result="skipped")
        return {
            "podcast_id": podcast_id,
            "result": "skipped",
            "reason": "inactive_or_missing",
        }
    with metrics.refresh_timer(result="ok"):
        report = ingest_podcast(podcast, timeout_seconds=timeout)
    result = "error" if report.error else "ok"
    metrics.refresh_total(result=result)
    return {
        "podcast_id": podcast_id,
        "result": result,
        "episodes_seen": report.episodes_seen,
        "episodes_created": report.episodes_created,
        "episodes_updated": report.episodes_updated,
        "error": report.error,
    }


@shared_task(
    name="podcasts.tasks.summarise_refresh_run",
    ignore_result=True,
)
def summarise_refresh_run(reports: list[dict[str, Any]]) -> dict[str, int]:
    """Chord callback: aggregate per-feed results into a summary.

    Also publishes the ``podcasts_refresh_stale`` gauge so the
    SLO rule ``PodcastsRefreshStale`` in
    ``monitoring/prometheus/alerts.yml`` can fire on a real
    signal.
    """
    from django.utils import timezone as django_timezone

    from podcasts import metrics
    from podcasts.models import Podcast

    total = len(reports)
    ok = sum(1 for r in reports if r.get("result") == "ok")
    errors = sum(1 for r in reports if r.get("result") == "error")
    skipped = sum(1 for r in reports if r.get("result") == "skipped")

    # A podcast is "stale" if it has been active for at least 2
    # beat cycles (2h) without a successful refresh.
    threshold = django_timezone.now() - timedelta(hours=2)
    stale = Podcast.objects.filter(
        is_active=True,
        last_refresh_status="error",
        last_refreshed_at__lte=threshold,
    ).count()
    metrics.set_refresh_stale(stale)

    logger.info(
        "podcasts_refresh_run_complete total=%d ok=%d errors=%d "
        "skipped=%d stale=%d",
        total,
        ok,
        errors,
        skipped,
        stale,
    )
    return {
        "total": total,
        "ok": ok,
        "errors": errors,
        "skipped": skipped,
        "stale": stale,
    }
