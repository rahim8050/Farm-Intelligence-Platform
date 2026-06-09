# NDWI — Normalized Difference Water Index

**Document:** 00-overview.md
**Stage:** Design (pre-implementation)
**Status:** Draft for review

---

## Business Objective

Provide farm operations teams with a satellite-derived water index (NDWI) for irrigation monitoring, flood detection, drainage assessment, and soil moisture inference. NDWI complements the existing NDVI (vegetation health) product by measuring surface water content at the same spatial and temporal resolution.

## Farm Operations Value

| Use case | Description | Seasonality |
|----------|-------------|-------------|
| Irrigation monitoring | Detect under- or over-irrigated fields from NDWI trends | Growing season |
| Flood detection | Rapid identification of standing water after heavy rainfall | Rainy season |
| Drainage assessment | Identify persistent wet spots that need tile drainage adjustment | Post-harvest |
| Soil moisture proxy | NDWI correlates with topsoil moisture in bare-soil periods | Shoulder seasons |
| Complement to NDVI | Cross-reference NDWI + NDVI for stress diagnosis (e.g., wet stress vs. dry stress) | All seasons |

## Expected Outcomes

1. **API parity with NDVI** — same `/timeseries`, `/latest`, `/refresh`, `/raster.png`, `/raster/queue` endpoints under `/api/v1/farms/<id>/ndwi/`
2. **Same engine coverage** — NDWI available via Stac (Sentinel-2), SentinelHub, GEE (Sentinel-2), Landsat, and MODIS engines.
3. **Same quality pipeline** — V1 raw → V2 quality scoring with confidence, temporal smoothing, outlier rejection.
4. **Same fusion framework** — Multi-source fallback with deterministic decision tree.
5. **Same observability** — Prometheus metrics, Grafana dashboards, SLO-defined uptime.
6. **Shared infrastructure** — No new STAC client, no new circuit breaker, no new task queue — all reused from the NDVI platform.

## Success Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| NDWI data freshness | ≤ 24h from satellite overpass to API | `ndwi_observation_latest_age_seconds` |
| NDWI API availability | ≥ 99.5% | Uptime monitor |
| NDWI data quality | ≥ 75% of observations pass V2 confidence ≥ 0.75 | `ndwi_v2_confidence_bucket` |
| Coverage gap | ≤ 5% of farm-days missing | Gap fill task |
| User adoption | ≥ 2 farm operations teams actively querying NDWI within 4 weeks | API request logs |
| Regression | Zero NDVI regression in production | Comparison run before/after generalization |

## Scope Boundaries

| In scope | Out of scope |
|----------|--------------|
| `/api/v1/farms/<id>/ndwi/*` endpoints | Changes to NDVI `/api/v1/farms/<id>/ndvi/*` behavior |
| NDWI raster PNG with blue colormap | NDWI time-lapse video generation |
| NDWI V1 raw observations | Separate NDWI mobile app or UI |
| NDWI V2 quality + fusion | Automatic irrigation actuator control |
| Shared `SpectralObservation` model with `index_type` discriminator | Renaming or deleting existing `NdviObservation` table in-place |
| 5 engines (stac, sentinelhub, gee, landsat, modis) | Custom UAV / drone NDWI processing |
| Prometheus metrics with `index` label | Per-farm dashboards |

## Assumptions

1. Green band (B03 for Sentinel-2, B3 for Landsat) is available on all target STAC collections.
2. MODIS pre-computed NDVI band cannot be meaningfully converted to NDWI — MODIS engine will not support NDWI (or will provide a separate pre-computed `NDWI` band if available on MODIS collection).
3. The existing STAC client, circuit breaker, retry policy, lock manager, and cache layer require zero changes for NDWI support.
4. Existing NDVI production workloads will not be affected by the model generalization migration (new `index_type` column with default `"NDVI"`).
5. NDWI thresholds (quality, fusion) will need tuning after 2 weeks of production data — initial values are conservative estimates.

## Open Questions

1. Does the existing `NdviObservation` table row count permit an online migration (adding `index_type` column with default)? Or does it require a shadow-table approach?
2. Should MODIS engine for NDWI fall back to a different band/product (e.g., `MCD43A4` for Nadir BRDF-Adjusted Reflectance) rather than skipping MODIS entirely?
3. What is the acceptable NDWI null rate for farm operations teams? (NDVI target: < 20% null.)
4. Should raster PNG generation for NDWI use a diverging colormap (blue-white-brown) or a sequential colormap (white-blue)?
