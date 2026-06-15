# NDVI Pipeline Evolution - Implementation Status Report

**Date:** April 18, 2026 (original)
**Date:** June 03, 2026 (status re-verified against current code)
**Architecture Document:** `docs/architecture/ndvi-pipeline-evolution.md`
**Implementation Plan:** `docs/architecture/ndvi-phase-2-implementation-plan.md`
**Related:** `docs/status/NDVI_RETRY_POLICY_STATUS.md`

> **Re-verification (June 03, 2026):** This document was last rewritten on
> April 18, 2026. Between then and June 03, 2026 the consumer command,
> stream observability metrics, Grafana panels, Prometheus alerts, and
> consumer tests were all implemented. The percentages and "Required
> Files Not Yet Created" list below are now stale. This revision corrects
> them against the current repo. The historical commit log is preserved
> in the "Document History" section at the bottom.

---

## Executive Summary

The NDVI pipeline is being modernized in phases to eliminate Redis SPOF, add
durable queue semantics, and improve observability. **Phase 1 is complete**,
**Phase 1.5 (retry policy) is complete**, **Phase 2 is fully
implemented through Stage 7**, **Phase 3 Multi-Engine Fallback is complete**,
and **Phase 3/4 Fusion & Intelligence is complete**. **Stage 8
(incremental rollout) is intentionally deferred**: `NDVI_QUEUE_BACKEND`
remains `"celery"` and no production workflow is stream-backed yet.

**Architectural invariant:** the stream consumer must integrate with the
hardened retry policy (`ndvi/retry_policy.py`) so that retry decisions stay
consistent across the stream path and the existing Celery path.

---

## Phase 1 - Redis Sentinel (HA Broker/Cache)

**Status:** ✅ **COMPLETE** (March 24 - April 1, 2026) — unchanged.

### What's Implemented

- ✅ **Redis Sentinel trio deployed**
  - `docker-compose.redis-sentinel.yml` exists with Sentinel configuration.
  - Sentinel service name and ports configured.
- ✅ **Django/Celery Sentinel integration**
  - `config/settings.py`: full Sentinel URL parsing (`redis-sentinel://`).
  - Converts Sentinel URLs to broker-compatible format for Celery.
  - Validates Sentinel scheme and extracts credentials/hosts.
- ✅ **Cache backend Sentinel support**
  - Django cache uses Sentinel-backed Redis; round-trip tested.
- ✅ **Celery broker Sentinel connection**
  - Sentinel-backed broker, `SentinelBackend` result backend.
- ✅ **Failover validation**
  - Failover drill executed (April 1, 2026).
  - Sentinel election observed; Celery survived (~54.7s recovery).
- ✅ **Sentinel metrics in Prometheus**
  - `redis_sentinel_master_status`, `redis_sentinel_master_ok_sentinels`,
    `redis_sentinel_master_ok_slaves`, `redis_sentinel_masters`.
- ✅ **Test coverage** in `tests/test_settings_redis_sentinel.py`.

### What's Left Out

- ⚠️ **Celery failover latency not acceptable for latency-sensitive workloads**
  - 54.7s delay is acceptable for background jobs only.
  - Not acceptable for real-time task dispatch (<10-15s target).
  - **Recommended:** Tune Celery reconnect behavior if needed.

---

## Phase 1.5 - NDVI Retry Policy Hardening

**Status:** ✅ **COMPLETE** (April 12, 2026) — verified June 03, 2026.

### What's Implemented

- ✅ **`classify_status_code()` — single source of truth** in
  `ndvi/retry_policy.py`; 13 branches covered.
- ✅ **Unified exception hierarchy.** All NDVI errors inherit from
  `UpstreamFailureError` with consistent `retryable`, `category`,
  `status_code`.
- ✅ **`should_retry()` — central retry decision function** returning
  `RetryDecision(retry, delay, reason)`.
