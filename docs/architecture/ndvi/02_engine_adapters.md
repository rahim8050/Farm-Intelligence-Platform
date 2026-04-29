# NDVI Engine Adapters

## Current Django modules

- `ndvi/engines/base.py`
- `ndvi/engines/stac.py`
- `ndvi/engines/sentinelhub.py`
- `ndvi/stac_client.py`
- `ndvi/retry_policy.py`
- `ndvi/circuit_breaker.py`

## Adapter contract

The adapter layer must expose the same shape regardless of provider.

### Required methods

- `get_timeseries(geometry, start, end, step_days, max_cloud)`
- `get_latest(geometry, lookback_days, max_cloud)`

### Normalized observation shape

- `engine`
- `bucket_date`
- `scene_id`
- `acquisition_at`
- `mean`
- `min`
- `max`
- `sample_count`
- `cloud_fraction`
- `valid_pixel_fraction`
- `quality_flags`

## Current adapters

### `StacEngine`

Module: `ndvi/engines/stac.py`

Responsibilities:

- Select the best STAC item within the requested window.
- Load red and NIR rasters through `ndvi/stac_client.py`.
- Compute NDVI statistics.
- Normalize cloud cover to a percent or fraction used by the rest of the pipeline.

Transaction boundary:

- None during fetch.
- Persist only after normalization in `ndvi/tasks.py` or `ndvi/services.py`.

Failure behavior:

- Raise `StacUpstreamError` or `StacProcessingError`.
- Do not partially persist on fetch failure.

### `SentinelHubEngine`

Module: `ndvi/engines/sentinelhub.py`

Responsibilities:

- Use the Sentinel Hub Statistics/Process APIs when credentials are configured.
- Apply scene-level cloud and SCL filtering.
- Return normalized observations in the same shape as STAC.

Transaction boundary:

- None during fetch.
- Persist only after normalization.

Failure behavior:

- Raise `SentinelHubUpstreamError` or `SentinelHubAuthError`.
- Respect retry policy only for retryable upstream failures.

## Sentinel-1 usage boundary

Sentinel-1 is not an NDVI adapter.

- It may be implemented as a signal adapter that yields context only.
- It must not emit `mean`, `min`, `max`, or `NdviObservation` rows.
- It may contribute `quality_flags["s1_context_wet_soil"]` and confidence penalties in V2.
- If persisted, persist only provider metadata, not an NDVI observation record.

## Async boundary

- Engine fetches run in Celery workers or in a stream-dispatched worker.
- No provider call should execute inside a read-only API request path except a deliberate refresh endpoint that only enqueues work.

## Idempotency strategy

- Re-fetching the same farm/date window must produce the same normalized scene selection.
- Persisting the same scene must hit the unique constraints in `ndvi/models.py` instead of creating duplicates.

