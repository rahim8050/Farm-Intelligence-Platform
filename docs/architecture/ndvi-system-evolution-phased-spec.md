# NDVI System Evolution - Phased Architecture and Implementation Spec

**Status:** Active architecture spec  
**Scope:** NDVI ingestion, quality control, fusion, API exposure, rollout guardrails  
**Applies to:** Django + DRF + async workers

This document consolidates the NDVI evolution discussions into one phased architecture spec. It defines the target system, the rollout phases, the exit criteria, and the guardrails required to move from raw single-source NDVI to a confidence-aware multi-source decision platform.

---

## 1. Purpose

The NDVI system must evolve from a single-source, raw-observation pipeline into a multi-source, confidence-aware platform that:

- avoids misleading vegetation signals under clouds and poor scene quality
- exposes confidence explicitly
- degrades gracefully when data is missing or unreliable
- supports multiple satellite sources without forcing a value

Core guarantees:

- Prefer missing data over incorrect data
- Never fabricate NDVI
- Never silently degrade quality
- Every accepted value must be explainable

---

## 2. Engine Strategy

| Role | Engine | Responsibility |
|---|---|---|
| Primary | Sentinel-2 L2A via CDSE STAC | Field-level NDVI |
| Fallback | Landsat Collection 2 SR | Backup signal |
| Continuity | MODIS NDVI | Temporal continuity |
| Signal-only | Sentinel-1 | Context only, not NDVI |
| Batch / backfill | Google Earth Engine | Offline or historical processing |

Rules:

- Do not average across sensors blindly.
- Select the highest-confidence valid source.
- Any fallback must reduce confidence.
- Sentinel-1 must never produce NDVI.

---

## 3. System Architecture

```text
[Satellite Sources]
    ↓
[Engine Adapters]
    ↓
[V1 Layer - Raw Observations]
    ↓
[V2 Layer - Quality Engine]
    ↓
[API Layer]
    ↓
[Consumers / Alerts / UI]
```

### Layer responsibilities

- V1 is immutable raw truth.
- V2 is the decision layer.
- API endpoints expose V1 by default during rollout, then V2 where safe.

---

## 4. Core Design Rules

### Rule 1 - V1 is immutable

- One record per acquisition per engine.
- No smoothing.
- No fusion.
- Full provenance required.

### Rule 2 - V2 is decision-grade

- All alerts and farm-state use V2.
- V2 may return `null`.
- V2 must carry confidence.

### Rule 3 - Confidence over completeness

- Missing is better than wrong.
- No fabricated fallback values.

### Rule 4 - Source priority over blending

- Best valid source wins.
- No naive blending.
- Conflict resolution must be deterministic.

---

## 5. Data Model Surface

### V1Observation

- `farm: ForeignKey(Farm, on_delete=CASCADE, related_name="v1_observations")`
- `engine: CharField`
- `source_scene_id: CharField`
- `acquisition_at: DateTimeField`
- `bucket_date: DateField`
- `raw_ndvi_mean: FloatField`
- `raw_ndvi_min: FloatField | null`
- `raw_ndvi_max: FloatField | null`
- `sample_count: PositiveIntegerField`
- `cloud_fraction: FloatField | null`
- `valid_pixel_fraction: FloatField | null`
- `quality_flags: JSONField`
- `provider_payload: JSONField`
- `created_at: DateTimeField`
- `updated_at: DateTimeField`

Constraints:

- unique `farm + engine + source_scene_id`
- unique `farm + engine + bucket_date` if one acquisition per bucket is enforced
- indexes on `farm + engine + bucket_date`, `engine + acquisition_at`

### V2Observation

- `farm: ForeignKey(Farm, on_delete=CASCADE, related_name="v2_observations")`
- `engine: CharField`
- `v1_observation: OneToOneField(V1Observation, on_delete=CASCADE, related_name="v2_observation")`
- `bucket_date: DateField`
- `source: CharField`
- `selected_ndvi: FloatField | null`
- `smoothed_ndvi: FloatField | null`
- `confidence: FloatField`
- `confidence_components: JSONField`
- `quality_flags: JSONField`
- `is_null: BooleanField`
- `null_reason: CharField | null`
- `created_at: DateTimeField`
- `updated_at: DateTimeField`

Constraints:

- unique `v1_observation`
- unique `farm + engine + bucket_date`
- indexes on `farm + engine + bucket_date`, `engine + confidence`, `source + bucket_date`

### FarmState

