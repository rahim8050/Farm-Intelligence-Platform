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

# Lock contention: transaction duration
ndvi_transaction_duration_seconds = Histogram(
    "ndvi_transaction_duration_seconds",
    "Duration of NDVI database transactions",
    labelnames=["operation"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)

# Lock contention: lock wait time
ndvi_lock_wait_seconds = Histogram(
    "ndvi_lock_wait_seconds",
    "Time spent waiting for database locks",
    labelnames=["operation"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)

# Lock contention: lock acquisition failures
ndvi_lock_contention_total = Counter(
    "ndvi_lock_contention_total",
    "Count of lock contention events (retries, timeouts, deadlocks)",
    labelnames=["operation", "reason"],
)

# Deadlock and retry classification
# Operational classes for failure analysis under heavy recompute load
ndvi_retry_classification_total = Counter(
    "ndvi_retry_classification_total",
    "Count of retry events by operational class",
    labelnames=["operation", "class_"],
)

# Retry storm detection: retries per time window
ndvi_retry_storm_window_total = Counter(
    "ndvi_retry_storm_window_total",
    "Count of retries within storm detection window",
    labelnames=["operation"],
)

# Starvation detection: transactions waiting beyond threshold
ndvi_starvation_events_total = Counter(
    "ndvi_starvation_events_total",
    "Count of transactions waiting beyond starvation threshold",
    labelnames=["operation"],
)

# Long lock wait events (beyond P95 threshold)
ndvi_long_lock_wait_total = Counter(
    "ndvi_long_lock_wait_total",
    "Count of lock waits exceeding P95 threshold",
    labelnames=["operation", "threshold_seconds"],
)

# Phase 6 — V2 quality output monitoring
ndvi_v2_null_output_total = Counter(
    "ndvi_v2_null_output_total",
    "Count of V2 observations forced to null",
    labelnames=["engine", "null_reason"],
)

ndvi_v2_low_confidence_total = Counter(
    "ndvi_v2_low_confidence_total",
    "Count of V2 observations below confidence threshold",
    labelnames=["engine"],
)

ndvi_v2_observation_total = Counter(
    "ndvi_v2_observation_total",
    "Total V2 observations produced (incl. null)",
    labelnames=["engine", "is_null"],
)

ndvi_v2_cache_hit_total = Counter(
    "ndvi_v2_cache_hit_total",
    "Cache hits for V2 derived observations",
    labelnames=["engine"],
)

# Phase 6 — Fallback and fusion monitoring
ndvi_fallback_usage_total = Counter(
    "ndvi_fallback_usage_total",
    "Count of fallback engine selections",
    labelnames=["engine_selected", "engine_primary"],
)

ndvi_source_disagreement_total = Counter(
    "ndvi_source_disagreement_total",
    "Count of source disagreement events during fusion",
    labelnames=["engine_a", "engine_b"],
)

# V2 confidence score distribution
ndvi_v2_confidence_bucket = Histogram(
    "ndvi_v2_confidence_bucket",
    "Distribution of V2 confidence scores",
    labelnames=["engine", "source"],
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

# Source usage tracking per endpoint
ndvi_source_usage_total = Counter(
    "ndvi_source_usage_total",
    "Count of NDVI source usage by source and endpoint",
    labelnames=["source", "endpoint"],
)

# V2 suppressed observations (passed over in fusion selection)
ndvi_v2_suppressed_observations_total = Counter(
    "ndvi_v2_suppressed_observations_total",
    "Count of V2 observations suppressed during fusion",
    labelnames=["reason"],
)

# Backfill rows processed
ndvi_backfill_rows_total = Counter(
    "ndvi_backfill_rows_total",
    "Count of backfill rows processed by engine and status",
    labelnames=["engine", "status"],
)

# DLQ messages
ndvi_stream_dlq_total = Counter(
    "ndvi_stream_dlq_total",
    "Count of messages moved to NDVI stream DLQ",
    labelnames=["consumer"],
)

# ── Unified Spectral Index Metrics ──────────────────────────────
# Design review (10-design-review.md #2) recommends using a single
# metric family with an `index` label instead of per-index duplicates.
# These are added alongside the existing ndvi_* / ndwi_* metrics for
# gradual migration. New code SHOULD increment these instead.
# TODO: Migrate all consumers to spectral_index_* and deprecate the
# per-index metric families.

spectral_jobs_total = Counter(
    "spectral_jobs_total",
    "Jobs processed per spectral index",
    labelnames=["index", "status", "type", "engine"],
)

spectral_upstream_requests_total = Counter(
    "spectral_upstream_requests_total",
    "Upstream engine requests per spectral index",
    labelnames=["index", "engine", "outcome"],
)

spectral_upstream_latency_seconds = Histogram(
    "spectral_upstream_latency_seconds",
    "Latency of upstream engine requests per spectral index",
    labelnames=["index", "engine"],
    buckets=(0.1, 0.3, 0.5, 1, 2, 5, 10, 20, 30),
)

spectral_task_runtime_seconds = Histogram(
    "spectral_task_runtime_seconds",
    "Runtime of Celery tasks per spectral index",
    labelnames=["index", "task", "engine"],
    buckets=(0.1, 0.3, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300),
)

spectral_farms_stale_total = Gauge(
    "spectral_farms_stale_total",
    "Farms missing fresh observations per spectral index",
    labelnames=["index", "engine"],
)

spectral_backfill_rows_total = Counter(
    "spectral_backfill_rows_total",
    "Backfill rows processed per spectral index",
    labelnames=["index", "engine", "status"],
)
