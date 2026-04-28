# NDVI Phase 2 Implementation Plan: Redis Streams

This document turns the Phase 2 section of
`docs/architecture/ndvi-pipeline-evolution.md` into a concrete
implementation plan. The goal is to introduce Redis Streams for NDVI
ingestion without rewriting the rest of the async stack or destabilizing the
 existing Celery-based workflow.

For the consolidated NDVI architecture and implementation spec, see
`docs/architecture/ndvi-system-evolution-phased-spec.md`.

## Objective

Phase 2 should give NDVI ingestion durable, observable queue semantics while
retaining Celery for the actual job execution path. The lowest-risk design is
to add a Redis Streams producer/consumer layer for NDVI dispatch and keep the
worker execution model unchanged.

## Current baseline

- Redis Sentinel for cache/broker/result backend is already implemented.
- NDVI job creation already has deterministic idempotency via `request_hash`
  in `ndvi/services.py`.
- NDVI dispatch is centralized through `dispatch_ndvi_job()` and
  `dispatch_farm_state_coverage()`.
- All NDVI enqueue call sites route through those helpers.
- `NDVI_QUEUE_BACKEND` now controls the dispatch boundary.
- Stream producer logic already exists in `ndvi/streams.py`, and the dispatch
  helpers publish to Redis Streams when `NDVI_QUEUE_BACKEND=stream`.
- Stream settings already exist in `config/settings.py`.
- No Redis Streams consumer code exists yet:
  - no Django management command for stream consumption
  - no `XREADGROUP`
  - no `XACK`
  - no `XPENDING`
  - no `XCLAIM`
  - no `XTRIM`
  - no dead-letter stream routing

## Stage 1 - Centralize NDVI dispatch

### Goal

Create a single dispatch boundary for NDVI-related async work before adding any
Redis Streams behavior.

### Current Status (as of April 18, 2026)

- ✅ Dispatch helpers implemented (`dispatch_ndvi_job`, `dispatch_farm_state_coverage`)
- ✅ All 9 call sites migrated from direct `.delay()` to dispatch helpers
- ✅ `NDVI_QUEUE_BACKEND` setting added with `get_ndvi_queue_backend()` helper
- ✅ Routing switch implemented in both dispatch helpers
- ✅ Tests cover Celery routing and stream-backed dispatch behavior
- ✅ Stage 1 is complete

### Work

- No Stage 1 code work remains.
- The dispatch boundary already exists and routes to Celery by default.
- `NDVI_QUEUE_BACKEND=stream` now routes to the producer and publishes into
  Redis Streams.

### File targets

- `ndvi/services.py`
- `ndvi/views.py`
- `ndvi/tasks.py`
- `config/settings.py`

### Expected outcome

- Default runtime behavior remains unchanged because Celery is still the
  default backend.
- All NDVI enqueue behavior flows through one place.
- Future Redis Streams logic can be added without editing every call site.
- Stage 1 is complete; remaining work starts at Stage 3.

## Stage 2 - Choose the transport model

### Goal

Resolve the architecture question before implementing any stream semantics.

### Preferred model

Use a separate Redis Streams consumer rather than relying directly on Celery's
Redis Streams transport.

### Why

- The architecture note explicitly treats Celery/Kombu stream support as an
  open risk.
- A separate consumer is easier to reason about, easier to observe, and easier
  to roll back.
- It preserves the current Celery worker model.

### Resulting flow

- Producer writes NDVI work to a Redis stream.
- Consumer reads entries from the stream.
- Consumer enqueues normal Celery work.
- Consumer acknowledges the stream entry after enqueue succeeds.

## Stage 3 - Add stream producer logic

### Goal

Publish NDVI jobs into a dedicated stream using deterministic identifiers.

### Current Status (as of April 18, 2026)

- ✅ `ndvi/streams.py` exists with producer helpers
- ✅ `publish_ndvi_job(job: NdviJob) -> str` is implemented
- ✅ `publish_farm_state_coverage(...) -> str` is implemented
- ✅ Stream payload schema is defined and covered by tests
- ✅ Dispatch helpers call the producer when `NDVI_QUEUE_BACKEND=stream`
- ✅ Stage 3 is complete

