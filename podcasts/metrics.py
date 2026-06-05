"""Prometheus metrics for the ``podcasts`` app.

Covers the production-readiness review
(``prompts/p4-staff-engineer-review.md`` #3 and #5): counters and
gauges needed to detect stuck feeds and to alert on stale
podcasts.

Exposed series:

- :data:`podcasts_refresh_total` - incremented once per
  :func:`alerts.services.dispatch_alert` per podcast, labeled by
  ``result`` (``ok`` / ``error``).
- :data:`podcasts_refresh_duration_seconds` - histogram of per-feed
  refresh time.
- :data:`podcasts_refresh_stale` - gauge of active podcasts whose
  ``last_refreshed_at`` is older than 2x the configured interval.
"""

from __future__ import annotations

from typing import Any

try:  # pragma: no cover
    from prometheus_client import Counter, Gauge, Histogram
except Exception:  # pragma: no cover
    Counter = Gauge = Histogram = None  # type: ignore[assignment,misc]


def _counter(name: str, doc: str, labels: list[str]) -> Any:
    if Counter is None:  # pragma: no cover
        return None
    return Counter(name, doc, labels)


def _histogram(name: str, doc: str, labels: list[str]) -> Any:
    if Histogram is None:  # pragma: no cover
        return None
    return Histogram(name, doc, labels)


def _gauge(name: str, doc: str, labels: list[str]) -> Any:
    if Gauge is None:  # pragma: no cover
        return None
    return Gauge(name, doc, labels)


podcasts_refresh_total = _counter(
    "podcasts_refresh_total",
    "Podcasts refresh attempts.",
    ["result"],
)
podcasts_refresh_duration_seconds = _histogram(
    "podcasts_refresh_duration_seconds",
    "Per-podcast feed refresh latency.",
    ["result"],
)
podcasts_refresh_stale = _gauge(
    "podcasts_refresh_stale",
    "Active podcasts with last_refreshed_at older than 2x the "
    "configured interval.",
    [],
)
