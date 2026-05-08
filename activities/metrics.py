"""Metrics for activity scheduling.

Provides Prometheus metrics for activity execution monitoring.
"""

from prometheus_client import Counter, Gauge, Histogram

activities_dispatched = Counter(
    "activities_dispatched_total",
    "Activities dispatched",
    ["type", "status"],
)

activity_duration_seconds = Histogram(
    "activity_duration_seconds",
    "Activity execution duration",
    ["type"],
)

activities_active = Gauge(
    "activities_active",
    "Currently active activities",
    ["type", "status"],
)