### Implemented work

- Producer module:
  - `ndvi/streams.py`
- Stream payload schema contains:
  - `job_id`
  - `request_hash`
  - `farm_id`
  - `owner_id`
  - `engine`
  - `job_type`
  - serialized params
  - enqueue timestamp
  - `colormap_normalization` (added April 2026: "histogram" or "fixed")
- `request_hash` from `enqueue_job(...)` remains the idempotency key.
- Producer helpers:
  - `publish_ndvi_job(job: NdviJob) -> str`
  - `publish_farm_state_coverage(...) -> str`

### Stream Payload Schema (Updated April 2026)

```python
{
    "job_id": int,                    # NdviJob.id
    "request_hash": str,              # Idempotency key
    "farm_id": int,                   # Farm reference
    "owner_id": int,                  # Job owner
    "engine": str,                    # "stac" or "sentinelhub"
    "job_type": str,                  # JobType enum value
    "start": str | None,              # ISO date or null
    "end": str | None,                # ISO date or null
    "step_days": int | None,          # Raster size or step days
    "max_cloud": int | None,          # Cloud cover threshold
    "lookback_days": int | None,      # Lookback window
    "colormap_normalization": str,    # "histogram" or "fixed" (added Apr 2026)
    "enqueue_timestamp": float,       # When published to stream
}
```

### File targets

- `ndvi/services.py`
- `ndvi/streams.py`
- `ndvi/models.py` only if explicit persistence metadata is needed

### Expected outcome

- NDVI jobs can be published into Redis Streams deterministically.
- Duplicate logical jobs are still governed by the existing `request_hash`
  behavior.

## Stage 4 - Add stream consumer logic

### Goal

Consume stream entries safely and enqueue normal Celery tasks.

### Current Status (as of April 21, 2026)

- ✅ Stage 4 is complete.
- ✅ `consume_ndvi_stream` command implemented and operational.
- ✅ Reliable stream consumption via `XREADGROUP` and `XAUTOCLAIM`.
- ✅ DLQ implemented with full metadata and strict delivery budgets.
- ✅ Execution-level idempotency implemented using token-based distributed Redis locking and DB state checks.
- ✅ Graceful shutdown and stream trimming operational.

### Completion Notes
- **Reliability:** The implementation achieves effectively-once execution semantics by combining at-least-once delivery (Redis Streams with `XACK` and `XAUTOCLAIM`) with idempotent task execution (unique `request_hash`, distributed Redis locking, and DB status checks).
- **Tradeoffs:** Manual lock release is token-based to avoid blind deletion; however, it relies on TTL expiry for recovery in the event of an abnormal consumer exit during lock release.

### Processing lifecycle

Message states:

- `new`: never delivered to any consumer.
- `pending`: delivered by group read, not yet acknowledged.
- `reclaimed`: pending on one consumer, then taken over by another.
- `acknowledged`: terminal success state after `XACK`.
- `dead-lettered`: terminal failure state after DLQ write + `XACK`.

State transitions:

- `XADD` -> `new`
- `XREADGROUP` -> `pending`
- `XPENDING` + `XCLAIM` -> `reclaimed`
- `XACK` -> `acknowledged`
- DLQ write + `XACK` -> `dead-lettered`

### Delivery guarantees

- Delivery is **at-least-once**.
- Duplicate processing is prevented by DB and distributed locking.
- Correctness depends on downstream idempotency keyed by `request_hash`.
- Exactly-once delivery is **not** guaranteed, but effectively-once execution is enforced.
- Ordering is **not** guaranteed across consumers.

### Multi-consumer model

- Multiple consumers are supported within the same consumer group using unique identities (hostname-pid).
- Cooperative distribution is used; no static partitioning.
- Reclaim is used to recover from dead or stalled consumers.

### Work

