# Audit: STAC raster_png failure (Job 66)

Scope: code-only audit of the STAC raster path in this repo. No runtime config or logs were available. Evidence supplied: farm bbox `[36.78345, -0.92234, 36.78411, -0.92202]`, dt_day `2026-01-22T00:00:00Z/2026-01-23T00:00:00Z`, dt_week `2026-01-16T00:00:00Z/2026-01-23T00:00:00Z`, and a raw STAC search **without** collections returning Sentinel-3/5P items with assets like `B0`, `B2`, `B3`, `MIR`, `NDVI`.

## Call graph for job_type="raster_png"

- `ndvi/tasks.py:39-113` → `run_ndvi_job()` loads the job, normalizes bbox, and for `RASTER_PNG` calls `render_ndvi_png(...)` with `raster_date = job.start or job.end or date.today()` and `raster_size = job.step_days`. It then saves the artifact if rendering succeeds. Failure surfaces as a `ValidationError` and the job is marked failed with `error=str(exc)` in `ndvi/tasks.py:174-182`.
- `ndvi/raster/service.py:17-40` → `render_ndvi_png()` builds a `RasterRequest` and calls the raster engine’s `render_png()`.
- `ndvi/raster/registry.py:31-54` → `resolve_raster_engine_name()` selects `stac` if configured; `get_engine()` loads `ndvi.raster.stac_compute_engine.StacComputeRasterEngine` for `stac`.
- `ndvi/raster/stac_compute_engine.py:114-172` → `StacComputeRasterEngine.render_png()` performs the STAC search, selects an item, resolves assets, loads NDVI data, and encodes a PNG.
- **Failure point**: `ndvi/raster/stac_compute_engine.py:57-87` → `_raise_raster_not_found()` raises `ValidationError` with `detail="Raster not found"` when any of these checks fail:
  - no items returned (`reason="no_items"`) at `ndvi/raster/stac_compute_engine.py:122-129`
  - no best item within window (`reason="no_best_item"`) at `ndvi/raster/stac_compute_engine.py:135-142`
  - missing assets or empty/NaN NDVI (`reason="missing_assets"`) at `ndvi/raster/stac_compute_engine.py:144-170`

## STAC payload used by raster_png

Payload is built in `ndvi/stac_client.py:316-338` and always includes `collections`:

```json
{
  "collections": ["<NDVI_STAC_COLLECTION>"],
  "bbox": [west, south, east, north],
  "datetime": "<start>T00:00:00Z/<end>T23:59:59Z",
  "limit": min(200, max_items)
}
```

How values are derived:

- `bbox` comes from `normalize_bbox(farm)` in `ndvi/services.py:153-171` and is serialized in STAC order (`west, south, east, north`) in `ndvi/stac_client.py:326-333`.
- `start`/`end` are **not** the raw job start/end for raster. They are `request.date ± date_window_days` in `ndvi/raster/stac_compute_engine.py:114-121`, where `request.date` is `job.start or job.end or date.today()` (from `ndvi/tasks.py:71-73`).
- `collections` is required and comes from `NDVI_STAC_COLLECTION` in `ndvi/stac_client.py:305-309`. If it is empty, the client raises a `ValueError` before any request is made.
- Cloud filtering is applied post-response via `filter_items_by_cloud(...)` in `ndvi/stac_client.py:77-87`, which keeps items with missing cloud cover or `cloud_cover <= max_cloud`.

## Item and asset selection logic for raster_png

- Items are chosen by `select_best_item(...)` in `ndvi/stac_client.py:90-110`: first by lowest cloud cover, then by closest date to the target.
- Required assets are **exactly** `NDVI_STAC_ASSET_RED` and `NDVI_STAC_ASSET_NIR` (defaults `B04`, `B08`) pulled from settings in `ndvi/raster/stac_compute_engine.py:49-54`.
- Asset lookup is case-insensitive via `resolve_asset_href(...)` in `ndvi/stac_client.py:113-122`.
- If assets are missing, raster rendering fails immediately with `reason="missing_assets"` and `detail="Raster not found"` at `ndvi/raster/stac_compute_engine.py:144-153`.
- Even if assets are present, `compute_ndvi_stats(ndvi)` returning `None` (all NaNs/empty array) triggers the same `missing_assets` failure at `ndvi/raster/stac_compute_engine.py:163-170`.

