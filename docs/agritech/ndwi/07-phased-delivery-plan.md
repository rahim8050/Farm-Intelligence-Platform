# NDWI Phased Delivery Plan

**Document:** 07-phased-delivery-plan.md
**Stage:** Design (pre-implementation)
**Status:** Draft for review

---

## Overview

NDWI delivery is organized into 7 phases, each producing a backward-compatible, deployable increment. The first 3 phases focus on shared infrastructure; phases 4–6 add NDWI-specific logic; phase 7 operationalizes.

**Total estimated effort:** 5–6 weeks with 2 engineers (1 backend, 1 data/infra).

## Phase Breakdown

### P1: Model Generalization (1 week)

**Objective:** Add `index_type` discriminator to existing models. Legacy NDVI unaffected.

**Deliverables:**
- Migration `0003` — add `index_type` to `NdviObservation`, `NdviDerivedObservation`, `NdviJob`, `NdviRasterArtifact`
- Rename Python model classes (`NdviObservation` → `SpectralObservation` with alias)
- Update all unique constraints to include `index_type`
- Update all indexes to lead with `index_type`
- Update `ValidObservationQuerySet` to respect `index_type`
- Add `NdviObservation` as alias for backward compatibility

**Dependencies:** None (standalone migration).
**Effort:** 1 engineer × 1 week.

**Acceptance criteria:**
- [ ] Migration runs in < 5s on staging with production-like data volume
- [ ] All existing NDVI API endpoints return identical results before/after migration
- [ ] All NDVI tests pass (must match pre-migration test output)
- [ ] `SpectralObservation.objects.filter(index_type="NDVI").count()` matches `NdviObservation.objects.count()` pre-migration

---

### P2: Engine Parameterization (1 week)

**Objective:** Parameterize existing engine classes to accept Green band name and NDWI formula.

**Deliverables:**
- Add `asset_green` parameter to `StacEngine`, `GeeEngine`, `LandsatEngine` constructors
- Add `NDWI_FORMULA = lambda green, nir: (green - nir) / (green + nir)` to formula registry
- Create factory functions: `_build_ndwi_stac_engine()`, `_build_ndwi_sentinelhub_engine()`, `_build_ndwi_gee_engine()`, `_build_ndwi_landsat_engine()`
- Add engine factories to `ENGINE_FACTORIES` with `ndwi_*` keys
- `get_engine()` accepts `index_type` parameter
- SentinelHub NDWI evalscript
- StacClient `load_ndvi_array()` → `load_index_array(formula=...)`
- MODIS engine: skip for NDWI (raises `UnsupportedIndexError`)
- Unit tests: each ndwi engine produces correct values for known inputs

**Dependencies:** P1 (model migration complete).
**Effort:** 1 engineer × 1 week.

**Acceptance criteria:**
- [ ] `get_engine("ndwi_stac", index_type="NDWI")` returns working engine
- [ ] `get_engine("stac", index_type="NDVI")` returns NDVI engine (unchanged)
- [ ] Engine output for synthetic input matches hand-computed NDWI
- [ ] `get_engine("modis", index_type="NDWI")` raises `UnsupportedIndexError`
- [ ] All existing NDVI engine tests still pass

---

### P3: NDWI API + Serializers (1 week)

**Objective:** Wire up NDWI endpoints under `/api/v1/farms/<id>/ndwi/`.

**Deliverables:**
- `views/ndwi.py` — parameterized views (derive `index_type="NDWI"` from URL prefix)
- `serializers/ndwi.py` — `NdwiObservationSerializer`, `NdwiTimeseriesRequestSerializer`, etc.
- `urls.py` — `/api/v1/farms/<id>/ndwi/timeseries/`, `/latest/`, `/refresh/`
- Cache layer with `ndwi:cache:` prefix
- Cache TTL: timeseries=86400s, latest=21600s
- Cooldown keys with `ndwi:refresh:` prefix
- `success_response` envelope with `NdwiEnvelope`
- `@extend_schema` decorators for Swagger

**Dependencies:** P2 (engines working).
**Effort:** 1 engineer × 1 week.

**Acceptance criteria:**
- [ ] `GET /api/v1/farms/<id>/ndwi/timeseries/` returns NDWI observations
- [ ] `GET /api/v1/farms/<id>/ndwi/latest/` returns latest NDWI (or null)
- [ ] `POST /api/v1/farms/<id>/ndwi/refresh/` enqueues `NDWI_REFRESH_LATEST` job
- [ ] Cooldown works (900s)
- [ ] Cache hit/miss works
- [ ] All endpoints documented in Swagger (no "No response body")
- [ ] NDVI endpoints completely unchanged

---

### P4: NDWI Quality (0.5 week)

**Objective:** V2 quality scoring for NDWI observations.

**Deliverables:**
- `ndwi/v2_quality.py` — `NdwiQualityEngine` with NDWI-specific thresholds
- `NDWI_SOURCE_WEIGHTS`, `NDWI_CONFIDENCE_WEIGHTS`
- `NdwiV2Result` dataclass
- `process_ndwi_v1_to_v2()` pipeline
- `persist_ndwi_v2_observation()`
- `SpectralDerivedObservation` records with `index_type="NDWI"`
- Unit tests for confidence scoring, outlier detection, null conditions

**Dependencies:** P3 (observations flowing into DB).
**Effort:** 1 engineer × 0.5 week.

**Acceptance criteria:**
- [ ] All V1 observations get V2 processing
- [ ] Confidence formula produces values in [0, 1]
- [ ] Null rate < 20% with initial thresholds
- [ ] Outlier detection flags true outliers
- [ ] Integration: `?representation=v2` returns V2 fields

