# TDD: Phase 3 — Processing Pipeline

---

## 1. Scope

### In-scope
- `ndvi/quality_ndwi.py` — NDWI-specific V2 quality scoring
- `ndvi/fusion_ndwi.py` — NDWI-specific multi-source fusion
- `ndvi/raster/png.py` — `ndwi_to_png_bytes()` with Blues colormap
- `ndvi/raster/service.py` — `render_ndwi_png()` using ndwi engines
- `ndvi/raster/registry.py` — register `ndwi_*` raster engines
- GET `/api/v1/farms/<id>/ndwi/raster.png`
- POST `/api/v1/farms/<id>/ndwi/raster/queue`
- `?representation=v2` now returns real V2 data (not empty)
- Water classification post-processing (open_water/wet_soil/dry_soil/vegetation)

### Out-of-scope
- Celery tasks for quality processing (triggered in Phase 4)
- Daily refresh schedule (Phase 4)
- Metrics for quality/fusion (Phase 4)

### Dependencies
- Phase 2 complete (API layer returns NDWI data, V2 envelope returns empty fields)
- Phase 1 engines produce NDWI observations

---

## 2. Requirements

### Functional
- `NdwiQualityEngine` computes confidence from NDWI-specific weights and thresholds
- Outlier detection uses NDWI-specific thresholds (deviation ≥ 0.25, confidence < 0.70, valid_pixel < 0.60)
- Null conditions per NDWI rules (valid_pixel < 0.25, confidence < 0.45, etc.)
- Fusion decision tree selects best NDWI source per NDWI priority order
- Conflict detection uses NDWI thresholds (conflict ≥ 0.15, confidence cap ≤ 0.70)
- Water classification: `value >= 0.20` → open_water, `>= 0.0` → wet_soil, `>= -0.30` → dry_soil, else → vegetation_dominated
- `ndwi_to_png_bytes()` produces valid PNG with Blues colormap
- `render_ndwi_png()` downloads COG, computes NDWI, applies Blues colormap, returns PNG bytes
- `GET /raster.png` returns cached PNG with ETag + 304 support
- `POST /raster/queue` enqueues NDWI raster job

### Non-functional
- Raster generation should be visually interpretable (water = blue, dry = white/light)
- V2 quality batch processing should complete within task timeout (default 300s)
- Quality engine must handle observations without prior context (rolling window returns lower confidence but does not error)

### Backward Compatibility
- NDVI quality (`ndvi/v2_quality.py`) untouched
- NDVI fusion (`ndvi/fusion.py`) untouched
- NDVI raster (`ndvi/raster/png.py`) colormap unchanged

---

## 3. Architecture Assumptions

| # | Assumption | Source | Risky? |
|---|-----------|--------|--------|
| A1 | NDWI quality formula is `confidence = 0.30*source + 0.20*cloud + 0.20*pixel + 0.15*recency + 0.15*temporal`. | 05-quality-fusion | Medium — weights are untested |
| A2 | NDWI outlier threshold (0.25) is appropriate to avoid flagging irrigation events as outliers. | 05-quality-fusion | Medium — needs production validation |
| A3 | Blues colormap: white → light blue → dark blue (sequential, not diverging). | 05-quality-fusion | Low |
| A4 | NDWI raster normalization: fixed [-1, 1] → [0, 1]. | 07-phased-delivery-plan | Low |
| A5 | `NdviDerivedObservation` stores NDWI V2 data with `index_type="NDWI"`. No separate model. | 11-implementation-readiness | No |

---

## 4. Open Questions

| # | Question | Owner | Resolved by |
|---|----------|-------|-------------|
| Q1 | Should water classification thresholds (0.20, 0.0, -0.30) be configurable via settings or hardcoded? | Data Science | Make `NDWI_WATER_THRESHOLD`, `NDWI_WET_SOIL_THRESHOLD`, `NDWI_DRY_SOIL_THRESHOLD` settings. Defaults as documented. |
| Q2 | Does the Blues colormap need a diverging variant (blue-white-brown) for some use cases? | Farm Ops | Start with sequential Blues. Add diverging variant in Phase 8+. |
| Q3 | How should the quality engine handle NDWI observations that lack prior context? | Engineering | Return lower confidence (capped at 0.49 without context), same as NDVI. |

---

## 5. Risk Analysis

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Water classification thresholds wrong | Medium | Medium — false flags for irrigation | Make configurable via settings. Tune after 2 weeks production data. |
| NDWI outlier threshold (0.25) too high → misses genuine outliers | Medium | Low — extra noise in V2 data | Monitor `ndwi_v2_confidence_bucket`. Tune if confidence distribution is bimodal. |
| NDWI outlier threshold (0.25) too low → flags irrigation as outlier | Medium | Medium — null V2 for irrigated fields | Set initial threshold conservatively (0.30). Tune after validation. |
| Blues colormap renders poorly for near-zero NDWI values | Medium | Low — visual confusion | Validate with farm ops before production. Have diverging colormap ready. |
| Raster engine for `ndwi_stac` downloads Green band instead of Red | Low | High — wrong values | Unit test band selection. |

