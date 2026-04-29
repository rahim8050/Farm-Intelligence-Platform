# NDVI Pipeline Execution

## Current Django modules

- `ndvi/tasks.py`
- `ndvi/services.py`
- `ndvi/streams.py`
- `ndvi/management/commands/consume_ndvi_stream.py`
- `ndvi/models.py`
- `ndvi/circuit_breaker.py`
- `ndvi/retry_policy.py`

## Service class mapping

- `NdviJobDispatchService`
- `NdviObservationIngestionService`
- `NdviV2BackfillService`
- `NdviFarmStateService`
- `NdviRasterService`
- `NdviStreamConsumerService`

## Async execution model

- Celery is the primary worker execution path.
- Redis Streams is a dispatch alternative controlled by `NDVI_QUEUE_BACKEND=stream`.
- The stream consumer must only ack after the corresponding Celery job is successfully enqueued.
- The API request path must only create or enqueue work; it must not perform long-running raster or STAC processing.

## Task boundaries

- `run_ndvi_job(job_id)` handles observation refresh, V1 persistence, V2 materialization, and job status updates.
- `compute_farm_state_coverage(...)` handles farm-state coverage refresh.
- Raster rendering runs through the raster queue and persists `NdviRasterArtifact`.
- Stream consumption is a separate management command, not a web request task.

## Ordering guarantees

- V1 must be persisted before V2 is computed.
- V2 must be persisted before farm-state recomputation.
- Job status must move `queued -> running -> success|failed` in that order.
- Duplicate work must collapse under unique constraints and request hashes.

## Deduplication strategy

- `NdviJob.request_hash` is the async job idempotency key.
- `(farm, engine, bucket_date)` prevents duplicate V1 observations.
- `v1_observation_id` prevents duplicate V2 rows.
- `(farm, engine, date, size, max_cloud)` prevents duplicate raster artifacts.

## Retry policy

- Upstream provider failures follow the provider retry policy and circuit breaker.
- Database connection failures retry once after `close_old_connections()`.
- Deterministic quality failures do not retry.
- Poison stream messages go to DLQ rather than being retried forever.

## V2 migration and backfill

- Backfill reads historical V1 rows in batch order.
- Backfill writes V2 in small atomic batches.
- Dual-run should continue until the promotion gates are satisfied.
- After promotion, `/latest/` and `/farm-state/` should source V2 first, but V1 must remain available for audit.

## Transaction boundaries

- Wrap V1 upsert and job state updates separately from engine fetches.
- Wrap V2 persistence separately from farm-state cache updates.
- Wrap raster artifact write in its own atomic block.

## Task Boundaries

All async execution MUST map to explicit task units.

### task_ingest_observation
Input:
- engine
- farm_id
- date_range

Flow:
EngineAdapter → IngestObservationService → V1Observation

### task_build_v2
Input:
- v1_observation_id

Flow:
BuildV2ObservationService → V2Observation | null

### task_select_fallback
Input:
- farm_id
- bucket_date

Flow:
FallbackSelectorService → selected candidate | null

### task_compute_farm_state
Input:
- farm_id
- window_start
- window_end

Flow:
BuildFarmStateService → FarmState

## Transaction Rules

### V1 Ingestion
- atomic per observation
- enforce unique (farm, engine, scene_id)

### V2 Materialization
- atomic per V1Observation
- must not partially persist confidence or flags

### FarmState Computation
- atomic per (farm, engine, window)

### Global Rules
- Never wrap external API calls in DB transactions
- Always commit V1 before triggering V2
- Enforce idempotency at DB constraint level

## Idempotency Guarantees

- V1: unique (farm, engine, source_scene_id)
- V2: one-to-one with V1Observation
- FarmState: unique (farm, engine, window_start, window_end)

Retries MUST NOT create duplicate rows.

On conflict:
- fetch existing row
- return existing result
- do not overwrite
