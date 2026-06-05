"""Tests for the parallel podcast ingestion (chord + per-feed backoff).

Per ``prompts/p4-staff-engineer-review.md`` #3 the periodic feed
refresh is split into one Celery task per active show, and a
misbehaving feed must not stall the rest of the catalogue. A
single backoff column (``next_retry_at``) with the schedule
1m -> 5m -> 1h -> 24h is enough to keep the catalogue healthy.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone as django_timezone

from podcasts.models import Podcast
from podcasts.services import (
    _BACKOFF_SECONDS,
    _backoff_next_attempt,
    clear_backoff,
    ingest_podcast,
)
from podcasts.tasks import (
    _MAX_BATCH_SIZE,
    _due_podcast_ids,
    dispatch_refresh_batch,
    refresh_one_podcast,
    summarise_refresh_run,
)

pytestmark = pytest.mark.django_db


def _make_podcast(id_: str = "p1", **kwargs: Any) -> Podcast:
    defaults: dict[str, Any] = {
        "id": id_,
        "title": f"Show {id_}",
        "feed_url": f"https://example.test/{id_}.xml",
    }
    defaults.update(kwargs)
    return Podcast.objects.create(**defaults)


def test_backoff_next_attempt_follows_schedule() -> None:
    base = django_timezone.now()
    for n in (1, 2, 3, 4, 5, 6):
        ts = _backoff_next_attempt(n)
        delay = (ts - base).total_seconds()
        expected = _BACKOFF_SECONDS[min(n - 1, len(_BACKOFF_SECONDS) - 1)]
        # Within 1s of expected (the function calls ``now()`` itself,
        # so the absolute value drifts by the test runtime).
        assert abs(delay - expected) < 1.0, (
            f"n={n} expected={expected}s got={delay:.2f}s"
        )


def test_clear_backoff_is_idempotent() -> None:
    p = _make_podcast()
    p.consecutive_failures = 3
    p.next_retry_at = django_timezone.now() + timedelta(minutes=5)
    p.save()
    clear_backoff(p)
    p.refresh_from_db()
    assert p.consecutive_failures == 0
    assert p.next_retry_at is None
    # Calling again is a no-op (does not raise / does not write).
    clear_backoff(p)
    p.refresh_from_db()
    assert p.consecutive_failures == 0


def test_ingest_podcast_failure_sets_backoff() -> None:
    p = _make_podcast()
    # The service catches Exception and returns a failure report,
    # so call it directly and inspect the resulting state.
    with patch(
        "podcasts.services.fetch_feed",
        side_effect=Exception("boom"),
    ):
        report = ingest_podcast(p, timeout_seconds=1.0)
    assert report.error == "unexpected: Exception"
    p.refresh_from_db()
    assert p.consecutive_failures == 1
    assert p.next_retry_at is not None
    assert p.last_refresh_status == "error"


def test_ingest_podcast_success_clears_backoff() -> None:
    p = _make_podcast()
    p.consecutive_failures = 2
    p.next_retry_at = django_timezone.now() + timedelta(minutes=5)
    p.save()
    parsed = MagicMock()
    parsed.entries = []
    with (
        patch(
            "podcasts.services.fetch_feed",
            return_value=b"<rss/>",
        ),
        patch(
            "podcasts.services.parse_feed_bytes",
            return_value=parsed,
        ),
    ):
        ingest_podcast(p, timeout_seconds=1.0)
    p.refresh_from_db()
    assert p.consecutive_failures == 0
    assert p.next_retry_at is None
    assert p.last_refresh_status == "ok"


def test_due_podcast_ids_filters_by_next_retry_at() -> None:
    now = django_timezone.now()
    _make_podcast(id_="due")
    _make_podcast(id_="future", next_retry_at=now + timedelta(hours=1))
    _make_podcast(id_="never", next_retry_at=None)
    ids = set(_due_podcast_ids())
    assert "due" in ids
    assert "never" in ids
    assert "future" not in ids
    # Sanity: at most _MAX_BATCH_SIZE ids.
    assert len(ids) <= _MAX_BATCH_SIZE


def test_due_podcast_ids_skips_inactive() -> None:
    _make_podcast(id_="active", is_active=True)
    _make_podcast(id_="inactive", is_active=False)
    ids = set(_due_podcast_ids())
    assert "active" in ids
    assert "inactive" not in ids


def test_dispatch_refresh_batch_with_no_due_podcasts() -> None:
    out = dispatch_refresh_batch()
    assert out == {"dispatched": 0, "remaining": 0}


def test_dispatch_refresh_batch_chord_built(monkeypatch: Any) -> None:
    """The dispatch task builds a chord for every due podcast."""
    _make_podcast(id_="a")
    _make_podcast(id_="b")
    captured: dict[str, Any] = {}

    def _fake_chord(header: Any) -> Any:
        captured["header"] = header
        captured["called_with"] = len(header)

        class _Chord:
            def __init__(self, h: Any) -> None:
                pass

            def __call__(self, callback: Any) -> Any:
                captured["callback"] = callback
                return None

        return _Chord(header)

    monkeypatch.setattr("podcasts.tasks.chord", _fake_chord)
    out = dispatch_refresh_batch()
    assert out["dispatched"] == 2
    # chord() was called once with a 2-item header and the
    # summarise_refresh_run callback. The signature objects
    # compare by task name, not identity.
    assert captured["called_with"] == 2
    assert captured["callback"].task == "podcasts.tasks.summarise_refresh_run"


def test_refresh_one_podcast_records_metrics() -> None:
    p = _make_podcast(id_="p_metric")
    with (
        patch(
            "podcasts.services.fetch_feed",
            return_value=b"<rss/>",
        ),
        patch(
            "podcasts.services.parse_feed_bytes",
            return_value=MagicMock(entries=[]),
        ),
    ):
        result = refresh_one_podcast("p_metric")
    assert result["podcast_id"] == "p_metric"
    assert result["result"] == "ok"
    p.refresh_from_db()
    assert p.last_refresh_status == "ok"


def test_refresh_one_podcast_skips_missing() -> None:
    result = refresh_one_podcast("does-not-exist")
    assert result["result"] == "skipped"


def test_summarise_refresh_run_publishes_stale_gauge() -> None:
    now = django_timezone.now()
    _make_podcast(
        id_="stale1",
        last_refresh_status="error",
        last_refreshed_at=now - timedelta(hours=3),
    )
    _make_podcast(
        id_="healthy1", last_refresh_status="ok", last_refreshed_at=now
    )
    reports = [
        {"podcast_id": "stale1", "result": "error"},
        {"podcast_id": "healthy1", "result": "ok"},
    ]
    summary = summarise_refresh_run(reports)
    assert summary["total"] == 2
    assert summary["ok"] == 1
    assert summary["errors"] == 1
    assert summary["stale"] == 1
