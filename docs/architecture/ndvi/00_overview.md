# NDVI Blueprint Overview

This folder breaks the NDVI system into implementation-ready slices for Django, DRF, and async workers. It does not change runtime behavior.

## Implementation map

| Concern | Django modules | Service class | Async boundary | Transaction boundary |
|---|---|---|---|---|
| V1 ingest and persistence | `ndvi/models.py`, `ndvi/services.py`, `ndvi/tasks.py`, `ndvi/proxy_views.py` | `NdviObservationIngestionService` | Celery task `run_ndvi_job`; Redis Streams consumer dispatches to Celery when enabled | One atomic block per observation batch |
| V2 quality materialization | `ndvi/services.py`, `ndvi/farm_state.py`, `ndvi/tasks.py` | `NdviV2QualityService` | Same worker that produced V1 or a backfill worker | One atomic block per V2 row |
| Fallback and fusion | `ndvi/engines/base.py`, `ndvi/engines/stac.py`, `ndvi/engines/sentinelhub.py`, `ndvi/stac_client.py`, `ndvi/services.py` | `NdviFallbackFusionService` | Worker-local, no separate request-time path | Same transaction as V2 write |
| Farm-state derivation | `ndvi/farm_state.py`, `ndvi/tasks.py` | `NdviFarmStateService` | `compute_farm_state_coverage` task or equivalent queued job | One atomic refresh if persisted; otherwise cache write only |
| API responses | `ndvi/views.py`, `ndvi/serializers.py`, `config/api/responses.py` | `NdviTimeseriesService`, `NdviLatestService`, `NdviRefreshService` | Request path only queues work; no long-running compute | No business write in read endpoints |
| Raster artifacts | `ndvi/raster/base.py`, `ndvi/raster/stac_compute_engine.py`, `ndvi/raster/sentinelhub_engine.py`, `ndvi/tasks.py`, `ndvi/models.py` | `NdviRasterService` | `NdviRasterQueueView` queues raster work | Atomic artifact upsert |
| Streams | `ndvi/streams.py`, `ndvi/management/commands/consume_ndvi_stream.py` | `NdviStreamConsumerService` | Redis Streams consumer group | Ack only after Celery enqueue succeeds |

## Async execution model

- Celery is the default worker model for NDVI jobs.
- Redis Streams is an opt-in dispatch layer when `NDVI_QUEUE_BACKEND=stream`.
- The stream consumer reads messages, validates payloads, and enqueues the same Celery jobs used by the direct path.
- No NDVI compute runs inline in the API request cycle.
- Every worker path must call `close_old_connections()` before write-side DB work and retry transient database failures once after reconnect.

## V2 migration and backfill

- V2 is computed from existing V1 rows.
- Backfill runs in deterministic windows ordered by `farm_id`, `engine`, and `bucket_date`.
- The backfill worker must be idempotent on `(v1_observation_id)` for V2 and on `(farm_id, engine, window_start, window_end)` for farm-state.
- Backfill starts in shadow mode, then dual-run mode, then becomes the default source for `/latest/` and `/farm-state/` only after the rollout gates are met.

## Sentinel-1 boundary

- Sentinel-1 is a signal input only.
- It must not create a V1 observation row.
- It must not create a V2 NDVI value.
- It may contribute only to V2 quality flags, source disagreement metadata, or confidence context.
- Do not store Sentinel-1 as a substitute for NDVI in `NdviObservation`.

## Safety rules

- Do not rename URLs, apps, or model names.
- Do not change auth or throttling.
- Do not alter response envelopes.
- Keep all changes additive and idempotent.

## Phase Execution Order (Strict)

Phases MUST be implemented sequentially:

1. Phase 1 – STAC hardening + V1 ingestion
2. Phase 2 – V2 quality engine
3. Phase 3 – fallback selection
4. Phase 4 – fusion + context signals
5. Phase 5 – API exposure
6. Phase 6 – async pipeline + observability

Rules:

- Do NOT implement later phases before earlier ones are complete
- Do NOT expose V2 in APIs before dual-run readiness
- Each phase must meet its exit criteria before proceeding

## Cross-Phase Isolation Rule

- Phase implementations must not depend on incomplete future phases
- V2 logic must not assume fallback exists until Phase 3
- API must not assume V2 availability until Phase 5

## Phase → Implementation Mapping

| Phase | Modules / Docs | Primary Services | Blocking Dependencies |
|------|----------------|------------------|-----------------------|
| Phase 1 | 01_models.md, 02_engine_adapters.md | IngestObservationService | None |
| Phase 2 | 03_v2_quality_engine.md | BuildV2ObservationService | Phase 1 complete |
| Phase 3 | 04_fallback_fusion.md | FallbackSelectorService | Phase 2 complete |
| Phase 4 | 04_fallback_fusion.md, 07_observability.md | FusionService | Phase 3 complete |
| Phase 5 | 06_api_contracts.md | DRF Views / Serializers | Phase 2–4 stable |
| Phase 6 | 05_pipeline_execution.md, 07_observability.md | Async Tasks / Workers | All prior phases |
