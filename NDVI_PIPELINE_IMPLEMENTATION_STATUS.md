# NDVI Pipeline Evolution - Implementation Status Report

**Date:** April 4, 2026  
**Architecture Document:** `docs/architecture/ndvi-pipeline-evolution.md`  
**Implementation Plan:** `docs/architecture/ndvi-phase-2-implementation-plan.md`

---

## Executive Summary

The NDVI pipeline is being modernized in phases to eliminate Redis SPOF, add durable queue semantics, and improve observability. **Phase 1 is complete**, **Phase 2 Stage 1 is partially complete**, and **Phases 2-4 remain largely unimplemented**.

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

## Phase 2 - Redis Streams for NDVI

**Status:** 🟡 **PARTIALLY IMPLEMENTED** (Stage 1 only, ~10% complete)

### Stage 1 - Centralize NDVI Dispatch (50% Complete)

**What's Implemented:**

- ✅ **Dispatch helpers created**
  - `dispatch_ndvi_job()` in `ndvi/services.py`
  - `dispatch_farm_state_coverage()` in `ndvi/services.py`
  
- ✅ **Settings added**
  - `NDVI_QUEUE_BACKEND = "celery"` in `config/settings.py`
  - Default value keeps existing behavior
  - Future value: `"stream"` for Redis Streams
  
- ✅ **Code comments document intent**
  - Dispatch helpers have comments noting they're routing boundaries for future Streams work
  - Stage 1 explicitly preserves existing Celery path

**What's Left Out:**

- ❌ **Not all `.delay()` calls replaced**
  - Some views/tasks still call `.delay(...)` directly
  - Need to replace all direct Celery calls with dispatch helpers
  
- ❌ **No routing switch implemented**
  - `NDVI_QUEUE_BACKEND` setting exists but not used for branching logic
  - Currently always uses Celery regardless of setting value

---

### Stage 2 - Choose Transport Model (✅ Decided, ❌ Not Implemented)

**Decision Made:**

