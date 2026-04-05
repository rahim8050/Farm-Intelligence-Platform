# Phase 2 Architecture Review & Pre-Implementation Recommendations

**Date:** April 4, 2026  
**Documents Reviewed:**  
- `docs/architecture/ndvi-pipeline-evolution.md`  
- `docs/architecture/ndvi-phase-2-implementation-plan.md`

**Purpose:** Identify missing items, regressions, architectural issues, and document updates needed before commencing Phase 2 implementation.

---

## Executive Summary

The architecture documents are well-structured but have several **critical gaps**, **outdated assumptions**, and **missing context** that must be addressed before Phase 2 implementation begins. Key findings:

1. ✅ **Phase 1 is complete** and validated
2. ⚠️ **Stage 1 is 70% complete** - missing routing switch (~1 hour work)
3. ❌ **Stages 2-8 not started** - but architecture has evolved since docs written
4. 🔴 **Critical: Colormap normalization fix changes raster pipeline** - docs don't reflect this
5. 🔴 **Critical: STAC raster fallback improvements** - docs assume old behavior
6. ⚠️ **Redis Sentinel failover latency** (54.7s) may impact stream consumer design
7. ❌ **Missing: Error handling strategy** for stream → Celery handoff failures
8. ❌ **Missing: Idempotency guarantees** beyond `request_hash`

---

## Phase-by-Phase Status & Gaps

### Phase 1 – Redis Sentinel ✅ COMPLETE (100%)

**Status:** Fully implemented and validated

**What's Done:**
- ✅ Sentinel trio deployed
- ✅ Django/Celery Sentinel integration complete
- ✅ Failover drill executed (Apr 1, 2026)
- ✅ Prometheus metrics exported
- ✅ Tests passing

**Issues/Regressions:**
1. ⚠️ **Celery failover latency: 54.7s** (documented in reports/2026-04-01.md)
   - Architecture doc doesn't mention this latency
   - **Impact on Phase 2:** Stream consumer must handle Celery broker unavailability during failover
   - **Recommendation:** Add stream consumer retry/backoff logic for Celery failures during Sentinel failover

2. ⚠️ **Sentinel URL conversion complexity**
   - `config/settings.py` converts `redis-sentinel://` to `sentinel://` for Celery
   - **Risk:** This conversion may break if Celery/Kombu updates URL parsing
   - **Recommendation:** Add test validating URL conversion survives Celery upgrades

**Document Updates Needed:**
```markdown
# Add to Phase 1 section in ndvi-pipeline-evolution.md:

### Known Limitations
- Celery failover recovery time: ~55 seconds (acceptable for background jobs only)
- Not suitable for latency-sensitive task dispatch (<10-15s target)
- If real-time dispatch is required, tune Celery reconnect behavior in future work
```

---

### Phase 2 Stage 1 – Centralize NDVI Dispatch 🟡 70% COMPLETE

**Status:** Helpers exist and call sites migrated, but routing switch missing

**What's Done:**
- ✅ `dispatch_ndvi_job()` helper created
- ✅ `dispatch_farm_state_coverage()` helper created
- ✅ All 9 call sites migrated from `.delay()` to dispatch helpers
- ✅ `NDVI_QUEUE_BACKEND` setting added
- ✅ `get_ndvi_queue_backend()` helper created and tested

**What's Missing:**
1. ❌ **Routing switch in `dispatch_ndvi_job()`**
   - Currently ignores `NDVI_QUEUE_BACKEND` setting
   - Always uses Celery regardless of value
   - **Fix needed:** Add branching logic (see STAGE_1_GAP_ANALYSIS.md)

2. ❌ **Routing switch in `dispatch_farm_state_coverage()`**
   - Same issue as above
   - **Fix needed:** Same pattern

3. ❌ **Tests for routing behavior**
   - No tests verify routing switch works
   - **Need:** 4 tests covering celery/stream routing

**Issues/Regressions:**
- ⚠️ **No regression risk** - current behavior is correct, just incomplete
- ⚠️ **Incomplete abstraction** - dispatch functions claim to be routing boundaries but aren't yet

**Document Updates Needed:**
```markdown
# Update Stage 1 section in ndvi-phase-2-implementation-plan.md:

### Current Status (as of April 4, 2026)
- ✅ Dispatch helpers implemented
- ✅ All call sites migrated (9 total)
- ✅ NDVI_QUEUE_BACKEND setting added
- ❌ Routing switch not implemented (blocks Stage 3+)
- ❌ Routing tests missing

### Remaining Work
- Add routing switch to dispatch_ndvi_job() (~8 lines)
- Add routing switch to dispatch_farm_state_coverage() (~8 lines)
- Add 4 routing tests (~30 lines)
- Estimated effort: ~1 hour
```

---

### Phase 2 Stage 2 – Choose Transport Model ✅ DECIDED (100%)

**Status:** Architecture decision made and documented

