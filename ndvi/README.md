# NDVI app

Back to root: `../README.md`

## Overview

This app provides NDVI retrieval for user-owned farms using provider engines
(Sentinel Hub, STAC, and a GEE backfill adapter) and exposes endpoints under
`/api/v1/…/ndvi/`.

It is responsible for:
- NDVI timeseries/latest endpoints and response caching
- Job creation, idempotency, and Celery task execution
- Raster artifact storage and raster retrieval/queueing endpoints
- V2 fusion / quality-aware representations for timeseries, latest, and
  farm-state payloads

It is not responsible for:
- Farm ownership and bounding box persistence (see `farms/`)
- Authentication primitives (see `accounts/` and `api_keys/`)

## Key concepts / data model

Models (from code: `ndvi/models.py`):

- `NdviObservation`: materialized NDVI observation for a farm and `bucket_date`.
- `NdviJob`: idempotent job record tracked for Celery tasks.
- `NdviRasterArtifact`: persisted PNG raster artifact for a farm/date/size/cloud.

## API surface

Routes (from code: `ndvi/urls.py` and `config/urls.py`):

All successful JSON responses use the project envelope produced by
`config.api.responses.success_response`:

```json
{
  "status": 0,
  "message": "string",
  "data": {},
  "errors": null,
  "request_id": "req_..."
}
```

AuthZ notes:
- All farm-scoped endpoints enforce “owner-only” access by fetching farms with
  `owner_id=request.user.id` and `is_active=True` (from code: `ndvi/views.py`).
- Unauthorized access to another user’s farm appears as `404` (from code:
  `ndvi/views.py` and `ndvi/tests/test_ndvi.py`).

| Method | Path | Auth | Purpose | Key params |
| --- | --- | --- | --- | --- |
| GET | `/api/v1/farms/<farm_id>/ndvi/timeseries/` | JWT or `X-API-Key` | NDVI timeseries (cached; may enqueue gap-fill) | query: `start`, `end`, optional `step_days`, optional `max_cloud` |
| GET | `/api/v1/farms/<farm_id>/ndvi/latest/` | JWT or `X-API-Key` | Latest observation (cached; may enqueue refresh) | query: optional `lookback_days`, optional `max_cloud` |
| POST | `/api/v1/farms/<farm_id>/ndvi/refresh/` | JWT or `X-API-Key` | Manual refresh trigger (cooldown) | no body |
| GET | `/api/v1/farms/<farm_id>/ndvi/raster.png` | JWT or `X-API-Key` | Fetch raster PNG (binary) | query: `date`, optional `size`, optional `max_cloud` |
| POST | `/api/v1/farms/<farm_id>/ndvi/raster/queue` | JWT or `X-API-Key` | Queue raster render job (cooldown) | body: `date`, optional `size`, optional `max_cloud` |
| GET | `/api/v1/farm-state/<farm_id>/` | JWT or `X-API-Key` | Summarize last 30–60 days of NDVI to classify farm state | response: mean/max/coverage/trend plus state/action |
| GET | `/api/v1/ndvi/jobs/<job_id>/` | JWT or `X-API-Key` | Job status for current user | path: `job_id` |

All GET endpoints accept `?representation=v1|v2` where supported. `v1` is the
backward-compatible default; `v2` adds derived fields such as
`smoothed_ndvi`, `confidence`, `source`, `quality_flags`, and, for timeseries,
`v2_observations`.

### Examples

#### Timeseries

```bash
curl -sS \
  "http://localhost:8000/api/v1/farms/$FARM_ID/ndvi/timeseries/?start=2024-01-01&end=2024-01-15&step_days=7&max_cloud=30" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

Response:

```json
{
  "status": 0,
  "message": "NDVI time series",
  "data": {
    "observations": [{ "bucket_date": "2024-01-01", "mean": 0.1 }],
    "engine": "sentinelhub",
    "is_partial": true,
    "missing_buckets_count": 2
  },
  "errors": null,
  "request_id": "req_..."
}
```

With `?representation=v2`, the payload includes `v2_observations` and
`representation: "v2"` in the response data while preserving the V1 fields.

#### Latest

```bash
curl -sS \
  "http://localhost:8000/api/v1/farms/$FARM_ID/ndvi/latest/?lookback_days=14&max_cloud=30" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