---

## 6. Test Matrix

### Unit tests — Quality

| Test | Count | File |
|------|-------|------|
| Confidence: all components max (source=1.0, cloud=1.0, pixel=1.0, recency=1.0, temporal=1.0) → 1.0 | 1 | `test_quality_ndwi.py` |
| Confidence: all components min → 0.0 | 1 | `test_quality_ndwi.py` |
| Confidence: typical values → confidence in [0, 1] | 1 | `test_quality_ndwi.py` |
| Source weight: `ndwi_stac` = 1.0, `ndwi_landsat` = 0.80 | 2 | `test_quality_ndwi.py` |
| Null condition: `valid_pixel_fraction < 0.25` → `"low_valid_pixel_fraction"` | 1 | `test_quality_ndwi.py` |
| Null condition: `confidence < 0.45` → `"low_confidence"` | 1 | `test_quality_ndwi.py` |
| Null condition: raw NDWI is None → `"missing_ndwi_value"` | 1 | `test_quality_ndwi.py` |
| Null condition: acquisition time is None → `"missing_acquisition_time"` | 1 | `test_quality_ndwi.py` |
| Null condition: prior V2 count < 4, engine not Sentinel-2 → `"insufficient_rolling_context"` | 1 | `test_quality_ndwi.py` |
| Null condition: outlier rejected → `"outlier_rejected"` | 1 | `test_quality_ndwi.py` |
| Outlier: deviation 0.30 > threshold 0.25, confidence 0.60 < 0.70 → outlier | 1 | `test_quality_ndwi.py` |
| Outlier: deviation 0.20 < threshold 0.25 → not outlier | 1 | `test_quality_ndwi.py` |
| Outlier: deviation 0.30 > 0.25 but confidence 0.80 > 0.70 → not outlier | 1 | `test_quality_ndwi.py` |
| Smoothed NDWI: median of [raw] + prior values (≥ 3 total) | 2 | `test_quality_ndwi.py` |

### Unit tests — Fusion

| Test | Count | File |
|------|-------|------|
| Decision tree: 1 primary (ndwi_stac) with confidence ≥ 0.70 → selects it | 1 | `test_fusion_ndwi.py` |
| Decision tree: no primary → Landsat fallback with confidence ≥ 0.65 | 1 | `test_fusion_ndwi.py` |
| Decision tree: no qualifying source → sort by (confidence desc, priority asc) | 1 | `test_fusion_ndwi.py` |
| Decision tree: no candidates → NULL | 1 | `test_fusion_ndwi.py` |
| Conflict: top 2 differ by ≥ 0.15 NDWI, both confidence ≤ 0.70 → NULL with source_disagreement | 1 | `test_fusion_ndwi.py` |
| Conflict: differ by 0.10 < threshold 0.15 → no conflict | 1 | `test_fusion_ndwi.py` |
| Source priority: `ndwi_stac` > `ndwi_sentinelhub` > `ndwi_gee` > `ndwi_landsat` | 1 | `test_fusion_ndwi.py` |
| Confidence degradation: `ndwi_landsat` degraded by 0.90 | 1 | `test_fusion_ndwi.py` |

### Unit tests — Water classification

| Test | Count | File |
|------|-------|------|
| `classify_ndwi(0.50)` → `"open_water"` | 1 | `test_fusion_ndwi.py` |
| `classify_ndwi(0.10)` → `"wet_soil"` | 1 | `test_fusion_ndwi.py` |
| `classify_ndwi(-0.10)` → `"dry_soil"` | 1 | `test_fusion_ndwi.py` |
| `classify_ndwi(-0.50)` → `"vegetation_dominated"` | 1 | `test_fusion_ndwi.py` |
| `classify_ndwi(0.0)` → `"wet_soil"` (boundary) | 1 | `test_fusion_ndwi.py` |
| `classify_ndwi(-0.30)` → `"dry_soil"` (boundary) | 1 | `test_fusion_ndwi.py` |

### Unit tests — Raster

| Test | Count | File |
|------|-------|------|
| `ndwi_to_png_bytes()` returns valid PNG bytes | 1 | `test_raster_ndwi.py` |
| Blues colormap: NDWI=1.0 → dark blue | 1 | `test_raster_ndwi.py` |
| Blues colormap: NDWI=-1.0 → white | 1 | `test_raster_ndwi.py` |
| Blues colormap: NDWI=0.0 → light blue | 1 | `test_raster_ndwi.py` |
| Normalization: fixed [-1, 1] → [0, 1] | 1 | `test_raster_ndwi.py` |
| `ndwi_to_png_bytes()` on all-NaN input → valid PNG or error | 1 | `test_raster_ndwi.py` |

### Integration tests — Raster

| Test | Count | File |
|------|-------|------|
| GET raster.png returns 200 with PNG content-type | 1 | `test_raster_ndwi.py` |
| GET raster.png with ETag: If-None-Match → 304 | 1 | `test_raster_ndwi.py` |
| POST raster/queue returns 200 with job_id | 1 | `test_raster_ndwi.py` |
| GET raster.png missing `date` param → 400 | 1 | `test_raster_ndwi.py` |