- ✅ **Separate Redis Streams consumer** (not Celery's built-in stream transport)
- ✅ Architecture documented in implementation plan
- ✅ Rationale documented (Celery/Kombu stream support is open risk)

**What's Left Out:**

- ❌ **No consumer implementation**
  - No `ndvi/streams.py` or `ndvi/streaming.py`
  - No `ndvi/management/commands/consume_ndvi_stream.py`
  - No `XREADGROUP` logic
  - No `XACK` logic
  - No `XPENDING` monitoring
  - No `XCLAIM` reclaim logic
  - No dead-letter stream handling

---

### Stage 3 - Stream Producer Logic (0% Complete)

**What's Left Out:**

- ❌ **No stream producer**
  - No `publish_ndvi_job()` function
  - No `publish_farm_state_coverage()` function
  - No stream payload schema defined
  - No `XADD` calls in codebase
  
- ❌ **No idempotency key usage**
  - `request_hash` exists but not used as stream idempotency key
  - No duplicate detection at stream level

---

### Stage 4 - Stream Consumer Logic (0% Complete)

**What's Left Out:**

- ❌ **No consumer implementation**
  - No Django management command for consumer
  - No consumer group creation
  - No blocking `XREADGROUP` reads
  - No `XACK` after successful processing
  - No `XPENDING` detection for stuck deliveries
  - No `XCLAIM` for stale entry reclaim
  - No dead-letter stream
  - No `XTRIM` for stream/DLQ trimming

---

### Stage 5 - Settings and Feature Flags (20% Complete)

**What's Implemented:**

- ✅ `NDVI_QUEUE_BACKEND` setting exists

**What's Left Out:**

- ❌ Missing settings:
  - `NDVI_STREAM_NAME`
  - `NDVI_STREAM_GROUP`
  - `NDVI_STREAM_CONSUMER`
  - `NDVI_STREAM_BLOCK_MS`
  - `NDVI_STREAM_CLAIM_IDLE_MS`
  - `NDVI_STREAM_MAXLEN`
  - `NDVI_STREAM_DLQ_NAME`

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

### Stage 7 - Tests (0% Complete)

**What's Left Out:**

- ❌ **No stream tests**
  - No `ndvi/tests/test_ndvi_streams.py`
  - No `ndvi/tests/test_ndvi_stream_consumer.py`
  
- ❌ **Missing test coverage:**
  - Producer payload shape
  - Duplicate `request_hash` behavior
  - Consumer enqueue + `XACK`
  - Stale message reclaim via `XPENDING`/`XCLAIM`
  - Dead-letter routing
  - Stream trimming behavior
  - Feature-flag fallback to plain Celery dispatch

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
| **Phase 2 Stage 1: Centralize Dispatch** | 🟡 Partial | **50%** | Helpers exist but not fully adopted |
| **Phase 2 Stage 2: Transport Model** | 🟡 Decided | **10%** | Architecture chosen, not implemented |
| **Phase 2 Stage 3: Stream Producer** | 🔴 Not Started | **0%** | No producer code exists |
| **Phase 2 Stage 4: Stream Consumer** | 🔴 Not Started | **0%** | No consumer code exists |
| **Phase 2 Stage 5: Settings** | 🔴 Minimal | **20%** | Only 1 of 8 settings added |
| **Phase 2 Stage 6: Observability** | 🔴 Not Started | **0%** | No stream metrics exported |
| **Phase 2 Stage 7: Tests** | 🔴 Not Started | **0%** | No stream tests exist |
| **Phase 2 Stage 8: Rollout** | 🔴 Not Started | **0%** | No production pilot |
| **Phase 3: Observability** | 🔴 Not Started | **0%** | Blocked on Phase 2 |
| **Phase 4: Kafka** | 🔴 Deferred | **0%** | Intentionally deferred until thresholds met |

---

## What's Going to Production Today (Apr 4, 2026)

**Recent commit:** `722eb27` - NDVI colormap normalization fix

**Note:** This commit is **unrelated to the NDVI pipeline evolution phases**. It fixes the green PNG visualization issue but doesn't advance Phase 2 implementation.

---

## Recommended Next Steps

### Immediate (This Week)

1. **Complete Stage 1** (1-2 days)
   - Replace all remaining `.delay()` calls with `dispatch_ndvi_job()`
   - Implement routing switch based on `NDVI_QUEUE_BACKEND`
   - Add tests proving no behavior change with `celery` backend

2. **Implement Stage 3-4** (3-5 days)
   - Create `ndvi/streams.py` with producer logic
   - Create management command for consumer
   - Implement basic `XREADGROUP` + `XACK` loop
   - Add DLQ and `XTRIM` handling

### Short-term (2-3 Weeks)

3. **Add settings and feature flags** (Stage 5)
   - Add all 7 missing stream settings
   - Make stream mode opt-in via feature flag

4. **Add observability** (Stage 6)
   - Export stream metrics to Prometheus
   - Add Grafana panels for stream lag
   - Set up alerting rules

5. **Add comprehensive tests** (Stage 7)
   - Producer/consumer tests
   - Reclaim/DLQ tests
   - Feature flag fallback tests

### Medium-term (1-2 Months)

6. **Incremental rollout** (Stage 8)
   - Enable stream mode for `enqueue_daily_farm_state_coverage()` first
   - Observe metrics for 1-2 weeks
   - Expand to remaining NDVI paths

7. **Phase 3 observability**
   - Add stream lag panels
   - Add Celery NDVI-specific histograms
   - Set up composite alerting

### Long-term (3-6 Months)

8. **Evaluate Phase 4 triggers**
   - Monitor stream lag trends
   - Assess replay/fan-out requirements
   - Make Kafka decision based on data

---

## Technical Debt & Risks

### Current Risks

1. **Celery failover latency** (Phase 1)
   - 54.7s recovery time may be unacceptable for latency-sensitive tasks
   - **Mitigation:** Tune Celery reconnect or accept delay for background jobs

2. **Partial dispatch centralization** (Phase 2 Stage 1)
   - Some code paths still use direct `.delay()` calls
   - **Risk:** Inconsistent behavior when stream mode is enabled
   - **Mitigation:** Complete Stage 1 before enabling stream mode

3. **Open architectural question** (Phase 2)
   - Celery/Kombu Redis Streams support unverified in production
   - **Decision made:** Use separate consumer (lower risk)
   - **Status:** Not yet implemented, so risk is theoretical

### Technical Debt

- No stream-related code exists yet (producer/consumer/management command)
- Phase 2 implementation plan exists but no code follows it
- Architecture docs are comprehensive but implementation is far behind

---

## Files That Should Exist (Per Architecture Docs)

### Required Files Not Yet Created:

```
ndvi/streams.py                          # Stream producer logic
ndvi/management/commands/
  consume_ndvi_stream.py                 # Stream consumer command
ndvi/tests/test_ndvi_streams.py          # Producer tests
ndvi/tests/test_ndvi_stream_consumer.py  # Consumer tests
ndvi/metrics.py                          # Stream metrics (extend existing)
config/settings.py                       # Add 7 stream settings
```

### Files That Exist But Need Updates:

```
ndvi/services.py                         # Complete dispatch centralization
ndvi/views.py                            # Use dispatch_ndvi_job() everywhere
ndvi/tasks.py                            # Use dispatch helpers
```

---

## Conclusion

**Phase 1 (Redis Sentinel)** is fully implemented and validated in production-like conditions.

**Phase 2 (Redis Streams)** has only Stage 1 partially complete (~10% overall). The foundational work (dispatch helpers, settings) exists but is not fully adopted. The core stream producer/consumer logic, observability, and tests are entirely missing.

**Priority:** Complete Phase 2 Stage 1 fully, then implement Stages 3-4 (producer/consumer) before adding observability and tests. The architecture documentation is excellent—now implementation needs to catch up.
