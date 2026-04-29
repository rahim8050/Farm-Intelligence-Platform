# NDVI V2 Quality Engine

## Current Django modules

- `ndvi/services.py`
- `ndvi/farm_state.py`
- `ndvi/tasks.py`
- `ndvi/models.py`

## Service class mapping

- `NdviV2QualityService`
- `NdviObservationWindowService`
- `NdviQualityFlagsService`

These services can live in `ndvi/services.py` initially and be split later without changing behavior.

## Inputs

- One persisted V1 row from `NdviObservation`
- Prior valid V2 rows for the same farm and engine
- Request window metadata

## Formula

Use the following weights:

- `source_weight = 1.00` for Sentinel-2
- `source_weight = 0.80` for Landsat
- `source_weight = 0.60` for MODIS
- `cloud_weight = 1 - clamp(cloud_fraction, 0, 1)`
- `valid_pixel_weight = clamp(valid_pixel_fraction, 0, 1)`
- `recency_weight = max(0, 1 - age_days / 14)`
- `temporal_consistency_weight = max(0, 1 - abs(raw_ndvi_mean - rolling_median) / 0.20)`, with `rolling_median` computed over the previous 5 valid V2 values

Final confidence:

```text
confidence =
  0.30 * source_weight +
  0.25 * cloud_weight +
  0.25 * valid_pixel_weight +
  0.10 * recency_weight +
  0.10 * temporal_consistency_weight
```

Clamp final confidence to `[0, 1]`.

## Thresholds

- Rolling median window: `5`
- Outlier threshold: absolute NDVI delta `0.15`
- Accept threshold: `0.75`
- Low-confidence threshold: `0.50`
- Valid-pixel rejection threshold: `0.30`

## Null-return conditions

Return `NULL` if any of the following are true:

- `valid_pixel_fraction < 0.30`
- `confidence < 0.50`
- `raw_ndvi_mean is null`
- no valid prior V2 context exists and the source is not Sentinel-2
- outlier rejection triggers

## Output fields

- `selected_ndvi`
- `smoothed_ndvi`
- `confidence`
- `confidence_components`
- `quality_flags`
- `is_null`
- `null_reason`

## Transaction boundaries

- Read prior V2 history outside the write lock.
- Compute quality in memory.
- Persist the final V2 row in one atomic block.
- Do not hold the transaction open during upstream fetches.

## Backfill strategy

- Backfill V2 from historical V1 rows in ascending `(farm, engine, bucket_date)` order.
- Recompute the same V1 row idempotently until the V2 unique constraint is satisfied.
- Run backfill in shadow mode before switching defaults.

## Failure behavior

- If the input row is low quality, write a null V2 record only if the blueprint requires auditability.
- If the persistence step fails, retry the DB write only after reconnecting.
- Do not soften a null into a numeric value.