Response:

```json
{
  "status": 0,
  "message": "Latest NDVI",
  "data": { "observation": null, "stale": true, "engine": "sentinelhub" },
  "errors": null,
  "request_id": "req_..."
}
```

With `?representation=v2`, the response also includes `v2_observation` and
`representation: "v2"`.

#### Manual refresh

```bash
curl -sS -X POST "http://localhost:8000/api/v1/farms/$FARM_ID/ndvi/refresh/" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

Response (queued):

```json
{
  "status": 0,
  "message": "Refresh queued",
  "data": { "job_id": 123 },
  "errors": null,
  "request_id": "req_..."
}
```

#### Raster PNG (binary)

```bash
curl -sS -D- \
  "http://localhost:8000/api/v1/farms/$FARM_ID/ndvi/raster.png?date=2024-03-03&size=256&max_cloud=25" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -o ndvi.png
```

Response:
- `200` with `Content-Type: image/png` and `ETag` header, or
- `304` if `If-None-Match` matches the current artifact hash, or
- `404` error envelope if the raster is not found (from code: `ndvi/views.py`)

#### Raster queue

```bash
curl -sS -X POST "http://localhost:8000/api/v1/farms/$FARM_ID/ndvi/raster/queue" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"date":"2024-03-03","size":256,"max_cloud":25}'
```

Response (queued):

```json
{
  "status": 0,
  "message": "Raster render queued",
  "data": { "job_id": 456 },
  "errors": null,
  "request_id": "req_..."
}
```

#### Job status

```bash
curl -sS "http://localhost:8000/api/v1/ndvi/jobs/$JOB_ID/" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

Response:

```json
{
  "status": 0,
  "message": "Job status",
  "data": { "status": "queued" },
  "errors": null,
  "request_id": "req_..."
}
```

## Business logic

High-level flow:
- Validate request params via DRF serializers (`ndvi/serializers.py`).
- Validate farm bbox and enforce quota (`ndvi/services.py`).
- Return cached response if present (timeseries/latest caches in
  `ndvi/services.py`).
- Enqueue jobs for missing data:
  - Timeseries can enqueue `gap_fill`
  - Latest can enqueue `refresh_latest`
  - Manual refresh and raster queue always enqueue (subject to cooldown)
  (from code: `ndvi/views.py`)

Jobs and idempotency:
- `enqueue_job()` writes an `NdviJob` keyed by a request hash so identical
  requests create a single active job (from code: `ndvi/services.py`).
- `run_ndvi_job` acquires a distributed lock in cache to prevent duplicate
  upstream calls (from code: `ndvi/tasks.py` and `ndvi/services.py`).

Provider engines:
- Timeseries/latest engines:
  - `gee`: `ndvi/engines/gee.py` (batch/backfill adapter stub)
  - `sentinelhub`: `ndvi/engines/sentinelhub.py` (Statistics API).
  - `stac`: `ndvi/engines/stac.py` (STAC search + local NDVI compute).
- Raster engines:
  - `sentinelhub`: `ndvi/raster/sentinelhub_engine.py` (Process API).
  - `stac`: `ndvi/raster/stac_compute_engine.py` (STAC search + local render).

## Engines: sentinelhub vs stac

Default engine selection is driven by `NDVI_ENGINE` (env var). You can
optionally override per request with `?engine=sentinelhub` or `?engine=stac`
(query param). Raster endpoints use `NDVI_RASTER_ENGINE_NAME` by default; it
defaults to `NDVI_ENGINE` unless explicitly overridden.

Engine resolution guardrails:
- **No import-time defaults**: defaults are read from Django settings at call
  time via `resolve_ndvi_engine_name()` (metrics) and
  `resolve_raster_engine_name()` (raster).
- **Two separate knobs**: metrics use `NDVI_ENGINE`; raster uses
  `NDVI_RASTER_ENGINE_NAME`.
- **Registry mapping only**: add new engines by updating one `SUPPORTED_*`
  list and one registry mapping (`ENGINE_FACTORIES` in `ndvi/services.py` for
  metrics, `RASTER_ENGINE_PATHS` in `ndvi/raster/registry.py` for raster).