- ✅ **Circuit breaker for STAC engine** in `ndvi/stac_client.py`.
- ✅ **Network error handling fixed** — `httpx.RequestError` wrapped after
  inline retry exhaustion.
- ✅ **Shared `CircuitBreaker` extracted to `ndvi/circuit_breaker.py`**
  (`CircuitBreaker` class at `ndvi/circuit_breaker.py:46`) and wired
  into all three engines: `stac`, `sentinelhub`,
  `sentinelhub_raster`. Eagerly initialized in
  `ndvi/apps.py:12-45`.
- ✅ **`Retry-After` header parsing** in `ndvi/retry_policy.py:84-128`
  (`parse_retry_after()`). `should_retry()` accepts `response_headers`
  and applies the delay only for 429 responses.
- ✅ **Prometheus metrics for circuit breaker state**
  (`ndvi/metrics.py:69-80`): `ndvi_circuit_breaker_state{engine}` and
  `ndvi_circuit_breaker_transitions_total{engine,from_state,to_state}`.
- ✅ **Admin endpoint to reset circuit breaker**
  `POST /api/v1/ndvi/circuit-breaker/reset/` (view at
  `ndvi/views.py:1309`; `permission_classes = [IsAdminUser]`). 4 tests
  in `ndvi/tests/test_ndvi_admin_views.py`.
- ✅ **Upstream health check endpoint**
  `GET /api/v1/ndvi/health/upstream/` (per-engine status).
- ✅ **Test coverage: 28+ tests** including 7 dedicated to
  `parse_retry_after` and 4 for the admin endpoint.

### What's Left Out

- None. All Phase 2 and Phase 3 retry-policy items called out in
  `docs/status/NDVI_RETRY_POLICY_STATUS.md` are now implemented.

**Full details:** See `docs/status/NDVI_RETRY_POLICY_STATUS.md`.

---

## Phase 2 - Redis Streams for NDVI

