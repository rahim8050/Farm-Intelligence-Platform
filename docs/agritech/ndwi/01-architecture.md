# NDWI Architecture

**Document:** 01-architecture.md
**Stage:** Design (pre-implementation)
**Status:** Draft for review

---

## Final Architecture Decision

**Hybrid Approach (Option C from ADR-007):** Generalize shared infrastructure (models, services, tasks, views, metrics) with an `index_type` discriminator. Keep per-index logic (formula, band names, quality thresholds, fusion rules, raster colormap) in separate, index-scoped modules.

**Rationale:**
- Maximum code reuse (STAC client, circuit breaker, cache, locks, task queue) — zero changes needed.
- Safe rollout — each phase is backward-compatible; legacy NDVI endpoints never break.
- Scalable — next index (NDMI, EVI, SAVI, NBR) is a configuration change + new module, not a new app.
- Minimal migration risk — one migration adds `index_type` column; existing rows get `"NDVI"`.

## Component Diagram

```
┌─────────────────────────────────────────────────────────┐
│                    Shared Platform                        │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌─────────┐ │
│  │StacClient│  │Services  │  │Tasks     │  │Metrics  │ │
│  │(generic) │  │(generic) │  │(generic) │  │(index   │ │
│  └──────────┘  └──────────┘  └──────────┘  │ label)  │ │
│                                              └─────────┘ │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐               │
│  │Cache     │  │Lock      │  │Circuit   │               │
│  │(generic) │  │Manager   │  │Breaker   │               │
│  └──────────┘  └──────────┘  └──────────┘               │
└─────────────────────────────────────────────────────────┘
                            │
                            ▼ index_type
┌─────────────────────────────────────────────────────────┐
│              Index-Specific Modules                      │
│                                                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌─────────┐ │
│  │ NDVI     │  │ NDWI     │  │ (future) │  │ (future)│ │
│  │ engines  │  │ engines  │  │ EVI      │  │ NBR     │ │
│  │ quality  │  │ quality  │  │ engines  │  │ engines │ │
│  │ fusion   │  │ fusion   │  │ quality  │  │ quality │ │
│  │ raster   │  │ raster   │  │ fusion   │  │ fusion  │ │
│  │ colormap │  │ colormap │  │ raster   │  │ raster  │ │
│  └──────────┘  └──────────┘  └──────────┘  └─────────┘ │
│                                                          │
│  ┌──────────────────────────────────────────────────┐    │
│  │  SpectralObservation (shared model)               │    │
│  │  index_type = "NDVI" | "NDWI" | "EVI" | ...      │    │
│  └──────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
```

## Data Flow

```
Satellite overpass
      │
      ▼
STAC API / SentinelHub / Planetary Computer
      │
      ▼
┌─────────────────────────────────────────┐
│              Engine Layer                │
│  - Discovers STAC items                  │
│  - Downloads Green + NIR bands           │
│  - Applies SCL mask                      │
│  - Computes (Green - NIR)/(Green + NIR)  │
│  - Returns NdwiPoint (index-agnostic)    │
└─────────────────────────────────────────┘
      │
      ▼
┌─────────────────────────────────────────┐
│           Services Layer                 │
│  - Upserts SpectralObservation           │
│    with index_type = "NDWI"              │
│  - Caches response (Redis)               │
│  - Enqueues async job if stale           │
│  - Enforces quota / rate limits          │
└─────────────────────────────────────────┘
      │
      ▼
┌─────────────────────────────────────────┐
│            Quality Layer                 │
│  - Computes NDWI-specific confidence     │
│  - Temporal smoothing (rolling window)   │
│  - Outlier rejection                     │
│  - Outputs NdwiDerivedObservation        │
└─────────────────────────────────────────┘
      │
      ▼
┌─────────────────────────────────────────┐
│            Fusion Layer                  │
│  - Gathers candidates from all engines   │
│  - Applies NDWI-specific thresholds      │
│  - Decision tree with fallback chain     │
│  - Conflict detection                    │
└─────────────────────────────────────────┘
      │
      ▼
┌─────────────────────────────────────────┐
│           API Layer                      │
│  - /api/v1/farms/<id>/ndwi/timeseries   │
│  - /api/v1/farms/<id>/ndwi/latest       │
│  - /api/v1/farms/<id>/ndwi/refresh      │
│  - /api/v1/farms/<id>/ndwi/raster.png   │
│  - /api/v1/farms/<id>/ndwi/raster/queue │
└─────────────────────────────────────────┘
```

## Integration with Existing NDVI Platform

| Component | Integration strategy |
|-----------|---------------------|
| **Models** | `SpectralObservation` replaces `NdviObservation` as shared model with `index_type` discriminator. Existing `NdviObservation` rows get `index_type="NDVI"`. View-level proxy maintains backward compat. |
| **StacClient** | No changes needed. `load_ndvi_array()` is renamed to `load_index_array()` with a formula parameter. Band asset names (Green vs. Red) configured per engine factory. |
| **Engine factories** | `ENGINE_FACTORIES` grows `ndwi_stac`, `ndwi_sentinelhub`, `ndwi_gee`, `ndwi_landsat`, `ndwi_modis` entries. Each calls the same engine class but with `asset_green` + `formula=NDWI_FORMULA`. MODIS NDWI uses MOD09GA surface reflectance (not MOD13Q1). |
| **Services** | `get_engine()` accepts `index_type` parameter. `upsert_observations()` writes to `SpectralObservation` with `index_type` set. Cache keys change from `ndvi:cache:` to `ndwi:cache:`. |
| **Views** | New `ndwi/` URL prefix. Internal view class dispatches by deriving `index_type` from URL prefix. No changes to existing `ndvi/` views. |
| **Tasks** | `run_index_job()` (generic) replaces `run_ndvi_job()`. New `NDWI_REFRESH_LATEST`, `NDWI_GAP_FILL`, `NDWI_BACKFILL`, `NDWI_RASTER_PNG` job types. Celery Beat schedule adds `enqueue_daily_ndwi_refresh`. |
| **Quality** | Separate `ndwi/v2_quality.py` with NDWI-specific thresholds and NDWI formula interpretation. |
| **Fusion** | Separate `ndwi/fusion.py` with NDWI-specific confidence thresholds and conflict detection. |
| **Raster** | Separate `ndwi/raster/` with blue colormap and NDWI-specific normalization. |
| **Metrics** | New `ndwi_*` metrics alongside `ndvi_*`. Future: merge into `spectral_index_*` with `index` label. |

## NDWI Formula

```
NDWI = (Green - NIR) / (Green + NIR)
```

- Sentinels-2: B03 (Green, 10m) - B08 (NIR, 10m)
- Landsat 8/9: B3 (Green, 30m) - B5 (NIR, 30m)
- Same SCL mask as NDVI (classes 0,1,2,3,8,9,10,11 masked)

**Range:** [-1, 1]
- Positive → water/moisture (0.2+ = open water, 0.0-0.2 = wet soil)
- Near zero → bare soil
- Negative → dry surface, vegetation (NDWI is negative for healthy vegetation; this is expected)

## Band Mapping

| Engine | Green band | NIR band | SCL band | Resolution |
|--------|-----------|----------|----------|------------|
| Stac (Sentinel-2) | B03_10m | B08_10m | SCL | 10m |
| SentinelHub | B03 | B08 | SCL | 10m |
| GEE (Sentinel-2) | B03_10m | B08_10m | SCL | 10m |
| Landsat 8/9 | B3 | B5 | (none) | 30m |
| MODIS (MOD09GA) | sur_refl_b04 (500m) | sur_refl_b02 (500m) | state_1km | 500m |
