# TDD: Phase 1 — Model + Engine

---

## 1. Scope

### In-scope
- Add `index_type` field to `NdviObservation`, `NdviDerivedObservation`, `NdviJob`, `NdviRasterArtifact`
- Drop old unique constraints, create new ones with `index_type`
- Add partial indexes for NDWI queries
- Add `load_ndwi_array()` to `stac_client.py` (keep `load_ndvi_array()` unchanged)
- Add `asset_green` constructor param + `NDWI_FORMULA` to `StacEngine`, `GeeEngine`, `LandsatEngine`
- Add NDWI evalscript to `SentinelHubEngine`
- Add `UnsupportedIndexError` to `ModisEngine` for NDWI
- Add `get_engine(index_type=)` param to `services.py`
- Add `ndwi_*` factory functions to `ENGINE_FACTORIES`
- Migration 0003 with rollback
- All unit tests for engine output, factory creation, formula correctness

### Out-of-scope
- NDWI views, serializers, URLs (Phase 2)
- NDWI quality, fusion, raster (Phase 3)
- NDWI tasks, metrics, schedules (Phase 4)
- Any NDVI model/table renames
- Any NDVI field renames

### Dependencies
- Migration 0003 must be rehearsed on staging < 2s
- NDVI test suite must pass pre- and post-migration

---

## 2. Requirements

### Functional
- `NdviObservation.objects.filter(index_type="NDWI")` returns correct rows
- Existing NDVI queries return same results with or without `index_type` filter
- Each engine (`stac`, `sentinelhub`, `gee`, `landsat`) produces NDWI from Green + NIR bands
- Engine output for known numeric input matches hand-computed NDWI
- MODIS engine raises `UnsupportedIndexError` for NDWI
- `get_engine("ndwi_stac", index_type="NDWI")` returns working engine
- `get_engine("stac", index_type="NDVI")` returns unchanged NDVI engine

### Non-functional
- Migration runs in < 2s on production-size dataset
- Existing NDVI queries do not use new partial indexes (PostgreSQL planner may ignore them, but must not regress)

### Backward Compatibility
- All NDVI API endpoints return identical results pre/post migration
- All NDVI tests pass without modification

---

## 3. Architecture Assumptions

| # | Assumption | Source | Risky? |
|---|-----------|--------|--------|
| A1 | `NdviObservation` keeps its name. No rename to `SpectralObservation`. | 10-design-review | No |
| A2 | No field renames (`selected_ndvi` stays). | 10-design-review | No |
| A3 | Existing NDVI engine classes need constructor param changes (backward-compatible defaults). | 11-implementation-readiness | Medium — existing callers pass no `asset_green` |
| A4 | NDWI formula: `(GREEN - NIR) / (GREEN + NIR)`. Range: [-1, 1]. | 01-architecture | No |
| A5 | SentinelHub NDWI evalscript is structurally identical to NDVI evalscript with band names swapped. | 01-architecture | Low |
| A6 | Unique constraint re-creation is safe with `default="NDVI"` (no existing violations). | 02-data-model | Medium — if existing data violates new constraints, migration fails |

---

## 4. Open Questions

| # | Question | Owner | Resolved by |
|---|----------|-------|-------------|
| Q1 | Does `default="NDVI"` on `CharField` cause a full table rewrite in PostgreSQL? | Infra | Rehearse migration on staging |
| Q2 | Do existing unique constraints have any violations in production? | Infra | Run validation query before migration |
| Q3 | Should `asset_green` default to `"B03_10m"` for StacEngine, or stay `None` and require explicit setting? | Engineering | Decision: default `"B03_10m"` for ndwi variant only; existing NDVI variant keeps `B04_10m` |
| Q4 | Does `NdviPoint` dataclass in `engines/base.py` need a new field for NDWI, or is the `mean` field semantically overloaded? | Engineering | Decision: `mean` stores index value. Consumer interprets by `index_type`. No new field. |

---

## 5. Risk Analysis

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Migration constraint violation (duplicate rows across engines/dates) | Low | High | Run pre-migration validation query: check for rows that would violate new `(index_type, farm, engine, bucket_date, version)` constraint |
| `asset_green` default breaks existing NDVI engine callers | Low | Critical | Default to existing behavior if `asset_green` is `None`; only change behavior when `asset_green` is explicitly set |
| NDWI formula implementation error (Green vs Red flip) | Low | High | Validate against known reference: open water pixel → NDWI ≈ 0.3–0.7 |
| `load_ndwi_array()` copies too much `load_ndvi_array()` internals | Medium | Medium | Extract common helper: `_compute_index(visible_data, nir_data, formula)` |
| `get_engine(index_type=)` leaks into NDVI call paths and changes behavior | Low | Critical | Default `index_type="NDVI"` — NDVI callers unaffected |