## Why STAC 200 + features can still end in “Raster not found”

Based on code, `Raster not found` is raised for three conditions:

1) **No usable items after filtering** (`reason="no_items"`).
   - Items can be filtered out by `max_cloud` (if `eo:cloud_cover`/`cloud_cover` is present and higher than requested), or if `features` is empty.

2) **Items exist but none are “best”** (`reason="no_best_item"`).
   - `select_best_item(...)` re-checks `delta_days <= window_days` even though the STAC search also uses a date window. If the STAC response includes items outside the window (or date parsing fails), all candidates can be filtered out.

3) **Items exist but assets don’t match** (`reason="missing_assets"`).
   - Raster rendering requires `B04`/`B08` (or configured alternatives). The example feature asset keys you observed (`B0`, `B2`, `B3`, `MIR`, `NDVI`) do **not** include `B04`/`B08`. That maps exactly to `missing_assets` in `ndvi/raster/stac_compute_engine.py:144-153`.
   - This can happen when the STAC `collections` filter is set to a non–Sentinel-2 collection (e.g., Sentinel-3/5P), or if the collection returns items without the expected bands. The code *always* sends `collections` (`ndvi/stac_client.py:326-328`), so the actual collection value (from `NDVI_STAC_COLLECTION`) is the likely driver.

## Why refresh_latest / gap_fill can “succeed” while raster_png fails

- For non-raster jobs, `run_ndvi_job()` treats “no points” as success: it only calls `upsert_observations(...)` if points exist and then marks the job successful regardless (`ndvi/tasks.py:56-140`).
- `StacEngine.get_latest()` returns `None` if there is no best item or if asset lookup fails (`ndvi/engines/stac.py:145-170`).
- `StacEngine.get_timeseries()` skips buckets with missing assets or missing stats (`ndvi/engines/stac.py:109-133`), which can result in an empty list. That still marks the job as success in `ndvi/tasks.py:114-140`.
- Raster rendering is stricter: any missing assets or empty NDVI array raises `ValidationError("Raster not found")` and the job is marked failed (`ndvi/raster/stac_compute_engine.py:57-87`, `ndvi/tasks.py:174-182`).

This difference explains why refresh/gap_fill can “succeed” (but potentially with no data) while raster_png fails with `Raster not found`.

## Datetime window assessment (start=end)

- Queue endpoint sets `start` and `end` to the same date (`ndvi/views.py:680-690`).
- Raster search **expands** the date window by `NDVI_STAC_DATE_WINDOW_DAYS` before it calls STAC (`ndvi/raster/stac_compute_engine.py:114-121`).
- The STAC payload always includes `T00:00:00Z` and `T23:59:59Z` for the range (`ndvi/stac_client.py:334-336`), so it is not a zero-length interval even if `start == end`.

Based on code, the failure is **not** caused by a zero-length datetime, unless the STAC service returns items outside the requested window (which would then be filtered out by `select_best_item(...)`). The observed asset keys from non-Sentinel-2 collections are a stronger match to the `missing_assets` failure path.

## Recommended minimal code changes (not implemented)

1) **Surface collection/asset mismatch in errors**: include the configured collection and expected asset names when raising `Raster not found` for `missing_assets` (e.g., log or include in job error details). This would immediately clarify “wrong collection vs. no imagery.”
2) **Validate collection/assets at startup or on first use**: add a lightweight check that `NDVI_STAC_COLLECTION` exists and that a sample item exposes the configured `NDVI_STAC_ASSET_RED/NIR` keys. Fail fast with a clear error if not.
3) **Differentiate missing-assets vs empty-data**: change the `reason` when `compute_ndvi_stats(ndvi) is None` so it doesn’t conflate empty/NaN data with missing assets.

## Suggested debug logging fields

Add (or extend) structured logs around the STAC search and item selection with:

- `job_id`, `farm_id`, `engine`, `bbox` (STAC order), `max_cloud`
- `collections` (value passed in payload)
- `datetime_start`, `datetime_end` (expanded window)
- `feature_count` (pre/post cloud filter)
- `chosen_item_id`, `chosen_item_datetime`
- `asset_keys` for the chosen item
- `red_asset`, `nir_asset` resolved href presence

These would directly confirm whether the failing jobs are selecting items that lack the required Sentinel-2 bands.