### Integration tests — V2 representation

| Test | Count | File |
|------|-------|------|
| GET timeseries with `?representation=v2` returns populated V2 fields | 1 | `test_views_ndwi.py` |
| GET latest with `?representation=v2` returns V2 fields | 1 | `test_views_ndwi.py` |

### Regression tests

| Test | Count | File |
|------|-------|------|
| NDVI V2 quality unchanged (same confidence output) | 3 | `test_no_regression.py` |
| NDVI fusion unchanged (same decision tree output) | 3 | `test_no_regression.py` |
| NDVI raster colormap unchanged (RdYlGn) | 1 | `test_no_regression.py` |
| NDVI raster endpoints unaffected | 2 | `test_no_regression.py` |

---

## 7. Acceptance Criteria

| # | Criterion | Verification |
|---|-----------|-------------|
| AC1 | Quality confidence formula produces values in [0, 1] | Unit tests |
| AC2 | Null rate < 20% with initial thresholds | Integration test on realistic data |
| AC3 | Outlier detection does not flag known non-outliers | Edge-case tests |
| AC4 | Fusion returns correct source priority | Unit tests |
| AC5 | Water classification returns correct label for known values | Unit tests |
| AC6 | `ndwi_to_png_bytes()` produces visualizable PNG | Integration test (render and inspect) |
| AC7 | Raster PNG caching works (ETag → 304) | Integration test |
| AC8 | V2 representation returns real values (not empty) | Integration test |
| AC9 | All NDVI processing pipelines unchanged | Regression tests |

---

## 8. Rollback Criteria

### Conditions requiring rollback
- NDWI water classification produces clearly wrong labels (verified by farm ops)
- NDWI raster produces unreadable images
- V2 integration causes 500 errors on NDWI endpoints
- NDVI quality pipeline degrades (wrong confidence values)

### Rollback procedure
```bash
git revert <phase-3-commit>
# If NDWI observations with V2 data exist, they remain in DB (harmless)
```

### Verification after rollback
- [ ] NDWI V2 fields return empty (pre-Phase-3 behavior)
- [ ] NDVI quality/fusion output matches pre-deployment baseline
- [ ] NDWI raster endpoints return 404

---

## A. Semantic Field Review

**Question:** `selected_ndvi` field stores NDWI V2 quality values. Acceptable?

**Decision: Keep as-is.** Same rationale as Phase 1. The field stores the output of a computation; `index_type` provides semantic context. Document: `# Stores selected index value. Interpret by index_type.`

---

## B. Migration Validation

N/A for Phase 3. No DB changes.

---

## C. Future Index Extensibility

**Question:** What changes are required to add NDMI after NDWI for Phase 3?

| NDMI requires | Add |
|--------------|-----|
| Quality config with NDMI thresholds | New `NDMI_CONFIDENCE_WEIGHTS`, `NDMI_SOURCE_WEIGHTS` |
| Fusion config with NDMI priority | New `NDMI_SOURCE_PRIORITY`, `NDMI_CONFIDENCE_THRESHOLDS` |
| Raster colormap for NDMI | New colormap (e.g., green-to-brown for moisture) |

**Remaining coupling:** Quality and fusion are fully parameterized via configuration dictionaries and threshold settings. Adding NDMI requires no new code files — only new config entries. This is the desired outcome of the NDWI platform effort.

**Exception:** Raster colormap is code (control points). Could be parameterized via settings in a future refactoring, but acceptable for now.

---

## D. Metrics Strategy Validation

N/A for Phase 3 — metrics are Phase 4.

---

## E. API Compatibility Validation

### Existing NDVI endpoint behavior
- V2 representation returns `NdviDerivedObservation` fields
- Raster PNG uses RdYlGn colormap
- Raster queue enqueues NDVI raster job

### Expected NDWI behavior
- V2 representation returns `NdviDerivedObservation(index_type="NDWI")` fields
- Raster PNG uses Blues colormap
- Raster queue enqueues NDWI raster job

### Regression coverage required before approval
- [ ] NDVI V2 response body unchanged (compare byte-for-byte)
- [ ] NDVI raster PNG unchanged (compare hash)
- [ ] NDVI raster queue enqueues NDVI job type (not NDWI)

---

## F. Data Integrity Validation

| Test | What it validates |
|------|-------------------|
| NDWI V2 stored with `index_type="NDWI"` | Row isolation |
| NDWI V2 `selected_ndvi` contains NDWI values (not NDVI) | Correct pipeline |
| NDWI + NDVI V2 on same date stored correctly (different rows) | Constraint isolation |
| Raster file stored with `index_type="NDWI"` in `NdviRasterArtifact` | Raster isolation |
| ETag for NDWI raster does not collide with NDVI raster ETag | Cache isolation |
| Water classification stored in `quality_flags["ndwi_water_class"]` | Field isolation |
