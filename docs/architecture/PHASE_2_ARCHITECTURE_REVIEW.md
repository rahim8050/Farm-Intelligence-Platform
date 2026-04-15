# Phase 2 Architecture Review & Pre-Implementation Recommendations

**Date:** April 4, 2026  
**Documents Reviewed:**  
- `docs/architecture/ndvi-pipeline-evolution.md`  
- `docs/architecture/ndvi-phase-2-implementation-plan.md`

**Purpose:** Identify missing items, regressions, architectural issues, and document updates needed before commencing Phase 2 implementation.

**Status note:** Stage 1 routing centralization has since been completed in code. The Stage 1 section below is updated to reflect the current state; later sections remain useful for the still-unimplemented stream work.

---

## Executive Summary

The architecture documents are well-structured but have several **critical gaps**, **outdated assumptions**, and **missing context** that must be addressed before Phase 2 implementation begins. Key findings:

1. ✅ **Phase 1 is complete** and validated
2. ✅ **Stage 1 is complete** - routing switch and tests are in place
3. ✅ **Stage 2 is complete** - transport model decision made
4. ✅ **Stage 3 is complete** - stream producer implemented (April 15, 2026)
5. ❌ **Stages 4-8 not started** - consumer, observability, tests, rollout pending
6. ✅ **Resolved: Colormap normalization** - now included in stream payload
7. ⚠️ **Redis Sentinel failover latency** (54.7s) may impact stream consumer design
8. ❌ **Missing: Error handling strategy** for stream → Celery handoff failures (Stage 4)
9. ❌ **Missing: Idempotency guarantees** beyond `request_hash` (Stage 4)

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

### Phase 2 Stage 1 – Centralize NDVI Dispatch ✅ COMPLETE (100%)

**Status:** Helpers exist, call sites migrated, routing switch implemented, and tests cover the behavior

**What's Done:**
- ✅ `dispatch_ndvi_job()` helper created
- ✅ `dispatch_farm_state_coverage()` helper created
- ✅ All 9 call sites migrated from `.delay()` to dispatch helpers
- ✅ `NDVI_QUEUE_BACKEND` setting added
- ✅ `get_ndvi_queue_backend()` helper created and tested
- ✅ Routing switch implemented in both dispatch helpers
- ✅ Tests cover Celery routing and `stream` fallback behavior

**What's Missing:**
1. ❌ **Stream producer implementation**
   - `NDVI_QUEUE_BACKEND=stream` still raises `NotImplementedError`
   - **Fix needed:** Add producer/consumer stack in Stage 3+

**Issues/Regressions:**
- ⚠️ **No regression risk** - current behavior is correct, and the routing boundary is explicit
- ⚠️ **Stream mode remains intentionally unavailable** until the producer exists

