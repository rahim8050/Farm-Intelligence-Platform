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

# Integrity: version drift
ndvi_version_mismatch_total = Counter(
    "ndvi_version_mismatch_total",
    "Count of observations not at current version",
    labelnames=["engine", "old_version"],
)

# Integrity: observation freshness
ndvi_observation_latest_age_seconds = Gauge(
    "ndvi_observation_latest_age_seconds",
    "Age in seconds of the latest observation per farm/engine",
    labelnames=["engine"],
)

# Integrity: recompute activity
ndvi_recompute_trigger_total = Counter(
    "ndvi_recompute_trigger_total",
    "Count of recompute operations triggered",
    labelnames=["engine", "reason"],
)

# Integrity: state distribution
ndvi_observation_state_total = Gauge(
    "ndvi_observation_state_total",
    "Count of observations by lifecycle state",
    labelnames=["engine", "state"],
)

# Integrity: anomaly detection
ndvi_anomaly_detected_total = Counter(
    "ndvi_anomaly_detected_total",
    "Count of NDVI anomalies detected",
    labelnames=["engine", "type"],
)

# Integrity: append-only write tracking
ndvi_append_only_writes_total = Counter(
    "ndvi_append_only_writes_total",
    "Count of append-only observation writes",
    labelnames=["engine"],
)

# Integrity: idempotent hit tracking
ndvi_idempotent_hit_total = Counter(
    "ndvi_idempotent_hit_total",
    "Count of idempotent observation hits (no write needed)",
    labelnames=["engine"],
)

# Saturation: constraint collision tracking
ndvi_constraint_collision_total = Counter(
    "ndvi_constraint_collision_total",
    "Count of IntegrityError collisions during upsert",
    labelnames=["engine", "constraint"],
)

# Saturation: supersession churn
ndvi_supersession_total = Counter(
    "ndvi_supersession_total",
    "Count of observations superseded by newer versions",
    labelnames=["engine"],
)

# Saturation: recompute backlog
ndvi_recompute_backlog_total = Gauge(
    "ndvi_recompute_backlog_total",
    "Count of stale observations awaiting recompute",
    labelnames=["engine"],
)

# Saturation: recompute failures
ndvi_recompute_failure_total = Counter(
    "ndvi_recompute_failure_total",
    "Count of recompute operations that failed",
    labelnames=["engine", "reason"],
)

# Saturation: RAW retention age
ndvi_raw_stale_age_seconds = Gauge(
    "ndvi_raw_stale_age_seconds",
    "Age in seconds of the oldest RAW observation",
    labelnames=["engine"],
)