---

### P5: NDWI Fusion (0.5 week)

**Objective:** Multi-source fusion for NDWI.

**Deliverables:**
- `ndwi/fusion.py` — `NdwiFusionEngine` with NDWI-specific thresholds
- `NDWI_SOURCE_PRIORITY`, `NDWI_CONFIDENCE_DEGRADATION`, `NDWI_CONFIDENCE_THRESHOLDS`
- `ndwi_fuse_observations()` with decision tree
- Conflict detection with `NDWI_CONFLICT_THRESHOLD=0.15`
- Sentinel-1 integration (same boundary as NDVI)
- Water classification post-processing (open_water/wet_soil/dry_soil/vegetation)
- Unit tests for decision tree branches, conflict, fallback

**Dependencies:** P4 (V2 quality available).
**Effort:** 1 engineer × 0.5 week.

**Acceptance criteria:**
- [ ] Fusion returns correct source priority order
- [ ] Conflict detection catches source disagreement
- [ ] Fallback chain works (optional → Landsat)
- [ ] Water classification set in `quality_flags`

---

### P6: NDWI Raster (0.5 week)

**Objective:** PNG raster generation with blue colormap.

**Deliverables:**
- `ndwi/raster/png.py` — `ndwi_to_png_bytes()` with `Blues` colormap
- `ndwi/raster/service.py` — `render_ndwi_png()`
- `ndwi/raster/registry.py` — raster engine entries for `ndwi_*` engines
- Blue colormap control points (white → light blue → medium blue → dark blue)
- Normalization: fixed [-1, 1] → [0, 1] for NDWI (diverging colormap unnecessary)
- `GET /api/v1/farms/<id>/ndwi/raster.png`
- `POST /api/v1/farms/<id>/ndwi/raster/queue`
- ETag + 304 support

**Dependencies:** P3 (API layer).
**Effort:** 1 engineer × 0.5 week.

**Acceptance criteria:**
- [ ] Raster PNG returns valid PNG bytes
- [ ] Blue colormap renders correctly (white=negative, blue=positive)
- [ ] ETag caching works (304 responses)
- [ ] Raster queue enqueues `NDWI_RASTER_PNG` job

---

### P7: Tasks, Metrics, Schedules (1 week)

**Objective:** Operationalize NDWI with Celery tasks, metrics, and scheduled refresh.

**Deliverables:**
- `ndwi/tasks.py` — `run_ndwi_job()`, `enqueue_daily_ndwi_refresh()`, `enqueue_weekly_ndwi_gap_fill()`
- `ndwi/metrics.py` — full `ndwi_*` metric catalog
- Celery Beat schedule additions:
  - `enqueue_daily_ndwi_refresh` (06:00 UTC)
  - `enqueue_weekly_ndwi_gap_fill` (Sunday 06:00 UTC)
- Separate Celery queue: `ndwi_ingestion`, `ndwi_recompute`, `ndwi_analysis`
- Retry policy (same as NDVI)
- Grafana dashboard panels for NDWI
- SLO monitoring

**Dependencies:** P1–P6 complete.
**Effort:** 1 engineer × 1 week.

**Acceptance criteria:**
- [ ] `run_ndwi_job` executes and writes `SpectralObservation(index_type="NDWI")`
- [ ] Daily refresh enqueues jobs for all active farms
- [ ] Gap fill detects and fills missing dates
- [ ] All `ndwi_*` metrics fire on endpoint access
- [ ] Grafana dashboard shows NDWI panels
- [ ] NDVI Celery tasks unaffected

---

## Timeline

```
Week 1  Week 2  Week 3  Week 4  Week 5  Week 6
├─P1──┤ ├─P2──┤ ├─P3──┤ ├─P4+P5┤ ├─P6──┤ ├─P7──┤
      │       │       │       │       │       │
      │       │       │       │       │       │
      ▼       ▼       ▼       ▼       ▼       ▼
    Model   Engine   API     Qual+   Raster  Ops
    Gen     Param    Layer    Fusion          (Tasks+Metrics)
```

Phases P4 and P5 can run in parallel (different engineers). P6 can start as soon as P3 is done.

## Dependencies

| Phase | Depends on | Blocked by |
|-------|-----------|------------|
| P1 | — | — |
| P2 | P1 | Migration complete |
| P3 | P2 | Working NDWI engines |
| P4 | P3 | Observations in DB |
| P5 | P3 | Observations in DB |
| P6 | P3 | API layer |
| P7 | P1–P6 | All prior phases |

## Resource Plan

| Role | P1 | P2 | P3 | P4 | P5 | P6 | P7 |
|------|----|----|----|----|----|----|----|
| Backend Engineer | 1.0 | 1.0 | 1.0 | 0.5 | — | 0.5 | 0.5 |
| Data/Infra Engineer | — | — | — | 0.5 | 0.5 | — | 0.5 |
| **Total** | **1.0** | **1.0** | **1.0** | **1.0** | **0.5** | **0.5** | **1.0** |

## Deployment Plan

| Phase | Deploy type | Verification | Rollback |
|-------|-------------|-------------|----------|
| P1 | Blue-green | Run NDVI integration tests | Revert migration |
| P2 | Rolling | Engine unit tests | Remove factory entries |
| P3 | Rolling | API smoke tests | Remove URL routes |
| P4 | Rolling | Quality unit tests | Disable V2 processing |
| P5 | Rolling | Fusion unit tests | Disable fusion |
| P6 | Rolling | Raster render test | Remove raster URL |
| P7 | Rolling | Dashboard verify | Remove Celery Beat entries |