Behavior highlights:
- `gee`: adapter for offline/backfill workloads; requires the GEE client and
  service account credentials.
- `sentinelhub`: upstream processing (Statistics/Process APIs) and requires
  Sentinel Hub credentials.
- `stac`: searches a STAC API for Sentinel-2 assets, downloads COGs, and
  computes NDVI locally for timeseries/latest and raster rendering.

## AuthZ / permissions

- `IsAuthenticated` on all NDVI endpoints (from code: `ndvi/views.py`).
- Farm ownership enforced by `_get_farm()` which filters by `owner_id` and
  `is_active` (from code: `ndvi/views.py`).

## Settings / env vars

Settings read from `config/settings.py` (non-exhaustive; see that file for full
list):

- `NDVI_ENGINE` (default: `sentinelhub`; set to `stac` for STAC backend)
- `NDVI_QUEUE_BACKEND` (default: `celery`; set to `stream` to publish NDVI work to Redis Streams)
- `NDVI_STREAM_NAME`, `NDVI_STREAM_GROUP`, `NDVI_STREAM_CONSUMER`
- `NDVI_STREAM_BLOCK_MS`, `NDVI_STREAM_CLAIM_IDLE_MS`, `NDVI_STREAM_RECLAIM_INTERVAL_SECONDS`
- `NDVI_STREAM_MAXLEN`, `NDVI_STREAM_DLQ_NAME`, `NDVI_STREAM_DLQ_MAXLEN`
- `NDVI_STREAM_BATCH_SIZE`, `NDVI_STREAM_MAX_DELIVERIES`, `NDVI_STREAM_START_ID`
- `NDVI_MAX_AREA_KM2`, `NDVI_MAX_DATERANGE_DAYS`
- `NDVI_DEFAULT_STEP_DAYS`, `NDVI_DEFAULT_MAX_CLOUD`, `NDVI_DEFAULT_LOOKBACK_DAYS`
- `NDVI_CACHE_TTL_TIMESERIES_SECONDS`, `NDVI_CACHE_TTL_LATEST_SECONDS`
- `NDVI_LOCK_TIMEOUT_SECONDS`
- `NDVI_MANUAL_REFRESH_COOLDOWN_SECONDS`
- `NDVI_REQUEST_TIMEOUT_SECONDS` (HTTP request timeout for NDVI service calls)
- `NDVI_V2_LOW_CONFIDENCE_THRESHOLD`, `NDVI_V2_SOURCE_DISAGREEMENT_THRESHOLD`
- `NDVI_V2_SMOOTHING_WINDOW_DAYS`
- Raster settings:
  - `NDVI_RASTER_ENGINE_PATH`, `NDVI_RASTER_ENGINE_NAME`
  - `NDVI_RASTER_DEFAULT_SIZE`, `NDVI_RASTER_MAX_SIZE`
  - `NDVI_RASTER_MANUAL_QUEUE_COOLDOWN_SECONDS`
  - `NDVI_RASTER_CACHE_TTL_SECONDS`

## Fusion / quality signals

The V2 representation uses the fusion and quality pipeline in
`ndvi/fusion.py`, `ndvi/sentinel1_context.py`, and `ndvi/v2_quality.py` to
derive:
- `smoothed_ndvi`
- `confidence`
- `source`
- `quality_flags`

The quality flags include source disagreement, fallback usage, anomaly
detection, and Sentinel-1 context indicators.

- Colormap normalization (added Apr 2026):
  - `NDVI_COLORMAP_NORMALIZATION` (default: `histogram`; or `fixed`)

STAC settings (used when `NDVI_ENGINE=stac` or `engine=stac`):
- `NDVI_STAC_API_URL` (default: `https://stac.dataspace.copernicus.eu/v1/`)
- `NDVI_STAC_COLLECTION` (default: `sentinel-2-l2a`; override for your STAC)
- `NDVI_STAC_MAX_CLOUD_DEFAULT` (default: 30)
- `NDVI_STAC_DATE_WINDOW_DAYS` (default: 3)
- `NDVI_STAC_ASSET_RED` (default: `B04_10m`)
- `NDVI_STAC_ASSET_NIR` (default: `B08_10m`)
- `NDVI_STAC_TIMEOUT_SECS` (default: 30)

