# Contributing NDVI Engines

This guide explains how to add a new NDVI provider engine (as an alternative to
Sentinel Hub) and how it plugs into time series/latest retrieval, job
execution, and raster PNG generation.

## Architecture overview

The NDVI stack has two engine layers:

1) **NDVI time series / latest engines** (`ndvi/engines/*`)
   - Implement `NDVIEngine` from `ndvi/engines/base.py`.
   - Used by services and jobs to fetch NDVI observations.

2) **Raster engines** (`ndvi/raster/*`)
   - Implement `NdviRasterEngine` from `ndvi/raster/base.py`.
   - Used to render PNG rasters that are stored as `NdviRasterArtifact`.

Views (`ndvi/views.py`) validate inputs, normalize bbox and quotas
(`ndvi/services.py`), read/write caches, and enqueue jobs (`ndvi/tasks.py`).

## Engine contracts

### NDVIEngine (timeseries + latest)

Defined in `ndvi/engines/base.py`:

```python
class NDVIEngine(Protocol):
    def get_timeseries(
        self,
        *,
        bbox: BBox,
        start: date,
        end: date,
        step_days: int,
        max_cloud: int,
    ) -> list[NdviPoint]:
        ...

    def get_latest(
        self,
        *,
        bbox: BBox,
        lookback_days: int,
        max_cloud: int,
    ) -> NdviPoint | None:
        ...
```

Key types:

- `BBox` (`ndvi/engines/base.py`): normalized WGS84 bbox (west/south/east/north).
- `NdviPoint` (`ndvi/engines/base.py`): `date`, `mean`, optional `min/max`,
  `sample_count`, `cloud_fraction`.

### NdviRasterEngine (PNG rasters)

Defined in `ndvi/raster/base.py`:

```python
class NdviRasterEngine(Protocol):
    def render_png(self, request: RasterRequest) -> bytes:
        ...
```

`RasterRequest` fields (`ndvi/raster/base.py`):

- `bbox` (BBox)
- `date` (target day)
- `size` (pixel size)
- `max_cloud`
- `engine` (string, used for metadata)

## Request/response semantics

### Input normalization

From `ndvi/serializers.py`:

- `step_days`: normalized to `1..30` for time series endpoints.
- `max_cloud`: normalized to `0..100`.
- `size`: raster requests are validated (defaults and max in settings).

From `ndvi/services.py`:

- `normalize_bbox(farm)` ensures bbox is present and ordered (`west < east`,
  `south < north`).
- `enforce_quota(farm, bbox)` rejects large areas (`NDVI_MAX_AREA_KM2`).

Engine implementations should assume inputs are validated and normalized, but
must still handle upstream errors gracefully.

### Date/bucket semantics

`get_timeseries()` returns points whose `date` is the bucket date. The default
Sentinel Hub engine uses ISO date buckets derived from the upstream interval
start.

`get_latest()` is expected to return the most recent point within a lookback
window (see `ndvi/engines/sentinelhub.py`).

## Engine selection and configuration

### Timeseries/latest engine selection

`ndvi/services.py`:

- `resolve_ndvi_engine_name()` reads defaults at call time from
  `settings.NDVI_ENGINE` (no import-time defaults).
- `SUPPORTED_ENGINES` + `ENGINE_FACTORIES` map engine names to constructors.
- `get_engine()` instantiates the engine from the registry mapping.

To add a new engine, update **exactly**:
- `SUPPORTED_ENGINES` (one list), and
- `ENGINE_FACTORIES` (one registry mapping).

### Raster engine selection

`ndvi/raster/registry.py`:

- `resolve_raster_engine_name()` reads defaults at call time from
  `settings.NDVI_RASTER_ENGINE_NAME` (no import-time defaults).
- `SUPPORTED_RASTER_ENGINES` + `RASTER_ENGINE_PATHS` map names to import paths.
- The registry caches engine instances by resolved name via `lru_cache`.

If you introduce a new raster engine, update **exactly**:
- `SUPPORTED_RASTER_ENGINES` (one list), and
- `RASTER_ENGINE_PATHS` (one registry mapping).

Note the **two separate knobs**:
- `NDVI_ENGINE` controls metrics (timeseries/latest).
- `NDVI_RASTER_ENGINE_NAME` controls raster PNGs.

### Settings and secrets

Provider credentials and base URLs live in `config/settings.py`, sourced from
environment variables (never hardcode secrets). Follow the Sentinel Hub pattern
in `ndvi/engines/sentinelhub.py`:

