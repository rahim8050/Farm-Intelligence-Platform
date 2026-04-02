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
- NDVI dispatch is still scattered across views and periodic tasks via direct
  `.delay(...)` calls.
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

### Work

- Add one NDVI dispatch helper in `ndvi/services.py`.
- Replace every direct `run_ndvi_job.delay(...)` call with that helper.
- Replace every direct `compute_farm_state_coverage.delay(...)` call with that
  helper.
- Introduce a routing switch in settings, for example:
  - `NDVI_QUEUE_BACKEND=celery`
  - future value: `NDVI_QUEUE_BACKEND=stream`

### File targets

- `ndvi/services.py`
- `ndvi/views.py`
- `ndvi/tasks.py`
- `config/settings.py`

### Expected outcome

- No runtime behavior change yet.
- All NDVI enqueue behavior flows through one place.
- Future Redis Streams logic can be added without editing every call site.

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
- Reuse `request_hash` from `enqueue_job(...)` as the idempotency key.
- Add helper functions such as:
  - `publish_ndvi_job(job: NdviJob) -> str`
  - `publish_farm_state_coverage(...) -> str`

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

## Stage 5 - Add settings and feature flags

### Goal

Keep rollout reversible and configurable.

### Work

Add settings in `config/settings.py`, for example:

- `NDVI_QUEUE_BACKEND`
- `NDVI_STREAM_NAME`
- `NDVI_STREAM_GROUP`
- `NDVI_STREAM_CONSUMER`
- `NDVI_STREAM_BLOCK_MS`
- `NDVI_STREAM_CLAIM_IDLE_MS`
- `NDVI_STREAM_MAXLEN`
- `NDVI_STREAM_DLQ_NAME`

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

1. Merge Stage 1 with no behavior change.
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

## Recommended first patch set

This is the best first implementation unit:

1. Add `NDVI_QUEUE_BACKEND` setting.
2. Add a single NDVI dispatch helper in `ndvi/services.py`.
3. Replace all direct `.delay(...)` NDVI dispatch calls with that helper.
4. Add tests proving no runtime behavior change while backend is still
   `celery`.

This first patch should be merged before any Redis Streams code is added.

## Definition of done for Phase 2

Phase 2 should only be considered implemented when all of the following are
true:

- NDVI dispatch is centralized.
- One NDVI workflow is routed through Redis Streams.
- Consumer-group processing with reclaim and DLQ handling is operational.
- Lag and consumer health metrics are visible in Prometheus/Grafana.
- Rollback to plain Celery dispatch is a settings change, not a code rollback.
- Tests cover producer, consumer, reclaim, and fallback behavior.