STAC request throttling (prevent WAF rate-limit blocks):
- `NDVI_STAC_REQUEST_INTERVAL_SECS` (default: `10.0`; min seconds between requests)
- `NDVI_STAC_JITTER_MIN_SECS` (default: `1.0`; min random jitter in seconds)
- `NDVI_STAC_JITTER_MAX_SECS` (default: `5.0`; max random jitter in seconds)

STAC circuit breaker (stop retrying when upstream is unreachable):
- `NDVI_STAC_CIRCUIT_BREAKER_THRESHOLD` (default: `3`; failures before opening circuit)
- `NDVI_STAC_CIRCUIT_BREAKER_TIMEOUT_SECS` (default: `300.0`; seconds before retrying)

STAC proxy (bypass IP bans):
- `NDVI_STAC_PROXY_URL` (default: unset; e.g. `http://proxy.example.com:8080`)

Note: Throttling applies to every STAC API request with randomized jitter
to avoid pattern detection by upstream WAFs. Adjust these values if you
continue seeing `Request Rejected` errors from Copernicus.
Note: STAC raster rendering requires `rasterio`. Install rasterio or install
the stac extra in your environment.

Sentinel Hub credentials are read from environment variables (from code:
`ndvi/engines/sentinelhub.py`):
- `SENTINELHUB_CLIENT_ID`
- `SENTINELHUB_CLIENT_SECRET`
- `SENTINELHUB_BASE_URL` (optional; defaults to `https://services.sentinel-hub.com`)

## Raster flow

- `POST /ndvi/raster/queue` creates a `raster_png` job.
- Celery executes the job and stores an `NdviRasterArtifact` on success.
- `GET /ndvi/raster.png` returns the stored PNG or `404` if missing.
- If a raster job fails, check `GET /ndvi/jobs/<job_id>/` for `last_error`.

## Failure modes

- Sentinel Hub auth failures (401/403) produce a clear `last_error` message
  suggesting switching to `NDVI_ENGINE=stac` or updating credentials.
- STAC search failures or raster processing errors surface in `last_error`.
- No imagery returns empty observations for latest/timeseries endpoints
  (`observation` is null or `observations` is empty).
- Raster PNG endpoints return `Raster not found` (404) with
  `errors.code="raster_not_found"` and `errors.reason`:
  - `no_items`: STAC search returned no items in the date window.
  - `no_best_item`: items returned but none matched the window/cloud filter.
  - `missing_assets`: selected item missing required assets (B04/B08) or
    NDVI stats are empty.

## Background jobs

Celery tasks (from code: `ndvi/tasks.py`):

- `ndvi.tasks.run_ndvi_job` (retries: `max_retries=3`, `default_retry_delay=60`)
- `ndvi.tasks.enqueue_daily_refresh`
- `ndvi.tasks.enqueue_weekly_gap_fill`

Celery beat schedules are configured in `config/settings.py` under
`CELERY_BEAT_SCHEDULE`.

## Metrics / monitoring

Prometheus metrics (from code: `ndvi/metrics.py`):

- `spectral_jobs_total{index,status,type,engine}`
- `spectral_upstream_requests_total{index,engine,outcome}`
- `spectral_upstream_latency_seconds{index,engine}`
- `spectral_cache_hit_total{index,level}`
- `spectral_farms_stale_total{index,engine}`
- `spectral_task_runtime_seconds{index,task,engine}`
- `spectral_backfill_rows_total{index,engine,status}`
- `spectral_shadow_comparison_diffs_total{engine,index,field}`
- `spectral_provider_circuit_state{provider}`
- `spectral_job_dead_letter_total{queue}`
- `ndvi_v2_null_output_total{engine,reason}`
- `ndvi_v2_low_confidence_total{engine,reason}`
- `ndvi_v2_fallback_total{engine,reason}`
- `ndvi_v2_source_disagreement_total{engine}`
- `ndvi_v2_quality_flags_total{flag}`

## Testing

- API tests: `ndvi/tests/test_ndvi.py`
- Raster tests: `ndvi/tests/test_ndvi_raster_png.py`
- Run: `pytest ndvi/tests/`
