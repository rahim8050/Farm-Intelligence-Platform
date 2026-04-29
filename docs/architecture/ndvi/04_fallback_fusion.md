# NDVI Fallback and Fusion

## Current Django modules

- `ndvi/services.py`
- `ndvi/farm_state.py`
- `ndvi/engines/stac.py`
- `ndvi/engines/sentinelhub.py`
- `ndvi/stac_client.py`
- `ndvi/tasks.py`

## Service class mapping

- `NdviFallbackFusionService`
- `NdviEngineRankingService`
- `NdviSourceConflictService`

## Deterministic decision tree

1. Collect all candidates for the same `farm_id` and `bucket_date`.
2. Score each candidate through the V2 quality engine.
3. Discard any candidate whose confidence is below `0.50`.
4. If a Sentinel-2 candidate remains with confidence `>= 0.75`, select it.
5. Otherwise, if a Landsat candidate remains with confidence `>= 0.70`, select it.
6. Otherwise, if a MODIS candidate remains with confidence `>= 0.60`, select it.
7. Otherwise, select the highest-confidence remaining candidate.
8. If confidence is tied, use the priority order Sentinel-2, then Landsat, then MODIS.
9. If no candidate survives, return `NULL`.

## Conflict rule

- If the top two surviving candidates differ by `>= 0.10` NDVI and neither is above `0.75` confidence, return `NULL`.
- Do not average across sources to hide disagreement.

## Confidence degradation on fallback

- Landsat fallback: multiply the final confidence by `0.90`
- MODIS fallback: multiply the final confidence by `0.80`
- Sentinel-1 never changes the NDVI value or the selected source

## Sentinel-1 usage boundary

- Sentinel-1 may explain a drop, not create one.
- It may add context flags such as wet-soil or storm-period context.
- It must not override a valid Sentinel-2 or Landsat numeric observation.

## Transaction boundaries

- Fusion happens before the V2 row is written.
- The selected source, confidence, and quality flags are written atomically with the V2 row.

## Failure behavior

- If no candidate is valid, return `NULL`.
- Do not backfill a numeric value from a weak candidate.
- Do not degrade silently; mark the reason in `null_reason` and `quality_flags`.