**Document Updates Needed:**
```markdown
# Update Stage 1 section in ndvi-phase-2-implementation-plan.md:

### Current Status (as of April 4, 2026)
- ✅ Dispatch helpers implemented
- ✅ All call sites migrated (9 total)
- ✅ NDVI_QUEUE_BACKEND setting added
- ✅ Routing switch implemented
- ✅ Routing tests exist
- ✅ Stage 1 complete

### Remaining Work
- Stage 1 is done; continue with Stage 3+ producer/consumer work.
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

### Phase 2 Stage 3 – Add Stream Producer Logic ✅ COMPLETE (100%)

**Status:** Producer implemented and tested (April 15, 2026)

**What's Done:**
1. ✅ **Stream module** (`ndvi/streams.py`) created
2. ✅ **Stream payload schema** implemented with all 13 fields
3. ✅ **`publish_ndvi_job()` function** implemented
4. ✅ **`publish_farm_state_coverage()` function** implemented
5. ✅ **`colormap_normalization`** field included in payload
6. ✅ **Dispatch helpers updated** to call producer when `NDVI_QUEUE_BACKEND=stream`
7. ✅ **All 8 stream settings** added to `config/settings.py`
8. ✅ **Tests** — 16 new tests in `ndvi/tests/test_ndvi_streams.py`
9. ✅ **Existing tests updated** — 2 tests in `test_ndvi_services.py` no longer expect `NotImplementedError`

**Resolved Issues:**

**Issue 1: Colormap normalization ✅ RESOLVED**
- `colormap_normalization` field included in stream payload via `get_default_colormap_normalization()`
- Payload uses `ColormapNormalization.value` (string: `"histogram"` or `"fixed"`)

**Issue 2: STAC raster fallback improvements ✅ NOTED FOR CONSUMER**
- Error classification still needed for Stage 4 consumer implementation
- Producer correctly passes through job data; consumer handles error routing

**Issue 3: Job model stream metadata fields ✅ DECIDED: Option 1**
- Stream is transient; `NdviJob` does not track stream processing state
- Idempotency handled by existing `UniqueConstraint` on `(owner, farm, engine, request_hash)`

**Implementation Details:**

| Component | File | Lines |
|---|---|---|
| Producer module | `ndvi/streams.py` | 185 |
| Stream tests | `ndvi/tests/test_ndvi_streams.py` | 337 |
| Settings | `config/settings.py` | +8 settings |
| Dispatch updates | `ndvi/services.py` | Updated 2 functions |

**Stream Payload Schema (Implemented):**

```python
{
    "job_id": str,                    # NdviJob.id
    "request_hash": str,              # Idempotency key
    "farm_id": str,                   # Farm reference
    "owner_id": str,                  # Job owner
    "engine": str,                    # "stac" or "sentinelhub"
    "job_type": str,                  # JobType enum value
    "start": str,                     # ISO date or empty string
    "end": str,                       # ISO date or empty string
    "step_days": str,                 # Step days or empty string
    "max_cloud": str,                 # Cloud threshold or empty string
    "lookback_days": str,             # Lookback window or empty string
    "colormap_normalization": str,    # "histogram" or "fixed"
    "enqueue_timestamp": str,         # Unix timestamp
}
```

**Note:** All values are strings (Redis streams require string field values).

**Verification Results:**
- Ruff: 0 errors
- Bandit: No issues identified
- Pytest: 35/35 tests pass (16 stream + 19 service tests)

**Remaining Work:**
- Stage 4: Consumer implementation (XREADGROUP, XACK, XPENDING, XCLAIM, DLQ)
- Stage 5: Additional settings already done; validation at startup still needed
- Stage 6: Observability metrics for stream

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

1. **Update Stage 1 references** (ndvi-phase-2-implementation-plan.md)
   - Mark the routing switch as complete
   - Show the current NotImplementedError fallback for `stream`
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

1. **Stage 1** (already complete)
   - Routing switch implementation
   - Routing tests
   - Dispatch helpers use routing boundary

2. **Stage 2 decision** (already made)
   - Architecture chosen (separate consumer)
   - Rationale documented

3. **Stage 3** (✅ COMPLETE — April 15, 2026)
   - Producer module (`ndvi/streams.py`) implemented
   - All 8 stream settings added to `config/settings.py`
   - Dispatch helpers updated to call producer in stream mode
   - 16 new tests pass
   - Stream payload schema includes `colormap_normalization`

4. **Settings infrastructure** (✅ COMPLETE)
   - All stream settings present with defaults

### What Needs Architecture Updates First 🔴

1. **Error handling strategy** (missing — needed for Stage 4)
   - Must define before implementing consumer
   - ~2 hours to design and document

2. **Idempotency guarantees** (partially specified — needed for Stage 4)
   - Producer relies on DB `UniqueConstraint` for idempotency
   - Consumer needs dedup strategy for XPENDING/XCLAIM
   - ~1 hour to design and document

3. **Consumer retry/backoff for Sentinel failover** (missing — needed for Stage 4)
   - Must handle 55-second Celery unavailability
   - ~1 hour to design and document

### Estimated Time to Stage 4 Implementation-Ready

| Task | Effort | Priority |
|------|--------|----------|
| Error handling matrix | 2 hours | Critical |
| Consumer idempotency strategy | 1 hour | Critical |
| Sentinel failover retry logic | 1 hour | Important |
| **Total** | **~4 hours** | **Before starting Stage 4** |

---

## Recommended Next Steps

### Week 1: Stage 4 Preparation & Consumer Implementation

**Day 1-2:** Update architecture documents for Stage 4
1. Add error handling matrix
2. Add consumer idempotency strategy
3. Add Sentinel failover retry logic
4. Add rollback procedure

**Day 3-5:** Consumer Implementation (Stage 4)
1. Create `ndvi/management/commands/consume_ndvi_stream.py`
2. Implement XREADGROUP loop
3. Implement XACK logic
4. Implement XPENDING/XCLAIM reclaim
5. Implement DLQ handling
6. Implement XTRIM
7. Add consumer tests

### Week 2: Observability & Tests (Stages 5-7)

1. Add remaining settings validation at startup
2. Add Prometheus metrics
3. Add Grafana panels
4. Add comprehensive tests
5. End-to-end integration tests
6. Load testing

### Week 3: Rollout (Stage 8)

1. Enable stream mode for `enqueue_daily_farm_state_coverage()` first
2. Monitor metrics for 1 week
3. Expand to remaining NDVI paths
4. Document lessons learned

---

## Conclusion

The architecture documents provide a solid foundation. **Stage 3 (Stream Producer) is now complete** (April 15, 2026). The remaining critical gaps are:

1. **Error handling strategy** (needed for Stage 4 consumer)
2. **Consumer idempotency guarantees** (needed for Stage 4)
3. **Sentinel failover retry logic** (needed for Stage 4)

**Recommendation:** Spend ~4 hours updating documents for Stage 4 requirements, then begin consumer implementation. The producer is ready, the settings are in place, and the dispatch helpers correctly route to the stream when `NDVI_QUEUE_BACKEND=stream`.

The phased approach is sound, the Sentinel foundation is solid, and the Redis Streams architecture is appropriate for the workload. With Stage 3 complete, Phase 2 implementation is progressing well.