---

## 6. Test Matrix

### Unit tests

| Test | Count | File |
|------|-------|------|
| NDWI formula: open water (Green=0.3, NIR=0.05) → ~0.714 | 1 | `test_engines_ndwi.py` |
| NDWI formula: vegetation (Green=0.1, NIR=0.4) → ~-0.6 | 1 | `test_engines_ndwi.py` |
| NDWI formula: bare soil (Green=0.2, NIR=0.2) → 0.0 | 1 | `test_engines_ndwi.py` |
| NDWI formula: zero divisor (Green=0, NIR=0) → NaN | 1 | `test_engines_ndwi.py` |
| SCL mask: classes 0,1,2,3,8,9,10,11 masked for NDWI | 1 | `test_engines_ndwi.py` |
| SCL mask: water (class 6) NOT masked by default | 1 | `test_engines_ndwi.py` |
| ModisEngine with `index_type="NDWI"` raises `UnsupportedIndexError` | 1 | `test_engines_ndwi.py` |
| `get_engine("ndwi_stac")` returns valid engine | 1 | `test_engines_ndwi.py` |
| `get_engine("ndwi_sentinelhub")` returns valid engine | 1 | `test_engines_ndwi.py` |
| `get_engine("ndwi_gee")` returns valid engine | 1 | `test_engines_ndwi.py` |
| `get_engine("ndwi_landsat")` returns valid engine | 1 | `test_engines_ndwi.py` |
| `get_engine("stac", index_type="NDVI")` returns NDVI engine | 1 | `test_engines_ndwi.py` |
| Engine constructor backward compat: no `asset_green` → uses default Red band | 2 | `test_engines_ndwi.py` |

### Integration tests

| Test | Count | File |
|------|-------|------|
| SentinelsHub NDWI evalscript returns correct `(Green - NIR)/(Green + NIR)` | 1 | `test_engines_ndwi.py` |

### Migration tests

| Test | Count | File |
|------|-------|------|
| `0003` forward: `index_type` column exists with `default="NDVI"` | 1 | `test_migrations.py` |
| `0003` forward: existing rows have `index_type="NDVI"` | 1 | `test_migrations.py` |
| `0003` backward: `index_type` column removed | 1 | `test_migrations.py` |
| `0003` backward: old unique constraints restored | 1 | `test_migrations.py` |
| New unique constraint prevents duplicate NDWI + NDVI on same date | 1 | `test_migrations.py` |
| New unique constraint allows NDWI + NDVI on same date (different index_type) | 1 | `test_migrations.py` |

### Regression tests

| Test | Count | File |
|------|-------|------|
| All existing NDVI engine tests pass after migration | Suite | `ndvi/tests/` |
| All existing NDVI service tests pass | Suite | `ndvi/tests/` |
| NDVI `load_ndvi_array()` behavior unchanged | 3 | `test_no_regression.py` |
| NDVI `get_engine("stac")` without `index_type` returns NDVI engine | 1 | `test_no_regression.py` |

### Edge-case tests

| Test | Count | File |
|------|-------|------|
| NDWI with all-NaN input (no valid pixels) | 1 | `test_engines_ndwi.py` |
| NDWI with single-pixel input | 1 | `test_engines_ndwi.py` |
| NDWI engine factory registry prevents duplicate key | 1 | `test_engines_ndwi.py` |
| `get_engine("modis", index_type="NDWI")` error message includes "MODIS" and "NDWI" | 1 | `test_engines_ndwi.py` |

---

## 7. Acceptance Criteria

| # | Criterion | Verification |
|---|-----------|-------------|
| AC1 | Migration 0003 runs in < 2s on staging | Time the migration |
| AC2 | All existing NDVI tests pass | `python -m pytest ndvi/tests/ -x --tb=short` |
| AC3 | All new NDWI engine tests pass | `python -m pytest ndwi/tests/test_engines_ndwi.py -x --tb=short` |
| AC4 | `get_engine("ndwi_stac")` returns engine that produces NDWI values in [-1, 1] | Unit test |
| AC5 | `get_engine("stac")` (no index_type) returns NDVI engine | Unit test |
| AC6 | Engines with `index_type="NDVI"` use Red band (B04/B4) | Constructor arg default |
| AC7 | Engines with `index_type="NDWI"` use Green band (B03/B3) | Constructor arg |
| AC8 | MODIS engine for NDWI raises clear error | Unit test |
| AC9 | Migration forward+backward is idempotent | `migrate ndvi 0003` + `migrate ndvi 0002` + verify |
| AC10 | NDVI baseline confirmed — all endpoints return identical results | Run comparison script against staging |