- `farm: ForeignKey(Farm, on_delete=CASCADE, related_name="farm_states")`
- `engine: CharField`
- `window_start: DateField`
- `window_end: DateField`
- `state: CharField`
- `mean_ndvi: FloatField | null`
- `max_ndvi: FloatField | null`
- `trend: FloatField | null`
- `coverage_pct: FloatField | null`
- `confidence: FloatField`
- `observation_count: PositiveIntegerField`
- `source_mix: JSONField`
- `quality_flags: JSONField`
- `created_at: DateTimeField`
- `updated_at: DateTimeField`

Constraints:

- unique `farm + engine + window_start + window_end`
- indexes on `farm + engine + window_end`, `engine + state`

---

## Service Layer

### Ingestion service

- Input: normalized `RawObservation`
- Output: persisted `V1Observation` or rejection reason
- Idempotency: `farm + engine + source_scene_id`
- Retry: only DB write retries
- Failure behavior: reject on invalid quality gates

### V1 persistence service

- Input: `RawObservation`
- Output: `V1Observation`
- Idempotency: same as ingestion
- Retry: DB write retry only
- Failure behavior: never mutate existing raw rows

### V2 quality engine service

- Input: `V1Observation`
- Output: `V2Observation | null`
- Idempotency: one V2 per V1
- Retry: DB write retry only
- Failure behavior: return null on low confidence or outlier rejection

### Fusion service

- Input: candidate V2 rows for a farm and bucket
- Output: selected V2 row or null
- Idempotency: deterministic ranking only
- Retry: none
- Failure behavior: return null when no candidate survives

### Fallback selector

- Input: candidate sources for the same farm and bucket
- Output: chosen source or null
- Idempotency: deterministic ordering
- Retry: none
- Failure behavior: no silent blending

---

## Engine Adapter Interface

### Contract

```python
fetch_observations(geometry, date_range) -> list[Observation]
fetch_latest(geometry, lookback_days) -> Observation | None
```

### Normalized observation fields

- `engine`
- `scene_id`
- `acquisition_at`
- `bucket_date`
- `raw_ndvi_mean`
- `raw_ndvi_min`
- `raw_ndvi_max`
- `sample_count`
- `cloud_fraction`
- `valid_pixel_fraction`
- `quality_flags`
- `provider_payload`

### Adapter rules

- Normalize provider-specific response shapes before persistence.
- Do not leak provider payloads into API responses.
- Sentinel-1 adapters only provide context metadata, not NDVI rows.

---

## Pipeline Execution Flow

### Triggers

- Cron for daily refresh, weekly backfill, and farm-state recomputation
- Event for manual refresh
- Stream consumer for async job execution

### Ordering guarantees

- V1 persistence always happens before V2 materialization.
- Farm-state recomputation always happens after V2 persistence.
- Per `(farm, engine, bucket_date)` processing is serialized.

### Steps

1. Fetch source scene or scenes.
2. Normalize into `RawObservation`.
3. Apply Phase 1 validity gates.
4. Persist `V1Observation`.
5. Build `V2Observation`.
6. Persist V2 if accepted.
7. Recompute `FarmState` from V2 only.
8. Emit metrics and structured logs.

### Deduplication

- Ingestion dedupe key: `farm + engine + source_scene_id`
- V2 dedupe key: `v1_observation`
- Farm-state dedupe key: `farm + engine + window_start + window_end`

### Retry and backoff

- Upstream fetch: `5s -> 30s -> 120s`, max 3 attempts
- DB write: retry twice after connection refresh
- Deterministic rejection: no retry

---

## 6. Phase 1 - STAC Hardening

### Objective

Ensure raw Sentinel-2 NDVI is scientifically valid before any V2 processing.

### Actions

- Apply Sentinel-2 SCL masking.
- Remove:
  - cloud
  - cloud shadow
  - cirrus
  - saturated / defective pixels
  - optionally water
- Compute:
  - `valid_pixel_fraction = valid_pixels / total_pixels`
- Reject observations when:
  - `valid_pixel_fraction < 0.30`
- Persist in V1:
  - `scene_id`
  - `acquisition_at`
  - `cloud_fraction`
  - `valid_pixel_fraction`
  - `quality_flags`

### Outcome

- No cloud-driven NDVI drops in raw stored observations.
- Fewer observations, but higher-quality ones.

### Tradeoff

- Gaps will increase.
- Signal quality will improve.

---

## 7. Phase 2 - V2 Quality Layer

### Objective

Convert raw NDVI into decision-grade NDVI.

### Inputs

- V1 observation
- prior V2 history for the same farm and engine
- source metadata

### Confidence formula

Use normalized values in `[0, 1]`:

- `source_weight`
  - Sentinel-2 = `1.00`
  - Landsat = `0.80`
  - MODIS = `0.60`
