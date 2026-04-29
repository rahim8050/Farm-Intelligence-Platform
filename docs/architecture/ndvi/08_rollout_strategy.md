# NDVI Rollout Strategy

## Current Django modules

- `ndvi/tasks.py`
- `ndvi/services.py`
- `ndvi/views.py`
- `ndvi/farm_state.py`
- `ndvi/models.py`

## Service class mapping

- `NdviV2BackfillService`
- `NdviLatestPromotionService`
- `NdviFarmStatePromotionService`
- `NdviRollbackService`

## Rollout phases

### Phase 1: Shadow compute

- Compute V2 from V1 without changing default API responses.
- Keep V1 as the production response for `/latest/` and `/farm-state/`.
- Backfill historical windows first.

### Phase 2: Dual run

- Return V1 and V2 side by side where the endpoint supports representation selection.
- Compare V1 vs V2 divergence, null rate, source usage, and trend consistency.
- Keep all default user-facing decisions on V1.

### Phase 3: Promotion

- Switch `/latest/` to V2 only when the rollout gates are met.
- Switch `/farm-state/` to V2 only when classification regressions are absent.
- Keep `/timeseries/` exposing V1 and V2 explicitly through representation selection.

### Phase 4: Deprecation

- Keep V1 rows for audit and replay.
- Remove V1 from default responses only after stability is confirmed.

## V2 promotion criteria

- At least 80 percent of V2 outputs have confidence `>= 0.75`
- False cloud-related NDVI drops are eliminated
- No critical farm-state regressions are observed
- Low-confidence or null outputs remain below 20 percent of observations

## Rollback strategy

- Revert `/latest/` and `/farm-state/` to V1 immediately if regressions appear.
- Keep V2 computation running for debugging.
- Investigate confidence thresholds, smoothing, fallback logic, and upstream quality before re-promotion.

## Observability gate before switch

- Confidence distribution over time
- V1 vs V2 divergence
- Percentage of null outputs
- Source usage by source and endpoint
- Anomaly detection for sudden drops and spikes
- Disagreement between sources

## Transaction boundaries

- Promotion changes are configuration-driven, not schema-breaking.
- Backfill writes are isolated from default-response switches.
- Rollback must not delete historical V2 data.

## Safety rule

- V2 must only replace V1 when it demonstrably reduces error, not just complexity.

