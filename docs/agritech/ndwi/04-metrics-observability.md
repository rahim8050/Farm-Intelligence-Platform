# NDWI Metrics & Observability

**Document:** 04-metrics-observability.md
**Stage:** Design (pre-implementation)
**Status:** Draft for review

---

## Strategy

Two options are under consideration:

**Option A: Separate `ndwi_*` metrics** — Copy all 37+ `ndvi_*` metric definitions with `ndwi_` prefix. Simpler to implement but doubles metric count.

**Option B: Unified `spectral_index_*` metrics with `index` label** — Rename all metrics to `spectral_index_*` and add an `index="NDVI"|"NDWI"` label. Requires dashboard migration for NDVI.

**Decision:** Start with Option A (separate `ndwi_*` metrics) in Phase 1–6, migrate to Option B in a future cleanup release. This avoids any risk of NDVI dashboard regression.

## NDWI Metric Catalog

All metrics mirror NDVI with `ndwi_` prefix. Only differences from `ndvi_*` metrics are listed.

### Counters

| Metric | Labels | Description |
|--------|--------|-------------|
| `ndwi_jobs_total` | status, type, engine | Jobs processed |
| `ndwi_upstream_requests_total` | engine, outcome | Upstream API calls |
| `ndwi_cache_hit_total` | layer | Redis cache hits |
| `ndwi_stream_consumer_failures_total` | consumer, failure_type | Stream consumer errors |
| `ndwi_version_mismatch_total` | engine, old_version | Observation version issues |
| `ndwi_recompute_trigger_total` | engine, reason | Recompute trigger events |
| `ndwi_anomaly_detected_total` | engine, type | Anomalies detected (NDWI-specific thresholds) |
| `ndwi_append_only_writes_total` | engine | Append-only mode writes |
| `ndwi_idempotent_hit_total` | engine | Idempotent dedup hits |
| `ndwi_constraint_collision_total` | engine, constraint | Constraint violation events |
| `ndwi_supersession_total` | engine | State supersession events |
| `ndwi_recompute_failure_total` | engine, reason | Recompute failures |
| `ndwi_lock_contention_total` | operation, reason | Lock contention |
| `ndwi_retry_classification_total` | operation, class | Retry classification count |
| `ndwi_retry_storm_window_total` | operation | Retry storm events |
| `ndwi_starvation_events_total` | operation | Starvation events |
| `ndwi_long_lock_wait_total` | operation, threshold_seconds | Excessive lock wait |
| `ndwi_v2_null_output_total` | engine, null_reason | V2 null output reasons |
| `ndwi_v2_low_confidence_total` | engine | V2 low confidence count |
| `ndwi_fallback_usage_total` | engine_selected, engine_primary | Fusion fallback events |
| `ndwi_source_disagreement_total` | engine_a, engine_b | Fusion conflict detection |
| `ndwi_backfill_rows_total` | engine, status | Backfill row count |
| `ndwi_stream_dlq_total` | consumer | Dead-letter queue entries |
| `ndwi_v2_suppressed_observations_total` | reason | V2 suppressed observations |

### Histograms

| Metric | Labels | Buckets | Description |
|--------|--------|---------|-------------|
| `ndwi_upstream_latency_seconds` | engine | 0.1,0.3,0.5,1,2,5,10,20,30 | Upstream API latency |
| `ndwi_task_runtime_seconds` | task, engine | 0.1..300 | Task execution duration |
| `ndwi_transaction_duration_seconds` | operation | 0.01..60 | DB transaction duration |
| `ndwi_lock_wait_seconds` | operation | 0.01..10 | Lock acquisition wait |
| `ndwi_v2_confidence_bucket` | engine, source | 0.1..1.0 | V2 confidence distribution |

### Gauges

| Metric | Labels | Description |
|--------|--------|-------------|
| `ndwi_farms_stale_total` | engine | Farms with stale NDWI data |
| `ndwi_circuit_breaker_state` | engine | Circuit breaker state (0/1/2) |
| `ndwi_observation_latest_age_seconds` | engine | Age of latest NDWI observation |
| `ndwi_observation_state_total` | engine, state | Observation count by state |
| `ndwi_recompute_backlog_total` | engine | Recompute backlog depth |
| `ndwi_raw_stale_age_seconds` | engine | Age of raw stale observations |
| `ndwi_stream_consumer_heartbeat` | consumer | Stream consumer liveness |
| `ndwi_stream_pending_entries` | group | Pending stream entries |
| `ndwi_stream_pending_age_max` | group | Max pending entry age |

## Grafana Dashboard

### New Panel: NDWI Overview

Copy of the NDVI overview dashboard with:

- NDWI timeseries panel (mean/min/max over time)
- NDWI latest value heatmap by farm
- NDWI confidence distribution
- NDWI source usage pie chart
- NDWI freshness gauge
- NDWI circuit breaker status

### Shared Panels

The following panels cover all indices and are added to a new "Spectral Indices" dashboard:

| Panel | Query | Indices |
|-------|-------|---------|
| Job completion rate | `rate(ndwi_jobs_total{status="success"}[5m])` | NDVI, NDWI |
| Upstream error rate | `rate(ndwi_upstream_requests_total{outcome=~"error|timeout"}[5m])` | NDVI, NDWI |
| Cache hit ratio | `ndwi_cache_hit_total / (ndwi_cache_hit_total + ndwi_cache_miss_total)` | NDVI, NDWI |
| Observation freshness | `ndwi_observation_latest_age_seconds` | NDVI, NDWI |
| V2 null rate | `rate(ndwi_v2_null_output_total[1d]) / rate(ndwi_v2_observation_total[1d])` | NDVI, NDWI |

## Alerts

| Alert | Condition | Severity | Response |
|-------|-----------|----------|----------|
| NDWI data stale | `ndwi_observation_latest_age_seconds > 86400` (>24h) | warning | Investigate engine or upstream |
| NDWI upstream errors | `rate(ndwi_upstream_requests_total{outcome="error"}[5m]) > 0.1` | critical | Check STAC API / SentinelHub |
| NDWI circuit breaker open | `ndwi_circuit_breaker_state > 0` | critical | Manual reset or upstream recovery |
| NDWI high null rate | `rate(ndwi_v2_null_output_total[1d]) / rate(ndwi_v2_observation_total[1d]) > 0.20` | warning | Tune NDWI thresholds |
| NDWI fusion conflict | `rate(ndwi_source_disagreement_total[1h]) > 5` | warning | Investigate engine disagreement |
| NDWI job failures | `rate(ndwi_jobs_total{status="failed"}[30m]) > 3` | warning | Check worker logs |

## SLO/SLI Considerations

| SLI | Target | Measurement |
|-----|--------|-------------|
| NDWI data freshness | ≤ 24h | `max(ndwi_observation_latest_age_seconds)` |
| NDWI API availability | ≥ 99.5% | Upstream health check pass rate |
| NDWI V2 quality availability | ≥ 80% of observations with confidence ≥ 0.75 | `ndwi_v2_confidence_bucket` |
| NDWI task success rate | ≥ 95% | `ndwi_jobs_total{status="success"} / ndwi_jobs_total` |
| NDWI cache hit rate | ≥ 70% | `ndwi_cache_hit_total / (ndwi_cache_hit_total + ndwi_cache_miss_total)` |