- Created `ndvi/management/commands/consume_ndvi_stream.py`.
- Implemented consumer-group reads, staleness detection with `XPENDING`/`XCLAIM`, task routing, and `XACK` management.
- Implemented DLQ with full metadata tracking.
- Implemented stream trimming with safe approximate limits.

### Idempotency implementation (April 2026 update)

- Execution-level idempotency is enforced via:
  - Token-based distributed Redis locking (`acquire_lock` + `release_lock` via atomic Lua script).
  - DB state refresh and terminal status checking (`job.status == SUCCESS`) before execution.
  - No blind lock deletions; release is guarded by token equality.

### Error handling strategy

- **Transient errors:** Retryable task failures handled via shared retry policy.
- **Permanent errors:** Dead-letter routing after retry budget exhaustion.
- **Structural errors:** Logged as alerts and dead-lettered.
- **Poison messages:** Delivery ceiling (`NDVI_STREAM_MAX_DELIVERIES`) strictly enforced, followed by DLQ routing and `XACK`.

### Observability

- Consumer supports processing rate, pending count, reclaim count, and DLQ rate monitoring.
- All consumer actions are logged with structural metadata (message_id, delivery_count, job_type).

### Expected outcome

- NDVI ingestion is durable and effectively-once.
- Recovery is deterministic.
- Rollback to plain Celery is a configuration toggle.


## Stage 5 - Add settings and feature flags

### Goal

Keep rollout reversible and configurable.

### Current Status (as of April 18, 2026)

- ✅ All stream-related settings listed below exist in `config/settings.py`
- ✅ `NDVI_QUEUE_BACKEND` still defaults to `celery`
- ✅ Stream mode remains opt-in
- ✅ Stage 5 is complete

### Implemented settings

Settings now present in `config/settings.py`:

- `NDVI_QUEUE_BACKEND` (✅ already added)
- `NDVI_STREAM_NAME`
- `NDVI_STREAM_GROUP`
- `NDVI_STREAM_CONSUMER`
- `NDVI_STREAM_BLOCK_MS`
- `NDVI_STREAM_CLAIM_IDLE_MS`
- `NDVI_STREAM_MAXLEN`
- `NDVI_STREAM_DLQ_NAME`
- `NDVI_STREAM_DLQ_MAXLEN`
- `NDVI_STREAM_BATCH_SIZE`
- `NDVI_STREAM_MAX_DELIVERIES`
- `NDVI_STREAM_START_ID`
- `NDVI_STREAM_RECLAIM_INTERVAL_SECONDS`

### Settings Reference Table (Updated April 2026)

| Setting | Default | Description |
|---------|---------|-------------|
| `NDVI_QUEUE_BACKEND` | `"celery"` | Dispatch backend: "celery" or "stream" |
| `NDVI_STREAM_NAME` | `"ndvi:stream"` | Redis stream name for NDVI jobs |
| `NDVI_STREAM_GROUP` | `"ndvi-group"` | Consumer group name |
| `NDVI_STREAM_CONSUMER` | `"consumer_1"` | This consumer's identifier |
| `NDVI_STREAM_BLOCK_MS` | `5000` | XREADGROUP block timeout (ms) |
| `NDVI_STREAM_CLAIM_IDLE_MS` | `60000` | Time before entry is considered stale (ms) |
| `NDVI_STREAM_MAXLEN` | `10000` | Max stream length before trimming |
| `NDVI_STREAM_DLQ_NAME` | `"ndvi:dlq"` | Dead-letter stream name |
| `NDVI_STREAM_DLQ_MAXLEN` | `10000` | Max DLQ length before trimming |
| `NDVI_STREAM_BATCH_SIZE` | `10` | Max messages per read or reclaim batch |
| `NDVI_STREAM_MAX_DELIVERIES` | `5` | Delivery ceiling before DLQ routing |
| `NDVI_STREAM_START_ID` | `"0"` | Consumer group start ID |
| `NDVI_STREAM_RECLAIM_INTERVAL_SECONDS` | `60` | Minimum seconds between reclaim passes |