- `SENTINELHUB_CLIENT_ID`
- `SENTINELHUB_CLIENT_SECRET`
- `SENTINELHUB_BASE_URL`

If you add a new engine, introduce new env vars with the same pattern.

## Request flow by endpoint

### Time series (`/api/v1/farms/<id>/ndvi/timeseries/`)

1) `NdviTimeseriesView` validates query params with
   `TimeseriesRequestSerializer`.
2) `normalize_bbox()` + `enforce_quota()` run before any engine call.
3) Cached payloads are returned via `get_cached_timeseries_response()`.
4) If data is missing, `enqueue_job()` schedules a `gap_fill` job.
5) When executed, `run_ndvi_job()` calls `engine.get_timeseries(...)`.

### Latest (`/api/v1/farms/<id>/ndvi/latest/`)

1) `NdviLatestView` validates params with `LatestRequestSerializer`.
2) Bbox + quota are enforced as above.
3) Cached payloads are returned via `get_cached_latest_response()`.
4) If missing/stale, `enqueue_job()` schedules `refresh_latest`.
5) When executed, `run_ndvi_job()` calls `engine.get_latest(...)`.

### Raster PNG (`/api/v1/farms/<id>/ndvi/raster.png` and `/raster/queue`)

1) Raster endpoints validate `date`, `size`, and `max_cloud` with
   `RasterPngRequestSerializer`.
2) The queue endpoint maps `size -> step_days` and enqueues `raster_png`.
3) `run_ndvi_job()` calls `render_ndvi_png(...)`, which calls the raster engine.
4) The PNG bytes are stored in `NdviRasterArtifact` and served by the raster
   view with ETag caching.

## Job pipeline integration

### Job types and parameters

Job types live in `ndvi/models.py`:

- `refresh_latest`
- `gap_fill`
- `backfill`
- `raster_png`

Jobs are created via `enqueue_job()` in `ndvi/services.py`, which:

- Builds a deterministic `request_hash` using engine + owner + farm + params.
- Returns an existing queued/running job when the same request is already in
  flight (idempotency).

Each job stores parameters in the `NdviJob` record:

- `start`, `end` (date range)
- `step_days` (time series bucket size **or** raster size, see below)
- `max_cloud`
- `lookback_days`

### Celery execution flow

`run_ndvi_job()` in `ndvi/tasks.py` executes jobs:

- Uses `acquire_lock()` to prevent duplicate upstream calls.
- `refresh_latest`: calls `engine.get_latest(...)` and upserts observations.
- `raster_png`: calls `render_ndvi_png(...)` and stores an artifact.
- All other job types (gap_fill/backfill): call `engine.get_timeseries(...)`.

If an exception is raised, the job is marked failed and `last_error` is set to
the exception string. Do not include secrets in error messages.

### Idempotency rules (do not break)

- `enqueue_job()` uses `hash_request()`; modifying params or engine names
  changes the hash and can create duplicates.
- `NdviJob` has a uniqueness constraint on active jobs
  (`uniq_active_ndvi_job`).
- `run_ndvi_job()` uses a cache lock with key `ndvi:lock:{request_hash}`.

New engines should not bypass these paths.

## Raster artifacts

### Generation and storage

- `render_ndvi_png()` (`ndvi/raster/service.py`) constructs a `RasterRequest`
  and delegates to the configured raster engine.
- It returns `(bytes, sha256_hash)` where the hash is used for ETag caching.
- `run_ndvi_job()` stores the PNG in `NdviRasterArtifact` and clears
  `last_error` on success.

### Cache keys and ETag behavior

`NdviRasterArtifact` is looked up by `farm`, `engine`, `date`, `size`,
`max_cloud`. The raster view caches artifact IDs at:

```
ndvi:raster:ptr:{farm_id}:{engine_name}:{date}:{size}:{max_cloud}
```

ETag is the artifact `content_hash`.

### Size vs step_days (important)

- For time series, `step_days` is the bucket interval.
- For raster jobs, **`step_days` is treated as raster size**.
  - The raster queue endpoint maps `size -> step_days` in `ndvi/views.py`.
  - The task converts `job.step_days` into `raster_size`.

Raster size bounds are validated in `ndvi/serializers.py` and re-checked in
`ndvi/tasks.py` using `NDVI_RASTER_DEFAULT_SIZE` and `NDVI_RASTER_MAX_SIZE`.