**Status:** 🟢 **STAGES 1-7 COMPLETE** (was: "Stages 1, 3, 4, and 5
complete" — Stage 4 is now also complete, plus Stages 6 and 7). **Stage 8
(rollout) is intentionally deferred** until the existing Celery
deployment has been observed under stream-side metrics for an
appropriate period.

### Stage 1 - Centralize NDVI Dispatch (✅ 100% Complete — unchanged)

- ✅ `dispatch_ndvi_job()` and `dispatch_farm_state_coverage()` in
  `ndvi/services.py`.
- ✅ `NDVI_QUEUE_BACKEND = "celery"` in `config/settings.py:712` with
  routing switch.
- ✅ All 9 call sites route through dispatch helpers.
- ✅ Tests for Celery routing and stream-backed dispatch behavior.

### Stage 2 - Choose Transport Model (✅ 100% Complete — was 40%)

- ✅ **Decision documented:** separate Redis Streams consumer, not
  Celery's built-in stream transport.
- ✅ **Consumer implementation exists** at
  `ndvi/management/commands/consume_ndvi_stream.py` (528 lines).
  - `XGROUP CREATE` (`_ensure_group`, line 149) with `mkstream=True`
    and `BUSYGROUP` handling.
  - Blocking `XREADGROUP` (`_read_messages`, line 168) with
    `count`/`block` parameters.
  - `XACK` after successful enqueue (in `_process_message`, line 286).
  - `XPENDING` / `XAUTOCLAIM` reclaim loop
    (`_periodic_reclaim`, `_run_autoclaim`, lines 197/208).
  - Dead-letter stream handling (`_move_to_dlq`, line 398) with
    enriched payload (`dlq_reason`, `dlq_original_id`,
    `dlq_delivery_count`).
  - `XTRIM` for stream and DLQ (`_trim_streams`, line 517).

### Stage 3 - Stream Producer Logic (✅ 100% Complete — unchanged)

- ✅ `ndvi/streams.py` exports `build_stream_payload`,
  `publish_ndvi_job`, `build_farm_state_coverage_payload`,
  `publish_farm_state_coverage`.
- ✅ Stream payload schema covers `job_id`, `request_hash`,
  `farm_id`, `owner_id`, `engine`, `job_type`, params,
  `colormap_normalization`, `enqueue_timestamp`.
- ✅ `XADD` with `MAXLEN ~` from `NDVI_STREAM_MAXLEN`.
- ✅ Dispatch helpers publish to the stream when
  `NDVI_QUEUE_BACKEND=stream`.
- ✅ Tests in `ndvi/tests/test_ndvi_streams.py` (~16 tests).

### Stage 4 - Stream Consumer Logic (✅ 100% Complete — was 0%)

- ✅ `ndvi/management/commands/consume_ndvi_stream.py` is fully
  implemented.
- ✅ `XGROUP CREATE` for bootstrap with idempotent re-create handling.
- ✅ Blocking `XREADGROUP` read loop.
- ✅ Payload routing to `run_ndvi_job` and
  `compute_farm_state_coverage` Celery tasks.
- ✅ `XACK` after successful enqueue.
- ✅ `XPENDING` / `XCLAIM` reclaim path for stale deliveries.
- ✅ Poison-message budget enforced via
  `NDVI_STREAM_MAX_DELIVERIES`; excess deliveries route to DLQ.
- ✅ Dead-letter stream routing with enriched metadata.
- ✅ `XTRIM` for stream and DLQ.

### Stage 5 - Settings and Feature Flags (✅ 100% Complete — unchanged)

All 15 stream-related settings exist in `config/settings.py:712-732`:

- `NDVI_QUEUE_BACKEND` (default `"celery"`)
- `NDVI_STREAM_NAME` (default `"ndvi:stream"`)
- `NDVI_STREAM_GROUP` (default `"ndvi-group"`)
- `NDVI_STREAM_CONSUMER` (default `"consumer_1"`)
- `NDVI_STREAM_METRICS_PORT` (default `8002`)
- `NDVI_STREAM_BLOCK_MS` (default `5000`)
- `NDVI_STREAM_CLAIM_IDLE_MS` (default `60000`)
- `NDVI_STREAM_MAXLEN` (default `10000`)
- `NDVI_STREAM_DLQ_NAME` (default `"ndvi:dlq"`)
- `NDVI_STREAM_DLQ_MAXLEN` (default `10000`)
- `NDVI_STREAM_BATCH_SIZE` (default `10`)
- `NDVI_STREAM_MAX_DELIVERIES` (default `5`)
- `NDVI_STREAM_START_ID` (default `"0"`)
- `NDVI_STREAM_RECLAIM_INTERVAL_SECONDS` (default `60`)

Default remains `celery`; stream mode is opt-in.

### Stage 6 - Observability (✅ 100% Complete — was 0%)

- ✅ **Stream metrics exported** in `ndvi/metrics.py:37-59`:
  - `redis_stream_pending_entries{group}`
  - `redis_stream_pending_age_max{group}`
  - `ndvi_stream_consumer_heartbeat{consumer}`
  - `ndvi_stream_consumer_failures_total{consumer, failure_type}`
- ✅ **Upstream request and latency metrics** for SentinelHub, STAC,
  and Raster engines (`ndvi/metrics.py:11-22`).
- ✅ **NDVI task runtime histogram** `ndvi_task_runtime_seconds{task,
  engine}` (`ndvi/metrics.py:30`).
- ✅ **Grafana panels** for stream lag and consumer health added to
  `grafana/dashboards/farm-intelligence-platform-ndvi.json` (lines 290, 319, 348, 377).
- ✅ **Prometheus alerts** in `monitoring/prometheus/alerts.yml:168-204`:
  - `NDVIStreamLagHigh` (pending > 100 for 10m)
  - `NDVIStreamOldMessage` (age > 3600s)
  - `NDVIStreamConsumerDown` (heartbeat stale > 600s)
  - `NDVIStreamConsumerFailuresHigh` (failure rate > 0.1/s)
- ✅ **Prometheus scrape job** for the consumer's metrics endpoint
  registered in `monitoring/prometheus/prometheus.yml:20`
  (`ndvi_stream_consumer`).
- ✅ **NDVI consumer metrics server** is started in-process by
  `consume_ndvi_stream._start_metrics_server` (line 123).

### Stage 7 - Tests (✅ 100% Complete — was 35%)

- ✅ **Producer and dispatch tests** in
  `ndvi/tests/test_ndvi_streams.py` (~16 tests).
- ✅ **Consumer tests** in
  `ndvi/tests/test_ndvi_stream_consumer.py` (251 lines), covering:
  - metrics server lifecycle
  - pending/age gauge updates
  - enqueue + `XACK` happy path
  - stale message reclaim via `XPENDING`/`XCLAIM`
  - dead-letter routing and poison-message budget
- ✅ **Task integration tests** in
  `ndvi/tests/test_ndvi_tasks_extra.py`.
- ✅ **Circuit-breaker admin tests** in
  `ndvi/tests/test_ndvi_admin_views.py` (4 tests).
- ✅ **Retry-After tests** in
  `ndvi/tests/test_ndvi_retry_policy.py:180-246` (7 tests).

### Stage 8 - Rollout (🔴 Intentionally Deferred — 0%)

- ❌ No incremental rollout has occurred.
- `NDVI_QUEUE_BACKEND` default is still `"celery"`
  (`config/settings.py:712`).
- No production pilot with stream-backed dispatch.
- **Why this is correct:** the rollout document
  (`docs/architecture/ndvi/08_rollout_strategy.md`) requires
  shadow-compute → dual-run → promotion → deprecation, gated on
  observation. The consumer is ready; the rollout itself is
  operational work, not code work.

---

## Phase 3 - Observability Enhancements

**Status:** 🟢 **MERGED INTO PHASE 2 STAGE 6 (COMPLETE)**

All items previously listed as Phase 3 ("stream lag panels in Grafana",
"stream lag alerting", "NDVI-specific Celery histograms") were implemented
as part of Phase 2 Stage 6 (see above). No separate Phase 3 work remains.

---

## Phase 3 - Multi-Engine Fallback

**Status:** ✅ **COMPLETE** (June 11, 2026)

### What's Implemented

- ✅ **Fusion service** (`ndvi/fusion.py:gather_candidates`, `_select_by_decision_tree`)
  - Gathers V2 candidates from all engines for a (farm, bucket_date)
  - Deterministic decision tree with explicit threshold checks
  - Confidence degradation: Landsat ×0.90, MODIS ×0.80
  - Conflict rule: top-2 NDVI diff ≥0.10 and neither ≥0.75 → NULL
  - Tie-break by source priority: Sentinel-2 → Landsat → MODIS
- ✅ **Fallback engine adapters** (`ndvi/engines/landsat.py`, `ndvi/engines/modis.py`)
  - Functional STAC-based implementations (not stubs)
  - Conform to the `NDVIEngine` protocol
- ✅ **Integration tests** in `ndvi/tests/test_phase3_fusion.py`
- ✅ **Fusion metrics** (`ndvi_fallback_usage_total`, `ndvi_source_disagreement_total`,
  `ndvi_v2_suppressed_observations_total`)

## Phase 3/4 - Fusion and Intelligence

**Status:** ✅ **COMPLETE** (June 11, 2026)

### What's Implemented

- ✅ **Cross-source disagreement detection** — conflict rule in `_check_conflict()`
  sets `source_disagreement` flag; returns NULL when triggered
- ✅ **Sentinel-1 context** (`ndvi/sentinel1_context.py`)
  - Queries CDSE STAC API for Sentinel-1 GRD items using farm bbox and bucket date
  - Derives `wet_soil`, `flooding`, `rough_surface` flags from item metadata
  - SAR backscatter processing is not yet implemented (VV/VH ratio analysis deferred)
- ✅ **Anomaly detection** — `detect_anomaly()` uses NDVI + S1 context to flag
  `possible_flooding`, `wet_soil_depression`, `urban_artifact`
- ✅ **Quality flags** in `FusionResult`: `source_disagreement`, `fallback_used`,
  `anomaly_detected`, s1 context flags
- ✅ **DLQ counter** (`ndvi_stream_dlq_total`) exported and incremented
- ✅ **Handler exception hierarchy** (`TemporaryHandlerError` / `PermanentHandlerError`)
  in `activities/handlers/base.py`

## Phase 4 - Kafka (Future / Conditional)

**Status:** 🔴 **NOT STARTED** (Intentionally Deferred) — unchanged.

- ✅ Kafka adoption triggers defined in `ndvi-pipeline-evolution.md`.
- ✅ Decision to defer Kafka is documented and justified.
- ✅ Thresholds established (stream lag despite consumer scale, durable
  replay across services, partitioned/fan-out consumption).
- ❌ Kafka topics, producers, consumers, and migration path are not
  implemented.

**Earliest re-evaluation date:** Q3 2026 (per
`ndvi-pipeline-evolution.md`).

---

## Implementation Summary by Phase

| Phase | Status | Completion | Notes |
|-------|--------|------------|-------|
| **Phase 1: Redis Sentinel** | ✅ Complete | **100%** | Sentinel HA for broker/cache/result backend |
| **Phase 1.5: Retry Policy** | ✅ Complete | **100%** | Policy + shared circuit breaker + CB metrics + admin |
| **Phase 2 Stage 1: Centralize Dispatch** | ✅ Complete | **100%** | Helpers, routing switch, tests in place |
| **Phase 2 Stage 2: Transport Model** | ✅ Complete | **100%** | Separate consumer implemented |
| **Phase 2 Stage 3: Stream Producer** | ✅ Complete | **100%** | Producer code, payload schema, and tests in place |
| **Phase 2 Stage 4: Stream Consumer** | ✅ Complete | **100%** | `consume_ndvi_stream` command with reclaim/DLQ/trim |
| **Phase 2 Stage 5: Settings** | ✅ Complete | **100%** | 15 stream settings; default stays `celery` |
| **Phase 2 Stage 6: Observability** | ✅ Complete | **100%** | Stream metrics, Grafana panels, Prometheus alerts |
| **Phase 2 Stage 7: Tests** | ✅ Complete | **100%** | Producer, consumer, admin, and retry-after tests |
| **Phase 2 Stage 8: Rollout** | 🔴 Deferred | **0%** | No production pilot; `NDVI_QUEUE_BACKEND=celery` |
| **Phase 3: Observability** | ✅ Complete | **100%** | Merged into Phase 2 Stage 6 |
| **Phase 3: Multi-Engine Fallback** | ✅ Complete | **100%** | Fusion service, fallback selector, Landsat/MODIS engines |
| **Phase 3/4: Fusion & Intelligence** | ✅ Complete | **100%** | S1 context, anomaly detection, quality flags, DLQ counter |
| **Phase 4: Kafka** | 🔴 Deferred | **0%** | Intentionally deferred until thresholds met |

---

## Current Repo State (June 03, 2026)

The following are present in the repo:

- `ndvi/streams.py` — producer module (`publish_ndvi_job`,
  `publish_farm_state_coverage`, `build_stream_payload`,
  `build_farm_state_coverage_payload`).
- `ndvi/management/commands/consume_ndvi_stream.py` — consumer command
  with `XGROUP CREATE`, `XREADGROUP`, `XACK`, `XPENDING`/`XAUTOCLAIM`,
  DLQ routing, `XTRIM`, and graceful shutdown.
- `ndvi/circuit_breaker.py` — shared `CircuitBreaker` class with
  CLOSED/OPEN/HALF_OPEN state machine, eagerly registered for all three
  engines in `ndvi/apps.py:12-45`.
- `ndvi/circuit_breaker.py` exports `register_circuit_breaker`,
  `get_circuit_breaker`, `list_circuit_breakers` for the admin endpoint.
- `ndvi/views.py:1309` — `CircuitBreakerResetView` (`IsAdminUser`).
- `ndvi/views.py:1383` — upstream health-check endpoint.
- `ndvi/metrics.py:37-80` — stream, upstream, task runtime, and
  circuit-breaker metrics.
- `ndvi/tests/test_ndvi_streams.py`,
  `ndvi/tests/test_ndvi_stream_consumer.py`,
  `ndvi/tests/test_ndvi_admin_views.py`,
  `ndvi/tests/test_ndvi_retry_policy.py` — test coverage.
- `monitoring/prometheus/alerts.yml:168-204` — four stream alerts.
- `monitoring/prometheus/prometheus.yml:20` — consumer scrape job.
- `grafana/dashboards/farm-intelligence-platform-ndvi.json` — stream lag, age,
  heartbeat, and failure panels (lines 290, 319, 348, 377).
- `grafana/dashboards/farm-intelligence-platform-observability.json` — circuit-breaker
  state and transition panels (lines 998, 1071, 1144, 1217).
- `config/settings.py:712-732` — all 15 stream settings; default
  `NDVI_QUEUE_BACKEND=celery`.

The following are **not** present, by design:

- No production deployment of `NDVI_QUEUE_BACKEND=stream`.
- No Kafka topics, producers, or consumers.

---

## Recommended Next Steps

### Operational (this quarter)

1. **Stage 8 pilot** — Enable `NDVI_QUEUE_BACKEND=stream` for
   `enqueue_daily_farm_state_coverage()` only. Observe stream metrics
   for 1-2 weeks. See `docs/architecture/ndvi/08_rollout_strategy.md`
   for the phased rollout criteria.

### Potential follow-up work

2. **DLQ counter** — `ndvi_stream_dlq_total{consumer}` is now exported
   in `ndvi/metrics.py` (line 286) and incremented in
   `consume_ndvi_stream.py:_move_to_dlq` (line 433).

3. **Phase 4 (Kafka) re-evaluation** — earliest review Q3 2026 per
   `ndvi-pipeline-evolution.md`. No code work; check the
   documented triggers (stream lag despite consumer scale, replay/fan-out
   requirements).

---

## Technical Debt & Risks

### Resolved since the previous version of this doc

- ~~No stream consumer code exists~~ — implemented in
  `ndvi/management/commands/consume_ndvi_stream.py`.
- ~~Producer exists without a consumer~~ — consumer now exists; the
  risk of enabling stream mode "with nothing to drain" is gone, but
  Stage 8 is still gated on operational observation.
- ~~Observability and rollout criteria still need implementation~~ —
  observability is in place; rollout is intentionally the next
  operational step.

### Current risks

1. **Celery failover latency** (Phase 1)
   - 54.7s recovery may be unacceptable for latency-sensitive tasks.
   - **Mitigation:** Tune Celery reconnect or accept delay for
     background jobs.

2. ~~**DLQ volume is not separately counted**~~ — now counted via
   `ndvi_stream_dlq_total{consumer}`. ✨ RESOLVED

---

## Document History

| Version | Date | Author | Notes |
|---------|------|--------|-------|
| 1.0 | April 18, 2026 | opencode | Initial status snapshot; Stages 4, 6, 7 marked incomplete. |
| 1.1 | June 03, 2026 | opencode | Re-verified: Stages 2, 4, 6, 7 are now complete in code. Stage 8 remains intentionally deferred. |
| 2.0 | June 11, 2026 | opencode | Phase 3 Multi-Engine Fallback (fusion + engines) complete. Phase 3/4 Fusion & Intelligence complete (S1 context, anomaly detection, quality flags). DLQ counter exported. S1 context upstream integration implemented (CDSE STAC query). |
