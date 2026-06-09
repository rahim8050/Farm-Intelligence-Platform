# ADR-007: NDWI Architecture Decision Record

**Document:** 09-adr.md
**Status:** Draft for review
**Date:** 2026-06-09

---

## Context

We need to add NDWI (Normalized Difference Water Index) as a new spectral index product. NDWI shares satellite sources, STAC infrastructure, model schema, caching, and task infrastructure with the existing NDVI product. The key difference is NDWI uses the Green band (B03/B3) instead of the Red band (B04/B4).

The decision is: **should NDWI be a separate Django app (copy-paste), a generalized spectral index platform (refactor NDVI), or a hybrid (generalize shared infrastructure, keep per-index modules separate)?**

## Options

### Option A: Dedicated `ndwi/` App

**Description:** Create a new Django app `ndwi/` duplicating `ndvi/` structure with band names and formulas swapped. Everything is independent — models, engines, views, serializers, tasks, metrics, tests.

**Pros:**
- Zero risk to NDVI production data
- Fastest initial implementation (only needs NDWI-specific code)
- Independent deployment and rollback

**Cons:**
- Does not scale — adding EVI, SAVI, NBR, etc. requires another app each time
- 3-year maintenance cost is O(n) where n = number of indices
- Bug fixes must be applied to N copies of the same code
- ~5000 lines of duplicated code

**Effort:** 4–6 weeks
**Migration risk:** Low (new tables, no NDVI changes)
**3-year maintenance:** High

### Option B: Generalized Spectral Index Platform

**Description:** Refactor all NDVI components into a generic index platform. Models become `SpectralObservation`, services accept `index_type`, views route by `?index=NDVI|NDWI|...`, metrics use `index` label.

**Pros:**
- Adding a new index is a configuration change + formula
- Single codebase, single maintenance burden
- Lowest 3-year maintenance cost

**Cons:**
- Highest initial effort (8–12 weeks)
- Risk of NDVI regression during refactoring
- Large branch that diverges from main for weeks
- Requires dashboard migration for `ndvi_*` → `spectral_index_*`

**Effort:** 8–12 weeks
**Migration risk:** High (touches production NDVI code)
**3-year maintenance:** Low

### Option C: Hybrid Approach (Recommended)

**Description:** Generalize the shared infrastructure (models, services, views, tasks, metrics) with `index_type` discriminator. Keep per-index logic (quality thresholds, fusion rules, raster colormap) in separate index-scoped modules. Ship NDWI on the generalized platform.

**Pros:**
- All code reuse benefits (STAC client, cache, locks, circuit breaker)
- Backward-compatible rollout — legacy NDVI endpoints never change
- Each phase is a deployable increment (no long-lived branch)
- Adding the next index (EVI, NBR) is a small module + config
- Lower migration risk than full generalization

**Cons:**
- Medium effort (5–6 weeks) — slower than Option A
- Requires one model migration (add `index_type` column)
- Per-index quality/fusion modules are still separate files (but small)

**Effort:** 5–6 weeks
**Migration risk:** Medium (one migration, validated on staging)
**3-year maintenance:** Low

## Trade-off Matrix

| Criterion | Option A | Option B | Option C |
|-----------|----------|----------|----------|
| Initial effort | 4–6 weeks | 8–12 weeks | 5–6 weeks |
| NDVI regression risk | None | High | Low |
| Scalability (5+ indices) | Poor | Excellent | Very good |
| 3-year maint. cost | $180k | $60k | $80k |
| Deployment safety | Independent | Big bang | Incremental |
| DB migration complexity | None (new tables) | High (rename tables) | Medium (add column) |
| Dashboard migration | None | Required | Separate panels |

## Final Recommendation

**Option C: Hybrid Approach.**

**Rationale:**
1. NDVI architecture is mature and stable (3+ months production). It does not need a full rewrite.
2. The STAC client, circuit breaker, lock manager, cache layer, and retry policy are already fully generic — zero changes needed.
3. Model generalization (adding `index_type` column) is a reversible, well-understood migration pattern.
4. Per-index quality/fusion thresholds are genuinely index-specific — sharing them would require complex parameter tables that obscure the logic.
5. The 5–6 week delivery timeline fits within the growing season for farm operations.
6. The hybrid approach proves the platform concept for future indices (NDMI → Phase 8+).

## Consequences

| Positive | Negative |
|----------|----------|
| Engine parameterization simplifies adding EVI (NIR + Red Edge, B08+B05) | One migration touches production `NdviObservation` table |
| View parameterization means NDVI endpoints are guaranteed unchanged | Per-index fusion modules are boilerplate but necessary |
| Metric pattern (`ndwi_*`) is proven by NDVI `ndvi_*` | Metric unification (`spectral_index_*`) deferred to future |
| Task reuse means Celery Beat configuration is a copy with different queue names | MODIS unsupported for NDWI (acceptable gap) |

## Decision Implementation

See `01-architecture.md` for component diagram and data flow.
See `02-data-model.md` for migration plan.
See `07-phased-delivery-plan.md` for 7-phase implementation schedule.