- `cloud_weight = 1 - clamp(cloud_fraction, 0, 1)`
- `valid_pixel_weight = clamp(valid_pixel_fraction, 0, 1)`
- `recency_weight = max(0, 1 - age_days / 14)`
- `temporal_consistency_weight = max(0, 1 - abs(raw_ndvi_mean - rolling_median) / 0.20)`

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

### Rolling median

- Window size: `5`
- Use valid prior V2 values only.
- If fewer than `3` valid prior values exist, cap confidence at `0.49`.

### Outlier rejection

Reject when all are true:

- `abs(raw_ndvi_mean - rolling_median) >= 0.15`
- `confidence < 0.75`
- `valid_pixel_fraction < 0.70`

### Null-return conditions

Return `null` when any are true:

- `valid_pixel_fraction < 0.30`
- `confidence < 0.50`
- `raw_ndvi_mean is null`
- `acquisition_at is null`
- no usable rolling context and source is not Sentinel-2
- outlier rejection triggers

### Smoothed value

- `smoothed_ndvi = median([raw_ndvi_mean] + prior_valid_selected_ndvi_window)`
- If fewer than `3` valid values exist, do not smooth.

### Quality flags

Set flags explicitly:

- `cloud_heavy`
- `partial_tile`
- `low_valid_pixel_fraction`
- `low_confidence`
- `outlier_removed`
- `fallback_used`
- `source_disagreement`
- `s1_context_wet_soil`

### Outcome

- Stable NDVI trends
- False dips and spikes suppressed
- Explicit null behavior when quality is insufficient

---

## 8. Phase 3 - Multi-Engine Fallback

### Objective

Keep coverage stable when Sentinel-2 is unavailable or unreliable.

### Priority order

1. Sentinel-2
2. Landsat
3. MODIS

### Deterministic decision tree

1. Gather candidates for the same `farm_id + bucket_date`.
2. Score every candidate through the V2 quality engine.
3. Discard all candidates where `confidence < 0.50` or null conditions trigger.
4. If one Sentinel-2 candidate remains and `confidence >= 0.75`, select it.
5. Else if one Landsat candidate remains and `confidence >= 0.70`, select it.
6. Else if one MODIS candidate remains and `confidence >= 0.60`, select it.
7. Else select the remaining candidate with the highest confidence.
8. If tied, break ties by source priority:
   - Sentinel-2
   - Landsat
   - MODIS
9. If no candidate survives, return `NULL`.

### Confidence degradation on fallback

- Landsat selected: multiply confidence by `0.90`
- MODIS selected: multiply confidence by `0.80`

### Conflict rule

If the top two surviving candidates differ by `>= 0.10` NDVI and neither exceeds `0.75` confidence, return `NULL`.

### Outcome

- Continuity without blind blending
- Controlled degradation instead of false certainty

---

## 9. Phase 4 - Fusion and Intelligence

### Objective

Handle real-world inconsistencies without hiding uncertainty.

### Features

- Cross-source disagreement detection
- Sentinel-1 context for wet soil and anomaly explanation
- Rule-based fusion

### Decision rule

- If one source has higher confidence, use it.
- If two sources are similar in confidence but disagree materially, return `NULL`.
- Sentinel-1 only affects context and flags, not NDVI selection.

### Outcome

- Explainable NDVI
- Reduced misinterpretation
- No blind consensus from conflicting sources

---

## 10. Phase 5 - API Evolution

### Endpoints

- `GET /api/v1/timeseries/`
- `GET /api/v1/latest/`
- `GET /api/v1/farm-state/`
- `GET /api/v1/raster.png`

### API behavior

#### `/timeseries/`

- Default response is V1 during rollout.
- `?representation=v2` returns V2 payload.
- V2 includes:
  - `smoothed_ndvi`
  - `confidence`
  - `source`
  - `quality_flags`

#### `/latest/`

- Default to V2 after migration window.
- Return `null` if confidence is low.

#### `/farm-state/`

- V2 only after promotion.
- Include:
  - `confidence`
  - `observation_count`
  - `source_mix`

#### `/raster.png`

- V1 is default.
- V2 composite is optional.

### Backward compatibility

- Keep endpoints stable.
- Extend response payloads additively.
- Use query params for representation selection.

---

## 11. Phase 6 - Operational Hardening

### Required controls

- Circuit breakers per source
- Retry with exponential backoff
- Caching for:
  - catalog responses
  - derived V2 outputs
- Monitoring for:
  - low-confidence rate
  - null output rate
  - fallback frequency
  - source disagreement
  - NDVI anomalies
  - stale farms

### Outcome

- Stable production behavior under failure
- Measurable quality and degradation behavior

---

## 12. Phase Exit Criteria

### Phase 1

- At least 95% of stored observations include:
  - `valid_pixel_fraction`
  - `cloud_fraction`
  - `quality_flags`
  - `scene_id`
  - `acquisition_at`
