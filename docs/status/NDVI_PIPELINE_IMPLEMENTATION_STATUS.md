# NDVI Pipeline Evolution - Implementation Status Report

**Date:** April 18, 2026
**Architecture Document:** `docs/architecture/ndvi-pipeline-evolution.md`
**Implementation Plan:** `docs/architecture/ndvi-phase-2-implementation-plan.md`
**Related:** `NDVI_RETRY_POLICY_STATUS.md`

---

## Executive Summary

The NDVI pipeline is being modernized in phases to eliminate Redis SPOF, add durable queue semantics, and improve observability. **Phase 1 is complete**, **retry policy hardening (Phase 1.5) is substantially complete**, and **Phase 2 is partially implemented**: dispatch centralization, stream producer code, producer tests, and stream settings are in place; the stream consumer, stream observability, and rollout work are still pending.

**New dependency:** The Redis Streams implementation (Phase 2) should integrate with the hardened retry policy (`ndvi/retry_policy.py`) to ensure stream consumers make correct retry decisions.

---

## Phase 1 - Redis Sentinel (HA Broker/Cache)

**Status:** ✅ **COMPLETE** (March 24 - April 1, 2026)

### What's Implemented:

- ✅ **Redis Sentinel trio deployed**
  - `docker-compose.redis-sentinel.yml` exists with Sentinel configuration
  - Sentinel service name and ports configured
  
