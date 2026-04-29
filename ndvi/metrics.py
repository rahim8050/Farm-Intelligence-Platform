from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

ndvi_jobs_total = Counter(
    "ndvi_jobs_total",
    "Total NDVI jobs processed",
    labelnames=["status", "type", "engine"],
)

ndvi_upstream_requests_total = Counter(
    "ndvi_upstream_requests_total",
    "Count of upstream NDVI engine requests",
    labelnames=["engine", "outcome"],
)

ndvi_upstream_latency_seconds = Histogram(
    "ndvi_upstream_latency_seconds",
    "Latency of upstream NDVI engine requests",
    labelnames=["engine"],
    buckets=(0.1, 0.3, 0.5, 1, 2, 5, 10, 20, 30),
)

ndvi_cache_hit_total = Counter(
    "ndvi_cache_hit_total",
    "Cache hits by NDVI layer",
    labelnames=["layer"],
)

ndvi_task_runtime_seconds = Histogram(
    "ndvi_task_runtime_seconds",
    "Runtime of NDVI Celery tasks",
    labelnames=["task", "engine"],
    buckets=(0.1, 0.3, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300),
)

redis_stream_pending_entries = Gauge(
    "redis_stream_pending_entries",
    "Number of pending entries in a Redis stream consumer group",
    labelnames=["group"],
)

redis_stream_pending_age_max = Gauge(
    "redis_stream_pending_age_max",
    "Age in seconds of the oldest pending Redis stream entry",
    labelnames=["group"],
)

ndvi_stream_consumer_heartbeat = Gauge(
    "ndvi_stream_consumer_heartbeat",
    "Unix timestamp of the NDVI stream consumer heartbeat",
    labelnames=["consumer"],
)

ndvi_stream_consumer_failures_total = Counter(
    "ndvi_stream_consumer_failures_total",
    "Total NDVI stream consumer failures",
    labelnames=["consumer", "failure_type"],
)

ndvi_farms_stale_total = Gauge(
    "ndvi_farms_stale_total",
    "Gauge of farms missing fresh NDVI observations",
    labelnames=["engine"],
)

# Circuit breaker state gauge (one time series per engine)
# Values: 0=CLOSED, 1=OPEN, 2=HALF_OPEN
ndvi_circuit_breaker_state = Gauge(
    "ndvi_circuit_breaker_state",
    "Current state of NDVI circuit breaker",
    labelnames=["engine"],
)

# Circuit breaker transition counter
ndvi_circuit_breaker_transitions_total = Counter(
    "ndvi_circuit_breaker_transitions_total",
    "Total number of circuit breaker state transitions",
    labelnames=["engine", "from_state", "to_state"],
)
