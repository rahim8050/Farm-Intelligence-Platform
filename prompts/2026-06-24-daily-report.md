# Daily Report — June 24, 2026

## Overview

NDMI Phase 0 platform foundations completed and deployed. Implemented formula registry, engine factories, STAC/SentinelHub support, model changes, views, URLs, and 84+ tests. Fixed mypy errors and credential handling for sentinelhub engine tests. CI coverage still below 96% threshold.

## Commits

### `994af901` — feat(ndmi): Phase 0 platform foundations

| File | Change |
|------|--------|
| `science/formulas/registry.py` | NDVI/NDWI/NDMI formula registry with band requirements |
| `science/formulas/band_registry.py` | Sensor-to-band key mappings (incl. SWIR1 for Sentinel-2) |
| `ndvi/engines/stac.py` | SWIR1 asset constants + NDMI branch in `_build_ndmi_stac_engine()` |
| `ndvi/engines/sentinelhub.py` | `NDMI_EVALSCRIPT` + evalscript dispatch for NDMI/NDWI/NDVI |
| `ndvi/stac_client.py` | `load_ndmi_array()` function |
| `ndvi/services.py` | 5 NDMI engine factories + cache functions + `ENGINE_FACTORIES` entries |
| `ndvi/models.py` | `"NDMI"` added to all 4 `index_type` choice fields |
| `ndvi/views.py` | 5 NDMI view classes (timeseries, latest, refresh, raster png, raster queue) + response schemas |
| `ndvi/urls.py` | 5 NDMI URL patterns under `/api/v1/farms/{id}/ndmi/` |

### `b3304970` — fix(migrations): add NDMI choices migration

- Generated and applied migration `0014` for NDMI choices in `NdviObservation`, `NdviJob`, `NdviRasterArtifact`, `NdviDerivedObservation`

### `3ecfaede` — fix(tests): add NDMI Phase 0 tests to fix CI coverage

- 15 tests for `test_ndmi_engines.py` (StacEngine NDMI branch, sentinelhub evalscript dispatch, engine factories)
- 17 tests for `test_formulas.py` (FORMULA_REGISTRY, compute_index for NDVI/NDWI/NDMI, get_band_asset_key)
- 16 tests for `test_ndmi_views.py` (timeseries, latest, refresh, raster png, raster queue, URL resolution)
- 4 tests for `test_celery_metrics.py` (collect_metrics, error handling, gauges, main)
- 7 new no-regression tests (NDMI factory distinctness, NDMI URL resolution)

### `3f2a9567` — fix(mypy): resolve 8 mypy errors in tests and NDVIEngine protocol

- Added `index_type: str` to `NDVIEngine` Protocol in `ndvi/engines/base.py`
- Fixed generator return types in `test_celery_metrics.py`
- Added `.list()` wrap for index access in `test_celery_metrics.py`
- Added `# type: ignore[attr-defined]` for `self.client.force_authenticate`

### `ab55fedb` — Fix sentinelhub credential check in NDMI engine tests

- Added `_SENTINEL_CREDS` constant + `_make_sentinel_engine()` helper wrapping constructor with `patch.dict(os.environ, ...)`
- All 5 sentinelhub tests now pass (were failing with `ValueError: Sentinel Hub client credentials are required`)
- Added V2 representation tests + gap-fill path test for NDMI timeseries

## CI Status

- Coverage: **95.40%** (needs 96%)
- 5 sentinelhub test failures → **fixed** by credential patch
- Mypy: clean (358 source files, no issues)
- Remaining gap: ~178 uncovered lines
- 65 NDMI tests passing