- ✅ **Django/Celery Sentinel integration**
  - `config/settings.py`: Full Sentinel URL parsing (`redis-sentinel://` scheme)
  - Converts Sentinel URLs to broker-compatible format for Celery
  - Validates Sentinel scheme and extracts credentials/hosts
  - Settings: `REDIS_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `DJANGO_CACHE_URL`
  
- ✅ **Cache backend Sentinel support**
  - Django cache uses Sentinel-backed Redis
  - Round-trip tested and verified
  
- ✅ **Celery broker Sentinel connection**
  - Broker established as Sentinel-backed connection
  - Result backend initialized as `SentinelBackend`
  - Celery tasks execute successfully through Sentinel
  
- ✅ **Failover validation**
  - Failover drill executed (Apr 1, 2026)
  - Sentinel election observed after master stop
  - Celery survived failover (~54.7s recovery time)
  - Prometheus metrics showed master address change
  
- ✅ **Sentinel metrics in Prometheus**
  - `redis_sentinel_master_status` ✅
  - `redis_sentinel_master_ok_sentinels` ✅
  - `redis_sentinel_master_ok_slaves` ✅
  - `redis_sentinel_masters` ✅
  - `redis_exporter` reports `redis_mode="sentinel"` and `tcp_port="26379"`
  
- ✅ **Test coverage**
  - `tests/test_settings_redis_sentinel.py`: Sentinel settings validation
  - Test validates Celery memory backend compatibility

### What's Left Out:

- ⚠️ **Celery failover latency not acceptable for latency-sensitive workloads**
  - 54.7s delay during failover is acceptable for background jobs only
  - Not acceptable for real-time task dispatch (<10-15s target)
  - **Recommended:** Tune Celery reconnect behavior if needed

---

## Phase 1.5 - NDVI Retry Policy Hardening

**Status:** ✅ **COMPLETE** (April 10, 2026)

### What's Implemented:

- ✅ **`classify_status_code()` — single source of truth**
  - Located in `ndvi/retry_policy.py`.
  - Canonical truth table for HTTP status → retry category mapping.
  - 13 branches covered (400, 401, 403, 422, 429, 500-504, 200, 201, 204, None).

- ✅ **Unified exception hierarchy**
  - All NDVI errors inherit from `UpstreamFailureError`.
  - Consistent `retryable`, `category`, `status_code` attributes.

- ✅ **`should_retry()` — central retry decision function**
  - Returns `RetryDecision(retry, delay, reason)`.
  - Used by Celery task handlers for consistent retry behavior.

- ✅ **Circuit breaker for STAC engine**
  - `_CircuitBreaker` in `ndvi/stac_client.py`.
  - CLOSED/OPEN/HALF_OPEN state machine.
  - Configurable threshold and timeout.

- ✅ **Network error handling fixed**
  - `httpx.RequestError` wrapped after inline retry exhaustion.
  - Celery-level retry now correctly handles transient network errors.

- ✅ **Test coverage: 28 tests**
  - Comprehensive truth table tests.
  - Edge cases for all exception types.

### What's Left Out:

- ⏳ **Circuit breaker for SentinelHub engines** (metrics + raster).
- ⏳ **Extract shared `_CircuitBreaker` to `ndvi/circuit_breaker.py`**.
- ⏳ **Retry-After header parsing for 429 responses**.
- ⏳ **Prometheus metrics for circuit breaker state**.
- ⏳ **Admin endpoint to reset circuit breaker**.

**Full details:** See `NDVI_RETRY_POLICY_STATUS.md`.

---

## Phase 2 - Redis Streams for NDVI

**Status:** 🟡 **PARTIALLY IMPLEMENTED** (Stages 1, 3, and 5 complete; Stage 4 not started)

### Stage 1 - Centralize NDVI Dispatch (100% Complete)

**What's Implemented:**

- ✅ **Dispatch helpers created**
  - `dispatch_ndvi_job()` in `ndvi/services.py`
  - `dispatch_farm_state_coverage()` in `ndvi/services.py`
  
- ✅ **Settings added**
  - `NDVI_QUEUE_BACKEND = "celery"` in `config/settings.py`
  - Default value keeps existing behavior
  - Future value: `"stream"` for Redis Streams

- ✅ **Routing switch implemented**
  - `NDVI_QUEUE_BACKEND` now controls dispatch branching
  - Celery remains the default backend
  - `stream` now publishes into Redis Streams through the producer

- ✅ **Tests exist for helper and routing behavior**
  - Celery routing covered
  - Stream-backed dispatch behavior covered

**What's Left Out:**

- ✅ **All `.delay()` calls replaced**
  - Direct NDVI enqueue call sites route through dispatch helpers
  - No Stage 1 code work remains

---

### Stage 2 - Choose Transport Model (✅ Decided, ❌ Not Implemented)

**Decision Made:**

- ✅ **Separate Redis Streams consumer** (not Celery's built-in stream transport)
- ✅ Architecture documented in implementation plan
- ✅ Rationale documented (Celery/Kombu stream support is open risk)

**What's Left Out:**

- ❌ **No consumer implementation**
  - No `ndvi/management/commands/consume_ndvi_stream.py`
  - No `XREADGROUP` logic
  - No `XACK` logic
  - No `XPENDING` monitoring
  - No `XCLAIM` reclaim logic
  - No dead-letter stream handling

---

### Stage 3 - Stream Producer Logic (100% Complete)

**What's Implemented:**

- ✅ **Stream producer module exists**
  - `ndvi/streams.py` implements the producer path
  - `publish_ndvi_job()` is implemented
  - `publish_farm_state_coverage()` is implemented

- ✅ **Stream payload schema exists**
  - NDVI jobs serialize `job_id`, `request_hash`, farm/owner identifiers,
    engine, job type, params, colormap normalization, and enqueue timestamp
  - Farm state coverage payloads serialize their dedicated fields

- ✅ **Redis stream publish calls exist**
  - Producer uses `XADD`
  - Producer respects `NDVI_STREAM_NAME` and `NDVI_STREAM_MAXLEN`

- ✅ **Dispatch helpers integrate with the producer**
  - `dispatch_ndvi_job()` publishes when `NDVI_QUEUE_BACKEND=stream`
  - `dispatch_farm_state_coverage()` publishes when
    `NDVI_QUEUE_BACKEND=stream`

- ✅ **Producer tests exist**
  - `ndvi/tests/test_ndvi_streams.py` covers payload shape
  - Publish helpers and dispatch integration are tested

**What's Left Out:**

- ✅ Producer-side work is in place
- Remaining gaps for stream mode are consumer, observability, and rollout work

---

### Stage 4 - Stream Consumer Logic (0% Complete)

**What's Left Out:**

- ❌ **No consumer implementation**
  - No Django management command for consumer
  - No consumer group bootstrap (`XGROUP CREATE ... MKSTREAM`)
  - No blocking `XREADGROUP` read loop
  - No payload routing from stream entry to the correct Celery task
  - No `XACK` after successful enqueue
  - No `XPENDING`/`XCLAIM` reclaim path for stale deliveries
  - No poison-message budget or delivery cutoff
  - No dead-letter stream payload contract
  - No `XTRIM` for stream/DLQ trimming
  - No shutdown semantics documented around enqueue-before-ack

---

### Stage 5 - Settings and Feature Flags (100% Complete)

**What's Implemented:**

- ✅ `NDVI_QUEUE_BACKEND`
- ✅ `NDVI_STREAM_NAME`
- ✅ `NDVI_STREAM_GROUP`
- ✅ `NDVI_STREAM_CONSUMER`
- ✅ `NDVI_STREAM_BLOCK_MS`
- ✅ `NDVI_STREAM_CLAIM_IDLE_MS`
- ✅ `NDVI_STREAM_MAXLEN`
- ✅ `NDVI_STREAM_DLQ_NAME`
- ✅ `NDVI_STREAM_DLQ_MAXLEN`
- ✅ Default remains `celery`, so stream mode is opt-in

**What's Left Out:**

- ✅ No Stage 5 settings work remains

---

### Stage 6 - Observability (0% Complete)

**What's Left Out:**

- ❌ **No stream metrics exported**
  - `redis_stream_pending_entries{group="ndvi_stream"}` ❌
  - `redis_stream_pending_age_max` ❌
  - `ndvi_stream_consumer_heartbeat` ❌
  - `ndvi_stream_consumer_failures_total` ❌
  
- ❌ **No Grafana panels for stream lag**
  - Current dashboards don't include stream metrics
  - No alerting for stream lag + Celery failures
  
- ❌ **No Celery histograms for NDVI task runtime**
  - Basic Celery metrics exist but not NDVI-specific

---

### Stage 7 - Tests (35% Complete)

**What's Implemented:**

- ✅ **Producer and dispatch tests exist**
  - `ndvi/tests/test_ndvi_streams.py` exists
  - Producer payload shape is covered
  - Publish helper behavior is covered
  - Stream-backed dispatch helper behavior is covered
  - Default stream setting values are covered

**What's Left Out:**

- ❌ **No consumer test module**
  - No `ndvi/tests/test_ndvi_stream_consumer.py`
  
- ❌ **Missing test coverage:**
  - Consumer enqueue + `XACK`
  - Stale message reclaim via `XPENDING`/`XCLAIM`
  - Dead-letter routing
  - Stream trimming behavior
  - Poison-message budget behavior
  - Feature-flag fallback coverage beyond producer dispatch

---

### Stage 8 - Rollout (0% Complete)

**What's Left Out:**

- ❌ No incremental rollout
- No stream mode enabled for any NDVI workflow
- No production pilot with stream-backed dispatch

---

## Phase 3 - Observability Enhancements

**Status:** 🔴 **NOT STARTED** (0% Complete)

**What's Left Out:**

- ❌ All Phase 3 observability features depend on Phase 2 completion
- ❌ Stream lag panels in Grafana
- ❌ Stream lag alerting
- ❌ NDVI-specific Celery histograms

---

## Phase 4 - Kafka (Future/Conditional)

**Status:** 🔴 **NOT STARTED** (Intentionally Deferred)

**Current State:**

- ✅ Kafka adoption triggers defined in architecture doc
- ✅ Decision to defer Kafka is documented and justified
- ✅ Thresholds established:
  - Stream lag remains high despite adding consumers
  - Need for durable replay across multiple services
  - Demand for partitioned/fan-out consumption

**What's Left Out:**

- ❌ Kafka topics (`ndvi-requests`, `ndvi-results`)
- ❌ Kafka producers/consumers
- ❌ Migration path from Redis Streams to Kafka

---

## Implementation Summary by Phase

| Phase | Status | Completion | Notes |
|-------|--------|------------|-------|
| **Phase 1: Redis Sentinel** | ✅ Complete | **100%** | Sentinel HA for broker/cache/result backend |
| **Phase 1.5: Retry Policy Hardening** | ✅ Complete | **80%** | Policy consolidated, STAC circuit breaker done |
| **Phase 2 Stage 1: Centralize Dispatch** | ✅ Complete | **100%** | Helpers, routing switch, and tests are in place |
| **Phase 2 Stage 2: Transport Model** | 🟡 Decided | **40%** | Architecture chosen; separate consumer still missing |
| **Phase 2 Stage 3: Stream Producer** | ✅ Complete | **100%** | Producer code, payload schema, and tests are in place |
| **Phase 2 Stage 4: Stream Consumer** | 🔴 Not Started | **0%** | No consumer command or reclaim/DLQ loop exists |
| **Phase 2 Stage 5: Settings** | ✅ Complete | **100%** | Stream settings and opt-in defaults are in place |
| **Phase 2 Stage 6: Observability** | 🔴 Not Started | **0%** | No stream metrics exported |
| **Phase 2 Stage 7: Tests** | 🟡 Partial | **35%** | Producer tests exist; consumer tests are missing |
| **Phase 2 Stage 8: Rollout** | 🔴 Not Started | **0%** | No production pilot |
| **Phase 3: Observability** | 🔴 Not Started | **0%** | Merged into Phase 2 Stage 6 |
| **Phase 4: Kafka** | 🔴 Deferred | **0%** | Intentionally deferred until thresholds met |

---

## Current Repo State (Apr 18, 2026)

- Producer code exists in `ndvi/streams.py`
- Producer tests exist in `ndvi/tests/test_ndvi_streams.py`
- Stream settings already exist in `config/settings.py`
- Stream consumer, stream metrics, and rollout work are still missing

---

## Recommended Next Steps

### Immediate (This Week)

1. **Complete Retry Policy Phase 2** (1-2 days)
   - Extract `_CircuitBreaker` to `ndvi/circuit_breaker.py`
   - Add circuit breaker to SentinelHub engines
   - See `NDVI_RETRY_POLICY_STATUS.md` for detailed roadmap

2. **Implement Stream Consumer** (Stage 4, 2-4 days)
   - Create management command for consumer
   - Implement `XGROUP CREATE`, `XREADGROUP`, `XACK`, `XPENDING`, and `XCLAIM`
   - Add DLQ routing and `XTRIM` handling
   - **Important:** Stream consumer should use `should_retry()` from
     `ndvi/retry_policy.py` for consistent retry decisions

### Short-term (2-3 Weeks)

3. **Add observability** (Stage 6)
   - Export stream metrics to Prometheus
   - Add Grafana panels for stream lag
   - Set up alerting rules

4. **Add comprehensive tests** (Stage 7)
   - Consumer tests
   - Reclaim/DLQ tests
   - Trim and poison-message budget tests

### Medium-term (1-2 Months)

5. **Incremental rollout** (Stage 8)
   - Enable stream mode for `enqueue_daily_farm_state_coverage()` first
   - Observe metrics for 1-2 weeks
   - Expand to remaining NDVI paths

6. **Phase 3 observability**
   - Add stream lag panels
   - Add Celery NDVI-specific histograms
   - Set up composite alerting

### Long-term (3-6 Months)

7. **Evaluate Phase 4 triggers**
   - Monitor stream lag trends
   - Assess replay/fan-out requirements
   - Make Kafka decision based on data

---

## Technical Debt & Risks

### Current Risks

1. **Celery failover latency** (Phase 1)
   - 54.7s recovery time may be unacceptable for latency-sensitive tasks
   - **Mitigation:** Tune Celery reconnect or accept delay for background jobs

2. **Producer exists without a consumer** (Phase 2)
   - Dispatch helpers and producer routing are implemented
   - **Risk:** Enabling `NDVI_QUEUE_BACKEND=stream` before deploying a consumer
     will publish work that never drains
   - **Mitigation:** Keep `NDVI_QUEUE_BACKEND=celery` until Stage 4 lands

3. **Separate consumer architecture is chosen but incomplete** (Phase 2)
   - Celery/Kombu Redis Streams support unverified in production
   - **Decision made:** Use separate consumer (lower risk)
   - **Status:** Producer exists; missing consumer still blocks rollout

### Technical Debt

- No stream consumer-related code exists yet
- Producer and settings exist, but the status docs had drifted behind the repo
- Observability and rollout criteria still need implementation

---

## Files That Should Exist (Per Architecture Docs)

### Required Files Not Yet Created:

```
ndvi/management/commands/
  consume_ndvi_stream.py                 # Stream consumer command