**Decision:** Use separate Redis Streams consumer (not Celery's built-in stream transport)

**Rationale Validated:**
- ✅ Avoids Celery/Kombu stream support uncertainty
- ✅ Easier to reason about and observe
- ✅ Preserves current Celery worker model
- ✅ Easier rollback path

**Issues/Regressions:**
- ✅ **No issues** - decision is sound and well-documented

**Document Updates Needed:**
```markdown
# Update Stage 2 section in ndvi-phase-2-implementation-plan.md:

### Decision (Made April 2026)
✅ Chosen: Separate Redis Streams consumer (outside Celery)
✅ Rationale: Celery/Kombu 5.6.2 Redis Streams support unverified in production
✅ Architecture: Producer → Redis Stream → Consumer → Celery Queue → Worker

### Consumer Design Pattern
- Implementation: Django management command
- Location: ndvi/management/commands/consume_ndvi_stream.py
- Consumer group: Configurable via NDVI_STREAM_GROUP setting
- Block timeout: Configurable via NDVI_STREAM_BLOCK_MS setting
```

---

### Phase 2 Stage 3 – Add Stream Producer Logic 🔴 0% COMPLETE

**Status:** No producer code exists

**What's Missing:**
1. ❌ **Stream module** (`ndvi/streams.py` or `ndvi/streaming.py`)
2. ❌ **Stream payload schema** (documented but not implemented)
3. ❌ **`publish_ndvi_job()` function**
4. ❌ **`publish_farm_state_coverage()` function**
5. ❌ **Stream idempotency key implementation**

**Critical Architecture Issues:**

**Issue 1: Colormap normalization fix changes raster pipeline**
- **Recent change:** Commit `722eb27` added `colormap_normalization` to `RasterRequest`
- **Impact:** Stream payload schema must now include `colormap_normalization` field
- **Missing from docs:** Schema doesn't reflect this new field
- **Recommendation:** Update stream payload schema to include:
  ```python
  {
      "job_id": int,
      "request_hash": str,
      "farm_id": int,
      "owner_id": int,
      "engine": str,
      "job_type": str,
      "params": dict,  # serialized params
      "colormap_normalization": str,  # NEW: "histogram" or "fixed"
      "enqueue_timestamp": float,
  }
  ```

**Issue 2: STAC raster fallback improvements not reflected**
- **Recent changes:** Commit `9fb2365` improved STAC raster fallback handling
- **Changes:**
  - Default assets changed: `B04` → `B04_10m`, `B08` → `B08_10m`
  - Candidate ranking added
  - Structured error reporting added
- **Impact:** Stream consumer must handle these new error types
- **Missing from docs:** Error handling strategy doesn't cover structured errors
- **Recommendation:** Add error classification to stream consumer:
  ```python
  # Error types consumer must handle:
  # - no_items: STAC search returned nothing
  # - no_best_item: No items within date window
  # - missing_assets: Items lack required bands
  # - processing_failed: Raster processing error
  # - empty_stats: NDVI computation returned empty
  ```

**Issue 3: Job model may need stream metadata fields**
- **Question:** Should `NdviJob` track stream processing state?
- **Options:**
  1. **No persistence** (simpler): Stream is transient, job tracks state
  2. **Stream metadata on job** (more observable): Add fields like:
     - `stream_entry_id: str` (Redis stream entry ID)
     - `stream_enqueued_at: datetime`
     - `stream_attempts: int`
- **Recommendation:** Option 1 for MVP, Option 2 if observability proves critical

**Document Updates Needed:**
```markdown
# Update Stage 3 section in ndvi-phase-2-implementation-plan.md:

### Stream Payload Schema (Updated April 2026)
The stream entry must contain all fields needed to reconstruct the job:

{
    "job_id": int,                    # NdviJob.id
    "request_hash": str,              # Idempotency key
    "farm_id": int,                   # Farm reference
    "owner_id": int,                  # Job owner
    "engine": str,                    # "stac" or "sentinelhub"
    "job_type": str,                  # JobType enum value
    "start": str | null,              # ISO date or null
    "end": str | null,                # ISO date or null
    "step_days": int | null,          # Raster size or step days
    "max_cloud": int | null,          # Cloud cover threshold
    "lookback_days": int | null,      # Lookback window
    "colormap_normalization": str,    # NEW: "histogram" or "fixed"
    "enqueue_timestamp": float,       # When published to stream
}

### Idempotency Strategy
- Primary: request_hash (existing, unchanged)
- Secondary: Stream entry ID (for deduplication at consumer level)
- Consumer must check request_hash before enqueueing to Celery

### Error Classification
Stream consumer must distinguish error types for proper retry/DLQ routing:
- Transient errors: Retry with backoff (network, STAC timeout)
- Permanent errors: Send to DLQ (missing assets, invalid params)
- Structural errors: Log and alert (schema violations, config errors)
```

---

### Phase 2 Stage 4 – Add Stream Consumer Logic 🔴 0% COMPLETE

**Status:** No consumer code exists

**What's Missing:**
1. ❌ **Management command** (`consume_ndvi_stream.py`)
2. ❌ **`XREADGROUP` implementation**
3. ❌ **`XACK` logic**
4. ❌ **`XPENDING` monitoring**
5. ❌ **`XCLAIM` reclaim logic**
6. ❌ **Dead-letter stream handling**
7. ❌ **`XTRIM` for stream/DLQ**

**Critical Architecture Issues:**

**Issue 1: Consumer must handle Celery broker unavailability**
- **Context:** Sentinel failover takes ~55 seconds
- **Problem:** Consumer may fail to enqueue to Celery during failover
- **Missing from docs:** Retry/backoff strategy for Celery failures
- **Recommendation:** Add consumer retry logic:
  ```python
  # Consumer retry strategy for Celery enqueue failures:
  MAX_CELERY_RETRY_ATTEMPTS = 3
  CELERY_RETRY_BACKOFF = [1, 2, 4]  # seconds
  
  for attempt, delay in enumerate(CELERY_RETRY_BACKOFF, 1):
      try:
          run_ndvi_job.delay(job_id)
          break
      except ConnectionError:
          if attempt == MAX_CELERY_RETRY_ATTEMPTS:
              # Don't XACK - let XPENDING/XCLAIM handle it
              raise
          await asyncio.sleep(delay)
  ```

**Issue 2: No back-pressure strategy defined**
- **Problem:** What happens when stream backlog grows?
- **Missing from docs:** Producer throttling strategy
- **Recommendation:** Add back-pressure handling:
  ```python
  # Monitor XPENDING count
  # If XPENDING > threshold:
  #   1. Log warning
  #   2. Consider pausing producers (return 429 on API)
  #   3. Scale consumer instances (if possible)
  
  PENDING_THRESHOLD_WARNING = 1000
  PENDING_THRESHOLD_CRITICAL = 5000
  ```

**Issue 3: Consumer group initialization strategy**
- **Problem:** What happens on first deploy? Consumer group doesn't exist
- **Missing from docs:** Bootstrap strategy
- **Recommendation:** Add consumer group auto-creation:
  ```python
  # On consumer startup:
  # 1. Try to create consumer group
  # 2. If group exists, join it
  # 3. If stream doesn't exist, create it
  # 4. Handle race condition (multiple consumers starting simultaneously)
  ```

**Issue 4: Graceful shutdown strategy**
- **Problem:** What happens when consumer is stopped mid-processing?
- **Missing from docs:** Shutdown handling
- **Recommendation:** Add graceful shutdown:
  ```python
  # On SIGTERM/SIGINT:
  # 1. Stop accepting new entries
  # 2. Finish processing current entry
  # 3. XACK current entry if successful
  # 4. Exit cleanly
  # 5. Kubernetes/systemd will restart consumer
  ```

**Document Updates Needed:**
```markdown
# Update Stage 4 section in ndvi-phase-2-implementation-plan.md:

### Consumer Error Handling Strategy

#### Celery Enqueue Failures
When consumer fails to enqueue to Celery (e.g., Sentinel failover):
1. Retry up to 3 times with exponential backoff (1s, 2s, 4s)
2. If all retries fail: DO NOT XACK the entry
3. Entry remains pending in stream
4. XPENDING/XCLAIM will reclaim it later
5. Consumer will retry on next read

#### Stream Entry Processing Errors
- Transient errors (network, timeout): Retry 3x, then leave pending
- Permanent errors (invalid data, missing assets): XACK and send to DLQ
- Structural errors (schema violations): XACK, log, and alert

### Back-Pressure Strategy
Monitor XPENDING count and take action at thresholds:
- Warning: XPENDING > 1,000 entries → Log warning
- Critical: XPENDING > 5,000 entries → Consider returning 429 on API
- Emergency: XPENDING > 10,000 entries → Pause producers

### Consumer Group Bootstrap
On consumer startup:
1. Attempt to create consumer group (XGROUP CREATE with MKSTREAM)
2. If group exists, join it
3. If stream doesn't exist, create it
4. Handle race condition: Multiple consumers may try to create group simultaneously
5. Use distributed lock or accept one consumer wins, others join

### Graceful Shutdown
On SIGTERM/SIGINT:
1. Set shutdown flag (stop accepting new entries)
2. Finish processing current entry
3. XACK if successful, leave pending if failed
4. Exit cleanly (exit code 0)
5. Orchestrator (systemd/Kubernetes) restarts consumer
```

---

### Phase 2 Stage 5 – Add Settings and Feature Flags 🟡 12% COMPLETE

**Status:** Only 1 of 8 settings implemented

**What's Done:**
- ✅ `NDVI_QUEUE_BACKEND` setting exists

**What's Missing:**
- ❌ `NDVI_STREAM_NAME`
- ❌ `NDVI_STREAM_GROUP`
- ❌ `NDVI_STREAM_CONSUMER`
- ❌ `NDVI_STREAM_BLOCK_MS`
- ❌ `NDVI_STREAM_CLAIM_IDLE_MS`
- ❌ `NDVI_STREAM_MAXLEN`
- ❌ `NDVI_STREAM_DLQ_NAME`

**Issues/Regressions:**
- ⚠️ **No defaults documented** - What should default values be?
- ⚠️ **No validation** - Settings should be validated at startup
- ⚠️ **No documentation** - What does each setting control?

**Recommendation:** Define all settings with defaults:
```python
# config/settings.py

NDVI_STREAM_NAME = env("NDVI_STREAM_NAME", default="ndvi_jobs")
NDVI_STREAM_GROUP = env("NDVI_STREAM_GROUP", default="ndvi_workers")
NDVI_STREAM_CONSUMER = env("NDVI_STREAM_CONSUMER", default="consumer_1")
NDVI_STREAM_BLOCK_MS = env.int("NDVI_STREAM_BLOCK_MS", default=5000)
NDVI_STREAM_CLAIM_IDLE_MS = env.int("NDVI_STREAM_CLAIM_IDLE_MS", default=30000)
NDVI_STREAM_MAXLEN = env.int("NDVI_STREAM_MAXLEN", default=10000)
NDVI_STREAM_DLQ_NAME = env("NDVI_STREAM_DLQ_NAME", default="ndvi_jobs_dlq")
NDVI_STREAM_DLQ_MAXLEN = env.int("NDVI_STREAM_DLQ_MAXLEN", default=1000)
```

**Document Updates Needed:**
```markdown
# Update Stage 5 section in ndvi-phase-2-implementation-plan.md:

### Settings Reference (April 2026)

| Setting | Default | Description |
|---------|---------|-------------|
| `NDVI_QUEUE_BACKEND` | `"celery"` | Dispatch backend: "celery" or "stream" |
| `NDVI_STREAM_NAME` | `"ndvi_jobs"` | Redis stream name for NDVI jobs |
| `NDVI_STREAM_GROUP` | `"ndvi_workers"` | Consumer group name |
| `NDVI_STREAM_CONSUMER` | `"consumer_1"` | This consumer's identifier |
| `NDVI_STREAM_BLOCK_MS` | `5000` | XREADGROUP block timeout (ms) |
| `NDVI_STREAM_CLAIM_IDLE_MS` | `30000` | Time before entry considered stale (ms) |
| `NDVI_STREAM_MAXLEN` | `10000` | Max stream length before trimming |
| `NDVI_STREAM_DLQ_NAME` | `"ndvi_jobs_dlq"` | Dead-letter stream name |
| `NDVI_STREAM_DLQ_MAXLEN` | `1000` | Max DLQ length before trimming |

### Settings Validation
At Django startup, validate:
- NDVI_QUEUE_BACKEND in ["celery", "stream"]
- NDVI_STREAM_BLOCK_MS > 0
- NDVI_STREAM_CLAIM_IDLE_MS > NDVI_STREAM_BLOCK_MS
- NDVI_STREAM_MAXLEN > 0
- NDVI_STREAM_DLQ_MAXLEN > 0
```

---

### Phase 2 Stage 6 – Add Observability 🔴 0% COMPLETE

**Status:** No stream metrics implemented

**What's Missing:**
1. ❌ `redis_stream_pending_entries{group="ndvi_stream"}`
2. ❌ `redis_stream_pending_age_max`
3. ❌ `ndvi_stream_consumer_heartbeat`
4. ❌ `ndvi_stream_consumer_failures_total`
5. ❌ Celery histograms for NDVI task runtime

**Issues/Regressions:**

**Issue 1: Current Celery metrics insufficient**
- **Problem:** Can't distinguish stream-enqueued jobs from direct Celery jobs
- **Recommendation:** Add tag to Celery tasks:
  ```python
  # When enqueueing from stream:
  run_ndvi_job.apply_async(
      args=[job_id],
      headers={"source": "stream"}
  )
  
  # In metrics:
  celery_task_runtime_seconds{task="run_ndvi_job", source="stream"}
  celery_task_runtime_seconds{task="run_ndvi_job", source="direct"}
  ```

**Issue 2: No Grafana dashboard updates planned**
- **Problem:** Architecture mentions updating Grafana but no plan
- **Recommendation:** Create new dashboard or panels:
  - Stream lag over time
  - Consumer heartbeat status
  - DLQ volume
  - Stream vs direct Celery task comparison

**Document Updates Needed:**
```markdown
# Update Stage 6 section in ndvi-phase-2-implementation-plan.md:

### Metrics Implementation Plan

#### Redis Stream Metrics (Custom)
Export via Django/Python Prometheus client:

```python
from prometheus_client import Gauge, Counter, Histogram

# Stream state
redis_stream_pending_entries = Gauge(
    'redis_stream_pending_entries',
    'Number of pending entries in stream',
    labelnames=['group']
)
redis_stream_pending_age_max = Gauge(
    'redis_stream_pending_age_max',
    'Maximum age of pending entries (seconds)',
    labelnames=['group']
)

# Consumer health
ndvi_stream_consumer_heartbeat = Gauge(
    'ndvi_stream_consumer_heartbeat',
    'Consumer heartbeat timestamp',
    labelnames=['consumer']
)
ndvi_stream_consumer_failures_total = Counter(
    'ndvi_stream_consumer_failures_total',
    'Total consumer failures',
    labelnames=['consumer', 'failure_type']
)

# Task runtime (extend existing Celery metrics)
# Add 'source' label to distinguish stream vs direct
```

#### Grafana Dashboard Updates
Create new panels:
1. Stream Lag: `redis_stream_pending_entries{group="ndvi_stream"}`
2. Consumer Heartbeat: `ndvi_stream_consumer_heartbeat`
3. DLQ Volume: Stream length for DLQ
4. Task Source Comparison: Stream vs direct Celery runtime
5. Alert: Fire when stream lag > threshold AND consumer failures > threshold
```

---

### Phase 2 Stage 7 – Add Tests 🔴 0% COMPLETE

**Status:** No stream tests exist

**What's Missing:**
1. ❌ `ndvi/tests/test_ndvi_streams.py`
2. ❌ `ndvi/tests/test_ndvi_stream_consumer.py`
3. ❌ Producer payload shape tests
4. ❌ Duplicate `request_hash` behavior tests
5. ❌ Consumer enqueue + `XACK` tests
6. ❌ Stale message reclaim tests
7. ❌ Dead-letter routing tests
8. ❌ Stream trimming behavior tests
9. ❌ Feature-flag fallback tests

**Issues/Regressions:**
- ⚠️ **Test infrastructure needed:** Need Redis Streams test fixture
- ⚠️ **Mocking strategy:** How to test without real Redis?
- ⚠️ **Integration tests:** Need end-to-end stream → Celery tests

**Recommendation:** Use pytest fixtures with FakeRedis or testcontainers:
```python
# conftest.py
@pytest.fixture
def redis_stream_client():
    """Provide Redis client connected to test container."""
    # Use testcontainers-redis or FakeRedis
    ...

@pytest.fixture
def stream_producer(redis_stream_client):
    """Provide configured stream producer."""
    ...

@pytest.fixture
def stream_consumer(redis_stream_client):
    """Provide configured stream consumer."""
    ...
```

**Document Updates Needed:**
```markdown
# Update Stage 7 section in ndvi-phase-2-implementation-plan.md:

### Test Infrastructure

#### Redis Streams Test Setup
Option 1: FakeRedis (fast, in-memory)
- Pros: No external dependencies, fast tests
- Cons: May not match real Redis behavior exactly

Option 2: Testcontainers-Redis (accurate, slower)
- Pros: Real Redis behavior, catches integration issues
- Cons: Slower tests, requires Docker

Recommendation: Use FakeRedis for unit tests, testcontainers for integration tests

### Test Plan

#### Unit Tests (test_ndvi_streams.py)
- Producer publishes entry with correct schema
- Producer includes all required fields
- Producer generates unique stream entry ID
- Duplicate request_hash detection works

#### Integration Tests (test_ndvi_stream_consumer.py)
- Consumer reads entry and enqueues to Celery
- Consumer XACKs after successful enqueue
- Consumer retries on Celery failure
- Consumer sends to DLQ after max retries
- XPENDING/XCLAIM reclaims stale entries
- XTRIM trims stream to MAXLEN
- Consumer handles graceful shutdown

#### Feature Flag Tests
- NDVI_QUEUE_BACKEND=celery uses direct dispatch
- NDVI_QUEUE_BACKEND=stream uses stream dispatch
- Invalid backend value raises ValidationError
- Default (unset) uses celery

### Test Execution Order
1. Unit tests first (fast feedback)
2. Integration tests second (catch real issues)
3. Feature flag tests last (verify rollout safety)
```

---

### Phase 2 Stage 8 – Roll Out Incrementally 🔴 0% COMPLETE

**Status:** No rollout started

**What's Missing:**
1. ❌ No stream mode enabled for any workflow
2. ❌ No production pilot
3. ❌ No metrics observation
4. ❌ No expansion to remaining paths

**Issues/Regressions:**
- ⚠️ **Rollout plan assumes Stage 1-7 complete** - can't start until those are done
- ⚠️ **No rollback plan documented** - what if stream mode fails in production?

**Recommendation:** Add explicit rollback procedure:
```markdown
### Rollback Procedure

If stream mode causes issues in production:

1. **Immediate rollback (seconds):**
   ```bash
   # Change setting
   export NDVI_QUEUE_BACKEND=celery
   
   # Restart Django
   systemctl restart weather-apis
   
   # No code deploy needed
   ```

2. **Verify rollback (minutes):**
   - Check Celery queues returning to normal
   - Verify jobs processing successfully
   - Monitor error rates dropping

3. **Post-incident (hours/days):**
   - Analyze root cause
   - Fix stream consumer/producer
   - Test fix in staging
   - Re-attempt rollout
```

**Document Updates Needed:**
```markdown
# Update Stage 8 section in ndvi-phase-2-implementation-plan.md:

### Rollback Strategy

#### Fast Rollback (Settings Change Only)
Stream mode is controlled entirely by NDVI_QUEUE_BACKEND setting.
Rollback requires no code deployment:

1. Set NDVI_QUEUE_BACKEND=celery in environment
2. Restart Django processes
3. All dispatch reverts to direct Celery calls
4. Stream consumer can be stopped independently

#### Rollback Triggers
Rollback immediately if any of these occur:
- Error rate increases > 5% after enabling stream mode
- Stream lag grows continuously (backlog not draining)
- Consumer crash loops (>3 restarts in 10 minutes)
- Celery task failures increase after stream enqueue
- DLQ volume grows faster than 10 entries/minute

### Rollout Checklist
Before enabling stream mode:
- [ ] Stage 1 complete (routing switch implemented)
- [ ] Stage 3 complete (producer implemented and tested)
- [ ] Stage 4 complete (consumer implemented and tested)
- [ ] Stage 5 complete (all settings configured)
- [ ] Stage 6 complete (metrics visible in Grafana)
- [ ] Stage 7 complete (all tests passing)
- [ ] Staging environment validated with stream mode
- [ ] On-call engineer aware of rollback procedure
- [ ] Monitoring alerts configured for stream metrics
```

---

### Phase 3 – Observability 🔴 0% COMPLETE

**Status:** Entirely blocked on Phase 2 completion

**Issues/Regressions:**
- ⚠️ **Phase 3 metrics depend on Phase 6** - circular dependency in docs
- ⚠️ **Grafana updates not scoped** - who creates dashboard updates?

**Recommendation:** Merge Phase 3 into Phase 2 Stage 6 (observability should be part of stream implementation, not separate phase)

**Document Updates Needed:**
```markdown
# Update ndvi-pipeline-evolution.md:

### Phase 3 – Observability
Status: MERGED into Phase 2 Stage 6

Observability is not a separate phase. It must be implemented alongside
the stream producer/consumer to ensure stream adoption is measurable from
day one.

All Phase 3 requirements are now tracked in Phase 2 Stage 6.
```

---

### Phase 4 – Kafka (Future/Conditional) 🔴 0% COMPLETE

**Status:** Intentionally deferred (correct decision)

**What's Documented:**
- ✅ Kafka adoption triggers defined
- ✅ Thresholds established
- ✅ Decision to defer is justified

**Issues/Regressions:**
- ✅ **No issues** - deferring Kafka is the right call

**Document Updates Needed:**
```markdown
# Update Phase 4 section in ndvi-pipeline-evolution.md:

### Current Status (April 2026)
✅ Kafka correctly deferred
✅ Adoption triggers defined and measurable
✅ No immediate need for Kafka-scale infrastructure

### Re-evaluation Schedule
Re-assess Kafka need when:
1. Redis stream lag consistently > 1000 entries for 7+ days
2. Pending age > 5× job runtime for 7+ days
3. Multiple services require NDVI data fan-out
4. Replay requirements exceed Redis Streams capabilities

Earliest re-evaluation date: Q3 2026 (6 months after stream rollout)
```

---

## Critical Missing Elements (Must Add Before Phase 2)

### 1. Error Handling Strategy 🔴 CRITICAL

**Problem:** Documents don't define how stream consumer handles errors when:
- Celery broker unavailable (Sentinel failover)
- STAC API errors (upstream failures)
- Invalid job parameters
- Network partitions

**Recommendation:** Add comprehensive error handling matrix:
```markdown
### Error Handling Matrix

| Error Type | Source | Consumer Action | Retry? | DLQ? |
|------------|--------|-----------------|--------|------|
| Celery connection error | Redis/Sentinel | Leave pending, retry later | Yes (XPENDING) | No |
| STAC timeout | Upstream API | Leave pending, retry later | Yes (XPENDING) | No |
| Invalid job params | Producer | XACK, log error, alert | No | Yes |
| Missing assets | STAC response | XACK, send to DLQ | No | Yes |
| Schema violation | Producer/Consumer | XACK, log, alert | No | Yes |
| Consumer crash | Infrastructure | Leave pending, reclaim | Yes (XCLAIM) | No |
```

### 2. Idempotency Guarantees 🔴 CRITICAL

**Problem:** Documents mention `request_hash` but don't define:
- How consumer prevents duplicate enqueues
- What happens if consumer crashes after enqueue but before XACK
- How to detect and handle duplicate stream entries

**Recommendation:** Add idempotency strategy:
```markdown
### Idempotency Strategy

#### Primary: request_hash (Existing)
- NdviJob model has UniqueConstraint on (owner, farm, engine, request_hash)
- Duplicate jobs with same request_hash are rejected at DB level
- Consumer can safely retry - DB enforces idempotency

#### Secondary: Stream entry ID (New)
- Each stream entry gets unique ID from Redis (XADD returns it)
- Consumer tracks processed entry IDs in local cache (LRU, 10k entries)
- If entry ID seen before, skip processing (already handled)

#### Tertiary: XPENDING deduplication
- Consumer checks XPENDING before processing
- If entry already pending to this consumer, skip (another instance handling it)
- If entry pending to dead consumer, XCLAIM and process

#### Duplicate Detection Flow
1. Read entry from stream (XREADGROUP)
2. Check local cache for entry ID → skip if seen
3. Check XPENDING → skip if already being processed
4. Enqueue to Celery (may fail idempotency check at DB)
5. XACK entry and add to local cache
```

### 3. Stream Retention & Memory Management 🟡 IMPORTANT

**Problem:** Documents mention XTRIM but don't define:
- When to trim (time-based? count-based?)
- What happens to unprocessed entries when trimming
- Memory limits for Redis

**Recommendation:** Add retention policy:
```markdown
### Stream Retention Policy

#### Main Stream
- Trim strategy: MAXLEN ~10000 entries OR 12 hours (whichever first)
- XTRIM called after every successful XACK
- Unprocessed entries: Leave untouched (let XPENDING/XCLAIM handle)
- Memory estimate: ~10k entries × ~500 bytes = ~5MB

#### Dead-Letter Stream
- Trim strategy: MAXLEN ~1000 entries OR 7 days
- XTRIM called after every DLQ entry added
- Manual review required for DLQ entries
- Memory estimate: ~1k entries × ~500 bytes = ~500KB

#### Redis Memory Limits
- Set maxmemory policy: allkeys-lru (evict least recently used)
- Monitor Redis memory usage via redis_exporter
- Alert if Redis memory > 80% of maxmemory
- Stream data is durable (in PostgreSQL via NdviJob), safe to evict if needed
```

### 4. Consumer Scaling Strategy 🟡 IMPORTANT

**Problem:** Documents don't address:
- Can multiple consumers read from same stream?
- How does consumer group partition work?
- What's the scaling limit?

**Recommendation:** Add scaling guidance:
```markdown
### Consumer Scaling

#### Horizontal Scaling
- Multiple consumers can join same consumer group
- Redis automatically partitions entries across consumers
- Each entry delivered to exactly one consumer in group

#### Scaling Limits
- Consumer count limited by stream parallelism (single stream = limited)
- Recommendation: 2-3 consumers max for single stream
- If need more throughput: Partition streams (ndvi_jobs_0, ndvi_jobs_1, etc.)

#### Consumer Deployment
- Deploy as systemd service or Kubernetes deployment
- Run 2 consumer instances for redundancy
- Use distinct NDVI_STREAM_CONSUMER names for each instance
- Monitor both instances via heartbeat metrics

#### When to Scale
- Scale up if: XPENDING > 1000 for > 30 minutes
- Scale down if: XPENDING consistently < 100 for > 24 hours
```

---

## Recommended Document Updates (Priority Order)

### Priority 1: Critical (Must Update Before Implementation)

1. **Add routing switch to Stage 1** (ndvi-phase-2-implementation-plan.md)
   - Document missing routing switch
   - Show implementation pattern
   - Update Stage 1 completion status

2. **Add error handling matrix** (both docs)
   - Define error types and handling strategy
   - Specify retry/DLQ behavior for each error type

3. **Update stream payload schema** (ndvi-phase-2-implementation-plan.md)
   - Add `colormap_normalization` field
   - Document all fields with types and examples
   - Note recent changes (STAC fallback improvements)

4. **Add idempotency strategy** (ndvi-phase-2-implementation-plan.md)
   - Define 3-tier idempotency (request_hash, entry ID, XPENDING)
   - Document duplicate detection flow
   - Explain how consumer prevents double-enqueue

### Priority 2: Important (Should Update Before Implementation)

5. **Add consumer error handling for Sentinel failover** (ndvi-phase-2-implementation-plan.md)
   - Document 55-second failover window
   - Add retry/backoff strategy
   - Define behavior during Celery broker unavailability

6. **Add settings reference table** (ndvi-phase-2-implementation-plan.md)
   - List all 9 settings with defaults
   - Document validation rules
   - Explain what each setting controls

7. **Add rollback procedure** (both docs)
   - Document fast rollback (settings change)
   - Define rollback triggers
   - Add rollout checklist

8. **Merge Phase 3 into Phase 2 Stage 6** (ndvi-pipeline-evolution.md)
   - Observability should not be separate phase
   - Update phase numbering or merge content

### Priority 3: Nice to Have (Update During Implementation)

9. **Add Phase 1 known limitations** (ndvi-pipeline-evolution.md)
   - Document 55s failover latency
   - Note it's acceptable for background jobs only

10. **Add Grafana dashboard plan** (ndvi-phase-2-implementation-plan.md)
    - List specific panels needed
    - Define alert thresholds
    - Document dashboard structure

11. **Add test infrastructure plan** (ndvi-phase-2-implementation-plan.md)
    - Choose FakeRedis vs testcontainers
    - Define test execution order
    - List required test fixtures

12. **Add Kafka re-evaluation schedule** (ndvi-pipeline-evolution.md)
    - Document when to reassess Kafka need
    - List measurable thresholds
    - Set earliest re-evaluation date

---

## Implementation Readiness Assessment

### What's Ready to Implement ✅

1. **Stage 1 completion** (1 hour work)
   - Routing switch implementation
   - Routing tests
   - Can start immediately

2. **Stage 2 decision** (already made)
   - Architecture chosen (separate consumer)
   - Can proceed with implementation

3. **Settings infrastructure** (partially done)
   - NDVI_QUEUE_BACKEND exists
   - Can add remaining 8 settings easily

### What Needs Architecture Updates First 🔴

1. **Error handling strategy** (missing)
   - Must define before implementing consumer
   - ~2 hours to design and document

2. **Idempotency guarantees** (underspecified)
   - Must define before implementing producer
   - ~1 hour to design and document

3. **Stream payload schema** (outdated)
   - Must update with colormap_normalization field
   - ~30 minutes to update

4. **Consumer retry/backoff for Sentinel failover** (missing)
   - Must handle 55-second Celery unavailability
   - ~1 hour to design and document

### Estimated Time to Implementation-Ready

| Task | Effort | Priority |
|------|--------|----------|
| Update documents (Priority 1 items) | 4 hours | Critical |
| Update documents (Priority 2 items) | 3 hours | Important |
| Complete Stage 1 (routing switch) | 1 hour | Critical |
| Add remaining settings | 30 minutes | Important |
| **Total** | **~8.5 hours** | **Before starting Stages 3-4** |

---

## Recommended Next Steps

### Week 1: Documentation & Stage 1 Completion

**Day 1-2:** Update architecture documents
1. Add error handling matrix
2. Update stream payload schema
3. Add idempotency strategy
4. Add settings reference table
5. Add rollback procedure
6. Merge Phase 3 into Phase 2 Stage 6

**Day 3:** Complete Stage 1
1. Add routing switch to `dispatch_ndvi_job()`
2. Add routing switch to `dispatch_farm_state_coverage()`
3. Add 4 routing tests
4. Run full test suite

**Day 4-5:** Add remaining settings
1. Add 7 missing settings to `config/settings.py`
2. Add settings validation at startup
3. Document settings in code comments
4. Add settings tests

### Week 2: Producer Implementation (Stage 3)

1. Create `ndvi/streams.py`
2. Implement `publish_ndvi_job()`
3. Implement `publish_farm_state_coverage()`
4. Add producer tests
5. Update dispatch helpers to call producer when `NDVI_QUEUE_BACKEND=stream`

### Week 3: Consumer Implementation (Stage 4)

1. Create `ndvi/management/commands/consume_ndvi_stream.py`
2. Implement XREADGROUP loop
3. Implement XACK logic
4. Implement XPENDING/XCLAIM reclaim
5. Implement DLQ handling
6. Implement XTRIM
7. Add consumer tests

### Week 4: Observability & Tests (Stages 6-7)

1. Add Prometheus metrics
2. Add Grafana panels
3. Add comprehensive tests
4. End-to-end integration tests
5. Load testing

### Week 5: Rollout (Stage 8)

1. Enable stream mode for `enqueue_daily_farm_state_coverage()` first
2. Monitor metrics for 1 week
3. Expand to remaining NDVI paths
4. Document lessons learned

---

## Conclusion

The architecture documents provide a solid foundation but require **~8.5 hours of updates** before Phase 2 implementation should begin. The critical gaps are:

1. **Error handling strategy** (completely missing)
2. **Idempotency guarantees** (underspecified)
3. **Stream payload schema** (outdated - missing colormap_normalization)
4. **Stage 1 routing switch** (not implemented)

**Recommendation:** Spend 1-2 days updating documents and completing Stage 1 before starting Stage 3 (producer) implementation. This prevents costly rework and ensures the implementation is based on complete, accurate specifications.

The phased approach is sound, the Sentinel foundation is solid, and the Redis Streams architecture is appropriate for the workload. With these document updates and Stage 1 completion, Phase 2 implementation will be well-guided and low-risk.
