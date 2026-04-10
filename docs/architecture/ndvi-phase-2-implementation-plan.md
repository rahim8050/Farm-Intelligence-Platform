# NDVI Phase 2 Implementation Plan: Redis Streams

This document turns the Phase 2 section of
`docs/architecture/ndvi-pipeline-evolution.md` into a concrete
implementation plan. The goal is to introduce Redis Streams for NDVI
ingestion without rewriting the rest of the async stack or destabilizing the
 existing Celery-based workflow.

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
- `NDVI_QUEUE_BACKEND` now controls the dispatch boundary; `stream` still
  raises `NotImplementedError` until the stream producer exists.
- No Redis Streams code exists yet:
  - no `XREADGROUP`
  - no `XPENDING`
  - no `XCLAIM`
  - no `XTRIM`
  - no `ndvi_stream` queue routing

## Stage 1 - Centralize NDVI dispatch

### Goal

Create a single dispatch boundary for NDVI-related async work before adding any
Redis Streams behavior.

### Current Status (as of April 4, 2026)

- ✅ Dispatch helpers implemented (`dispatch_ndvi_job`, `dispatch_farm_state_coverage`)
- ✅ All 9 call sites migrated from direct `.delay()` to dispatch helpers
- ✅ `NDVI_QUEUE_BACKEND` setting added with `get_ndvi_queue_backend()` helper
- ✅ Routing switch implemented in both dispatch helpers
- ✅ Tests cover Celery routing and `stream` fallback behavior
- ✅ Stage 1 is complete

### Work

- No Stage 1 code work remains.
- The dispatch boundary already exists and routes to Celery by default.
- `NDVI_QUEUE_BACKEND=stream` remains intentionally unimplemented until the
  Stage 3 producer is added.

### File targets

- `ndvi/services.py`
- `ndvi/views.py`
- `ndvi/tasks.py`
- `config/settings.py`

### Expected outcome

- No runtime behavior change yet.
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

### Work

- Add a new stream module, for example:
  - `ndvi/streams.py`
  - or `ndvi/streaming.py`
- Define a stream payload schema containing:
  - `job_id`
  - `request_hash`
  - `farm_id`
  - `owner_id`
  - `engine`
  - `job_type`
  - serialized params
  - enqueue timestamp
  - `colormap_normalization` (added April 2026: "histogram" or "fixed")
- Reuse `request_hash` from `enqueue_job(...)` as the idempotency key.
- Add helper functions such as:
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

### Work

- Implement consumer-group reads using `XREADGROUP`.
- Acknowledge successfully handled entries with `XACK`.
- Detect stuck deliveries with `XPENDING`.
- Reclaim stale entries with `XCLAIM`.
- Send persistent failures to a dead-letter stream.
- Trim the main stream and DLQ with `XTRIM`.

### Recommended implementation shape

- Create a Django management command:
  - `ndvi/management/commands/consume_ndvi_stream.py`
- The command should:
  - ensure the consumer group exists
  - block on `XREADGROUP`
  - enqueue corresponding Celery work
  - `XACK` after successful enqueue
  - reclaim stale deliveries
  - move unrecoverable failures to the DLQ

### Expected outcome

- NDVI ingestion becomes durable and observable before execution reaches Celery.
- Recovery logic is explicit rather than hidden inside broker internals.

### Error Handling Strategy (Added April 2026)

#### Celery Enqueue Failures (e.g., Sentinel Failover)
When consumer fails to enqueue to Celery (e.g., during 55s Sentinel failover):
1. Retry up to 3 times with exponential backoff (1s, 2s, 4s)
2. If all retries fail: DO NOT XACK the entry
3. Entry remains pending in stream
4. XPENDING/XCLAIM will reclaim it later
5. Consumer will retry on next read

#### Stream Entry Processing Errors
- **Transient errors** (network, timeout): Retry 3x, then leave pending
- **Permanent errors** (invalid data, missing assets): XACK and send to DLQ
- **Structural errors** (schema violations): XACK, log, and alert

#### Error Classification
Consumer must distinguish error types for proper retry/DLQ routing:
- `no_items`: STAC search returned nothing → Retry
- `no_best_item`: No items within date window → Retry
- `missing_assets`: Items lack required bands → DLQ
- `processing_failed`: Raster processing error → Retry
- `empty_stats`: NDVI computation returned empty → DLQ

### Idempotency Strategy (Added April 2026)

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

## Stage 5 - Add settings and feature flags

### Goal

Keep rollout reversible and configurable.

### Work

Add settings in `config/settings.py`, for example:

- `NDVI_QUEUE_BACKEND` (✅ already added)
- `NDVI_STREAM_NAME`
- `NDVI_STREAM_GROUP`
- `NDVI_STREAM_CONSUMER`
- `NDVI_STREAM_BLOCK_MS`
- `NDVI_STREAM_CLAIM_IDLE_MS`
- `NDVI_STREAM_MAXLEN`
- `NDVI_STREAM_DLQ_NAME`

### Settings Reference Table (Updated April 2026)

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

### Work

Add tests for:

- producer payload shape
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

Stage 1 is already merged, so the next patch set should start at stream
producer and consumer work.

## Definition of done for Phase 2

Phase 2 should only be considered implemented when all of the following are
true:

- NDVI dispatch is centralized.
- One NDVI workflow is routed through Redis Streams.
- Consumer-group processing with reclaim and DLQ handling is operational.
- Lag and consumer health metrics are visible in Prometheus/Grafana.
- Rollback to plain Celery dispatch is a settings change, not a code rollback.
- Tests cover producer, consumer, reclaim, and fallback behavior.