- Cloud-contaminated scenes no longer produce extreme NDVI drops.
- Observations below the valid-pixel threshold are consistently rejected.

### Phase 2

- V2 outputs are generated for at least 90% of V1 observations.
- Confidence scores are stable and within expected distribution.
- Temporal smoothing removes visible noise without flattening trends.

### Phase 3

- All engines are operational.
- Fallback works deterministically.
- Confidence degrades correctly.

### Phase 4

- Disagreement is detected.
- Fusion is explainable.
- No blind merging.

### Phase 5

- API exposes V2 safely.
- Backward compatibility is maintained.

### Phase 6

- System remains stable under failure.
- Monitoring is complete.
- No cascading errors.

---

## 13. Dual-Run Validation

Run V1 and V2 in parallel for a validation window of 2 to 4 weeks.

Track:

- V1 vs V2 NDVI divergence
- number of suppressed low-confidence points
- frequency of V2 null outputs
- NDVI trend consistency over time
- percentage of null outputs
- source usage:
  - Sentinel-2
  - Landsat
  - MODIS
- anomaly detection:
  - sudden NDVI drops
  - sudden NDVI spikes
- disagreement between sources

V2 promotion criteria:

- at least 80% of V2 outputs have confidence `>= 0.75`
- false cloud-related NDVI drops are eliminated
- no critical regressions in farm-state classification
- low-confidence / null rate stays below 20% of observations

---

## 14. Safe Rollout Strategy

### Step 1 - Shadow mode

- V2 is computed but not exposed by default.
- Accessible through `?representation=v2`.

### Step 2 - Soft exposure

- V1 and V2 are exposed side by side.
- UI and consumers can compare both.

### Step 3 - Default switch

- `/latest/` defaults to V2.
- `/farm-state/` uses V2 only.

### Step 4 - Deprecation

- V1 is retained for audit and debugging.
- V1 is removed from default responses only after stability is confirmed.

---

## 15. Rollback Strategy

If issues are detected:

- immediately revert `/latest/` and `/farm-state/` to V1
- keep V2 computation running for debugging
- investigate:
  - confidence thresholds
  - smoothing parameters
  - fallback logic

---

## 16. Observability and Metrics

### Metrics

- `ndvi_v1_observations_ingested_total{engine}`
- `ndvi_v1_observations_rejected_total{engine,reason}`
- `ndvi_v2_observations_materialized_total{engine,source}`
- `ndvi_v2_null_outputs_total{endpoint,engine}`
- `ndvi_v2_confidence_bucket{engine,source}`
- `ndvi_source_usage_total{source,endpoint}`
- `ndvi_fallback_used_total{from_source,to_source}`
- `ndvi_source_disagreement_total{pair}`
- `ndvi_anomaly_total{type,engine}`
- `ndvi_upstream_failure_total{engine,error_class}`
- `ndvi_retry_total{stage,reason}`
- `ndvi_pipeline_duration_seconds{stage}`
- `ndvi_stale_farms_total{engine}`

### Logging fields

- `request_id`
- `job_id`
- `farm_id`
- `engine`
- `source`
- `scene_id`
- `acquisition_at`
- `bucket_date`
- `v1_observation_id`
- `v2_observation_id`
- `confidence`
- `cloud_fraction`
- `valid_pixel_fraction`
- `quality_flags`
- `decision`
- `decision_reason`
- `retry_count`
- `error_class`

### Traceability

- Every V2 row must reference exactly one V1 row.
- Every farm-state row must record the V2 source mix used.
- Every null decision must store `null_reason`.

---

## 17. Failure Modes and Safeguards

### Cloud-heavy weeks

- Return `null` when no candidate reaches confidence `>= 0.75`.
- Do not smooth cloud-heavy periods into false stability.
- Mark `cloud_heavy` and `low_confidence` explicitly.

### Missing data

- Preserve sparse V1 as-is.
- Keep V2 null until a valid candidate exists.
- Queue backfill, but do not fabricate values.

### Upstream API failure

- Retry with explicit backoff.
- Fall back by priority when retries are exhausted.
- If all sources fail, return `null` and mark `upstream_failure`.

### Partial tile coverage

- Reject at V1 when `valid_pixel_fraction < 0.30`.
- Mark `partial_tile` when `0.30 <= valid_pixel_fraction < 0.70`.
- Do not promote partial tiles unless confidence is still high and no better candidate exists.

### Global safeguards

- Never fabricate NDVI.
- Never silently degrade quality.
- Never overwrite V1 with V2.
- Never average across sources unless the decision tree explicitly permits it.

---

## 18. Final Safety Rule

> V2 must only replace V1 when it demonstrably reduces error, not just complexity.