ndvi/tests/test_ndvi_stream_consumer.py  # Consumer tests
```

### Files That Exist But Need Updates:

```
ndvi/streams.py                          # Producer is done; consumer compatibility must follow
ndvi/metrics.py                          # Extend with stream metrics
ndvi/services.py                         # Producer routing is done; keep stream mode disabled until consumer ships
docs/architecture/ndvi-phase-2-implementation-plan.md
docs/status/NDVI_PIPELINE_IMPLEMENTATION_STATUS.md
```

---

## Conclusion

**Phase 1 (Redis Sentinel)** is fully implemented and validated in production-like conditions.

**Phase 1.5 (Retry Policy Hardening)** is complete with 80% of planned work done.
The retry policy is now a single source of truth with consistent error handling
across all NDVI engines. Circuit breaker exists for STAC only; SentinelHub
engines remain to be updated.

**Phase 2 (Redis Streams)** has dispatch centralization, producer logic, producer tests, and stream settings in place. The stream consumer, stream observability, and rollout work are still missing.

**Recommended implementation order:**
1. Complete retry policy Phase 2 (circuit breaker expansion) — 1-2 days
2. Implement stream consumer (Stage 4) — 2-4 days
3. Add observability and consumer tests (Stages 6-7) — 2-3 days
4. Incremental rollout (Stage 8) — 1-2 weeks observation