### Defaults

- Keep `NDVI_QUEUE_BACKEND=celery` by default.
- Make stream mode opt-in until verified in production-like conditions.

### Expected outcome

- Rollout and rollback are controlled entirely through settings.
- The repo can support both list-backed and stream-backed NDVI dispatch during
  transition.

## Stage 6 - Add observability with the implementation

### Goal

Make the new queue path measurable from the first rollout.

### Work

Add the metrics already named in the architecture document:

- `redis_stream_pending_entries{group="ndvi_stream"}`
- `redis_stream_pending_age_max`
- `ndvi_stream_consumer_heartbeat`
- `ndvi_stream_consumer_failures_total`

Also continue using existing Celery runtime metrics to measure execution after
enqueue.

### File targets

- `ndvi/metrics.py`
- stream consumer module
- Grafana dashboard and Prometheus alert definitions later in rollout

### Expected outcome

- Queue lag, stale claims, consumer liveness, and failure behavior are visible.
- Stream adoption can be judged by data rather than guesswork.

## Stage 7 - Add tests before enabling stream mode

### Goal

Verify correctness before rollout.

### Current Status (as of April 18, 2026)

- ✅ `ndvi/tests/test_ndvi_streams.py` exists
- ✅ Producer payload, publish helpers, dispatch integration, and default
  stream settings are covered
- ❌ Consumer-specific tests do not exist yet

### Work

Add the remaining tests for:

- duplicate `request_hash` behavior
- consumer enqueue + `XACK`
- stale message reclaim via `XPENDING`/`XCLAIM`
- dead-letter routing
- stream trimming behavior
- feature-flag fallback to plain Celery dispatch

### Suggested test files

- `ndvi/tests/test_ndvi_streams.py`
- `ndvi/tests/test_ndvi_stream_consumer.py`
- extend `ndvi/tests/test_ndvi_tasks_extra.py`
- extend `ndvi/tests/test_ndvi.py`

### Expected outcome

- Stream behavior is covered before rollout.
- Regressions in dispatch semantics are caught without needing manual drills.

## Stage 8 - Roll out incrementally

### Goal

Reduce risk while introducing stream-backed NDVI dispatch.

### Rollout order

1. Stage 1 is already merged with no behavior change.
2. Merge producer/consumer code behind a disabled flag.
3. Enable stream mode for one NDVI workflow only.
4. Observe lag, claims, DLQ volume, and Celery runtime metrics.
5. Expand to the remaining NDVI dispatch paths.

### Best first candidate

Start with one predictable background workflow rather than every NDVI endpoint
at once. Good candidates:

- `enqueue_daily_farm_state_coverage()` in `ndvi/tasks.py`
- `enqueue_weekly_gap_fill()` in `ndvi/tasks.py`

These are safer than immediately moving every user-triggered NDVI path.

### Rollback Strategy (Added April 2026)

#### Fast Rollback (Settings Change Only)
Stream mode is controlled entirely by `NDVI_QUEUE_BACKEND` setting.
Rollback requires no code deployment:

1. Set `NDVI_QUEUE_BACKEND=celery` in environment
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

## Recommended first patch set

This is the best first implementation unit:

1. Add producer code and payload schema.
2. Add stream consumer command and reclaim/DLQ handling.
3. Add stream-specific settings and feature flags.
4. Add producer/consumer tests and rollback coverage.

Stage 1 and Stage 3 are already merged, so the next patch set should start at
stream consumer, observability, and rollout-safety work.

## Definition of done for Phase 2

Phase 2 should only be considered implemented when all of the following are
true:

- NDVI dispatch is centralized.
- One NDVI workflow is routed through Redis Streams.
- Consumer-group processing with reclaim and DLQ handling is operational.
- Lag and consumer health metrics are visible in Prometheus/Grafana.
- Rollback to plain Celery dispatch is a settings change, not a code rollback.
- Tests cover producer, consumer, reclaim, and fallback behavior.
