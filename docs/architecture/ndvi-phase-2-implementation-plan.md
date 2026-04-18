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

### Work

- Create `ndvi/management/commands/consume_ndvi_stream.py`.
- Ensure the consumer group exists with `XGROUP CREATE ... MKSTREAM` and use
  stream ID `0` on first creation so the first consumer can drain backlog
  instead of skipping already-published entries.
- Implement consumer-group reads using `XREADGROUP`.
- Detect stuck deliveries with `XPENDING`.
- Reclaim stale entries with `XCLAIM`.
- Enqueue the corresponding Celery task for each valid payload shape.
- Acknowledge successfully handled entries with `XACK`.
- Send persistent failures to a dead-letter stream.
- Trim the main stream and DLQ with `XTRIM`.

### Recommended implementation shape

- The command should run an explicit loop:
  1. reclaim stale deliveries
  2. block on `XREADGROUP` for new work
  3. validate and route the payload
  4. enqueue Celery work
  5. `XACK` only after enqueue succeeds
  6. move unrecoverable or over-retried entries to the DLQ

### Payload routing contract

- `job_type="farm_state_coverage"` routes to
  `compute_farm_state_coverage.delay(...)` and requires:
  - `farm_id`
  - `engine`
  - `target_date`
  - `threshold`
- All other supported `NdviJob.JobType` payloads route to
  `run_ndvi_job.delay(job_id)` and require:
  - `job_id`
  - `request_hash`
  - `farm_id`
  - `owner_id`
  - `engine`
  - `job_type`
- Payloads missing required fields are structural errors:
  - they must be `XACK`ed
  - they must be copied to the DLQ
  - they must emit a log entry suitable for alerting

### Consumer invariants

- `request_hash` uniqueness in the database remains the primary durable
  idempotency guard.
- A local cache of processed stream entry IDs may be used only as an
  optimization; it must not be the only deduplication mechanism.
- `XACK` must happen strictly after Celery enqueue succeeds.
- If the process crashes after enqueue but before `XACK`, duplicate delivery is
  acceptable and must be absorbed by downstream idempotency rules.

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
- **Poison messages** (retry budget exhausted): XACK and send to DLQ

#### Error Classification
Consumer must distinguish error types for proper retry/DLQ routing:
- `no_items`: STAC search returned nothing → Retry
- `no_best_item`: No items within date window → Retry
- `missing_assets`: Items lack required bands → DLQ
- `processing_failed`: Raster processing error → Retry
- `empty_stats`: NDVI computation returned empty → DLQ

### Retry budget and reclaim policy

- The consumer must use a finite poison-message budget. An entry that exceeds
  the delivery budget must be moved to the DLQ instead of being reclaimed
  forever.
- Reclaim runs must be bounded per loop iteration so one noisy backlog does not
  starve new work.
- `NDVI_STREAM_CLAIM_IDLE_MS` remains the threshold for reclaim eligibility.
- `XPENDING` is the source of truth for stale delivery detection; `XCLAIM` is
  the mechanism for takeover. `XAUTOCLAIM` is acceptable as an implementation
  detail if the external behavior stays the same.

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

### DLQ payload contract

Each DLQ entry should preserve enough context for replay and debugging:

- original stream name
- original stream entry ID
- original payload
- error classification
- human-readable error message
- delivery count
- failed-at timestamp
- consumer name

### Acceptance criteria for Stage 4

- `consume_ndvi_stream` exists and can create the consumer group safely.
- The consumer can route both payload families already produced by
  `ndvi/streams.py`.
- Successful Celery enqueue leads to `XACK`.
- Transient enqueue failures remain pending and are reclaimable.
- Structural, permanent, and over-budget failures are copied to the DLQ and
  then `XACK`ed.
- Reclaim and trim behavior are explicit code paths, not operator runbooks.

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