---

## 8. Rollback Criteria

### Conditions requiring rollback
- Migration takes > 5s on production
- Any NDVI endpoint returns different results after migration
- Any NDVI test fails post-migration

### Rollback procedure
```bash
python manage.py migrate ndvi 0002
```

### Verification after rollback
- [ ] All NDVI endpoints return pre-migration results
- [ ] `index_type` column no longer exists
- [ ] Old unique constraints are restored

---

## A. Semantic Field Review

**Question:** Is `selected_ndvi` / `smoothed_ndvi` acceptable for NDWI?

**Decision: Keep as-is.**

Rationale:
- `NdviDerivedObservation` stores index values in `selected_ndvi` and `smoothed_ndvi` fields. These field names indicate the *computation* (NDVI formula applied to data), not the *semantic interpretation* (water vs vegetation).
- For NDWI, the computation produces water index values, but the field name `selected_ndvi` is technically incorrect (it's not NDVI). However, renaming it to `selected_index_value` would be an API-breaking change.
- Risk: Low. The `index_type` field on the parent `NdviObservation` provides the semantic context. Consumers must check `index_type` to interpret the value.
- Mitigation: Add `index_type` to the response serializer output. Document that `selected_ndvi` means "selected index value" — interpret by `index_type`.

**No refactor.** Add code comment: `# NOTE: Stores any spectral index value. Interpret by observation.index_type.`

---

## B. Migration Validation

### Benchmark methodology
- Run `EXPLAIN ANALYZE` on constraint creation queries beforehand
- Time migration on staging with production-size dataset (row count matched)

### Dataset size assumptions
- Assume up to 10M rows in `NdviObservation` (worst case)
- Adding `CharField` with `default` in PostgreSQL is metadata-only — no table rewrite for 9.2+

### Verification plan
1. Pre-migration: dump row count of `ndvi_ndviobservation`
2. Run `python manage.py migrate ndvi 0003`
3. Post-migration: verify row count unchanged, `index_type="NDVI"` on all rows
4. Run NDVI test suite
5. Verify API endpoints return identical results

### Rollback verification plan
1. Run `python manage.py migrate ndvi 0002`
2. Verify `index_type` column gone
3. Verify old unique constraints exist
4. Run NDVI test suite
5. Verify API endpoints return identical results to pre-migration

---

## C. Future Index Extensibility

**Question:** What changes are required to add NDMI after NDWI for Phase 1?

| NDMI requires | Status | Coupling |
|--------------|--------|----------|
| `index_type="NDMI"` choice added | Add to choices tuple | Minimal |
| NDMI formula: `(NIR - SWIR) / (NIR + SWIR)` | Add to formula registry | Minimal |
| Band config: B08 (NIR) + B11 (SWIR) for Sentinel-2 | New engine variant params | Minimal |
| `load_ndmi_array()` in stac_client | New function (like `load_ndwi_array()`) | Minimal |

**Phase 1 coupling is already zero.** The `index_type` discriminator was designed for extensibility. No changes needed to migration, constraints, or indexes — they already use `index_type` generically.

---

## D. Metrics Strategy Validation

N/A for Phase 1 — metrics are Phase 4. See Phase 4 TDD.

---

## E. API Compatibility Validation

### Existing NDVI endpoint behavior (baseline)
- All endpoints return `NdviObservation` data filtered by engine name
- Endpoints do not filter by `index_type` (implicitly NDVI-only because no NDWI data exists yet)

### Expected post-migration behavior
- NDVI endpoints continue to return NDVI data (unchanged)
- NDWI endpoints (Phase 2) will filter by `index_type="NDWI"`
- `ValidObservationQuerySet` with no `index_type` filter returns all data (both NDVI and NDWI) — this is acceptable for internal use but NDVI views should explicitly filter

### Regression coverage required before approval
- All NDVI endpoint responses must byte-match pre- and post-migration
- Run comparison script on staging

---

## F. Data Integrity Validation

| Test | What it validates |
|------|-------------------|
| Unique constraint: NDWI + NDVI same date allowed | `index_type` isolation |
| Unique constraint: two NDWI same date blocked | `index_type` scope |
| Unique constraint: two NDVI same date still blocked | `index_type` backwards compat |
| Partial index: NDWI queries use index (EXPLAIN ANALYZE) | Performance |
| Cache isolation: N/A for Phase 1 | (Phase 2) |
| Job isolation: N/A for Phase 1 | (Phase 4) |