When adding engines, keep this convention to avoid mismatched raster sizes.

## Error handling policy

### Timeseries/latest engines

- Upstream HTTP failures should not leak secrets or full payloads.
- If you include response bodies, truncate and redact tokens.
- Exceptions bubble to `run_ndvi_job()` and will be recorded in
  `NdviJob.last_error`.

### Raster engines

`ndvi/raster/sentinelhub_engine.py` uses `SentinelHubRasterError` to attach a
status code and truncated body snippet (max 1600 chars). `run_ndvi_job()` stores
the exception string in `NdviJob.last_error`, so ensure it is sanitized.

## Testing expectations

Add tests that avoid network calls by mocking request methods.

Minimum tests for a new engine:

1) **Timeseries parsing** (e.g., parse upstream payload into `NdviPoint`).
2) **Latest retrieval** (e.g., uses lookback range and returns most recent).
3) **Raster rendering** (returns PNG bytes for a `RasterRequest`).
4) **Failure path** (upstream error stored in `last_error`).

Reference tests:

- `ndvi/tests/test_ndvi.py`
- `ndvi/tests/test_ndvi_services.py`
- `ndvi/tests/test_ndvi_raster_engines.py`
- `ndvi/tests/test_ndvi_tasks_extra.py`

Run only NDVI tests you touched, for example:

```
python -m pytest ndvi/tests/test_ndvi_raster_engines.py
python -m pytest ndvi/tests/test_ndvi_tasks_extra.py
```

## Performance and quotas

Quota enforcement happens in `ndvi/services.py`:

- `normalize_bbox()` ensures bbox is present and valid.
- `enforce_quota()` rejects large areas (setting: `NDVI_MAX_AREA_KM2`).

Caching is handled in `ndvi/services.py`:

- Timeseries and latest responses are cached per owner/farm/engine/params.
- Engines should not bypass these caches.

## Example engine skeletons

### ExampleNdviEngine (timeseries/latest)

```python
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import httpx

from ndvi.engines.base import BBox, NDVIEngine, NdviPoint


class ExampleNdviEngine(NDVIEngine):
    engine_name = "example"

    def get_timeseries(
        self,
        *,
        bbox: BBox,
        start: date,
        end: date,
        step_days: int,
        max_cloud: int,
    ) -> list[NdviPoint]:
        payload = self._fetch(
            {
                "bbox": [
                    float(bbox.west),
                    float(bbox.south),
                    float(bbox.east),
                    float(bbox.north),
                ],
                "start": start.isoformat(),
                "end": end.isoformat(),
                "step_days": step_days,
                "max_cloud": max_cloud,
            }
        )
        points: list[NdviPoint] = []
        for item in payload.get("data", []):
            points.append(
                NdviPoint(
                    date=date.fromisoformat(item["date"]),
                    mean=float(item["mean"]),
                    min=float(item["min"]) if item.get("min") else None,
                    max=float(item["max"]) if item.get("max") else None,
                    sample_count=(
                        int(item["count"]) if item.get("count") else None
                    ),
                    cloud_fraction=(
                        float(item["cloud"]) if item.get("cloud") else None
                    ),
                )
            )
        return points

    def get_latest(
        self,
        *,
        bbox: BBox,
        lookback_days: int,
        max_cloud: int,
    ) -> NdviPoint | None:
        end = date.today()
        start = end - timedelta(days=lookback_days)
        points = self.get_timeseries(
            bbox=bbox,
            start=start,
            end=end,
            step_days=lookback_days,
            max_cloud=max_cloud,
        )
        return sorted(points, key=lambda p: p.date)[-1] if points else None

    def _fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        response = httpx.get("https://example.com/ndvi", params=params)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Unexpected ExampleNdviEngine response shape")
        return data
```

### ExampleRasterEngine (PNG)

```python
from __future__ import annotations

from ndvi.raster.base import NdviRasterEngine, RasterRequest


class ExampleRasterEngine(NdviRasterEngine):
    def render_png(self, request: RasterRequest) -> bytes:
        # Call upstream and return PNG bytes.
        ...
```

## PR checklist

- Engine class implemented and registered (`get_engine()` and/or
  `NDVI_RASTER_ENGINE_PATH`).
- Settings/env vars documented and used (no secrets in code).
- Tests added for time series, latest, raster, and failure path.
- Docs updated (`docs/contributing_ndvi_engines.md`).
- Error handling truncates upstream bodies and redacts credentials.
