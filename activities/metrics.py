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

activities_scheduler_runs = Counter(
    "activities_scheduler_runs_total",
    "Activity scheduler polling runs",
    ["status"],
)

activities_scheduler_dispatch_latency_seconds = Histogram(
    "activities_scheduler_dispatch_latency_seconds",
    "Activity scheduler dispatch latency",
    ["status"],
)

activities_websocket_events = Counter(
    "activities_websocket_events_total",
    "Activity WebSocket events emitted",
    ["status"],
)

activities_websocket_failures = Counter(
    "activities_websocket_failures_total",
    "Activity WebSocket delivery failures",
    ["stage"],
)

activities_lock_contention = Counter(
    "activities_lock_contention_total",
    "Activity claim or execution contention events",
    ["stage"],
)

activities_circuit_breaker_trips = Counter(
    "activities_circuit_breaker_trips_total",
    "Circuit breaker trips by handler type",
    ["type"],
)

activities_circuit_breaker_resets = Counter(
    "activities_circuit_breaker_resets_total",
    "Circuit breaker resets by handler type",
    ["type"],
)

activities_dead_letter_count = Counter(
    "activities_dead_letter_count_total",
    "Dead-letter entries registered",
    ["type"],
)

activities_chained_count = Counter(
    "activities_chained_count_total",
    "Follow-up activities created from chaining",
    ["source_type", "target_type"],
)

activities_ndvi_event_count = Counter(
    "activities_ndvi_event_count_total",
    "NDVI events received",
    ["event_type", "status"],
)
