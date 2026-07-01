# NDWI Risks & Mitigations

**Document:** 06-risks.md
**Stage:** Design (pre-implementation)
**Status:** Draft for review

---

## Risk Matrix

| ID | Risk | Category | Likelihood | Impact | Overall |
|----|------|----------|-----------|--------|---------|
| R1 | Model migration corrupts existing NDVI data | Migration | Low | Critical | High |
| R2 | NDVI view regression from model rename | Technical | Medium | High | High |
| R3 | NDWI quality thresholds produce >20% null rate | Operational | Medium | Medium | Medium |
| R4 | NDWI formula band math wrong (Green vs Red flip error) | Implementation | Low | High | Medium |
| R5 | NDWI cache poisoning from shared Redis keys | Technical | Low | Medium | Low |
| R6 | Engine cross-contamination (ndwi engine selected for NDVI job) | Implementation | Low | Critical | Medium |
| R7 | NDWI and NDVI Celery tasks compete for worker capacity | Operational | Medium | Medium | Medium |
| R8 | Dashboards need update for `ndwi_*` metrics | Operational | High | Low | Low |
| R9 | STAC API rate limiting when NDWI + NDVI run concurrently | Operational | Medium | Medium | Medium |
| R10 | Farm ops misinterprets NDWI negative values as "error" | Operational | High | Low | Low |
| R11 | MODIS skipped for NDWI — gap in lower-resolution coverage | Technical | Certain | Low | Medium |
| R12 | Rollback of NDWI data after production writes | Migration | Low | Medium | Low |

## Detailed Risk Analysis

### R1: Model migration corrupts existing NDVI data

**Description:** Adding `index_type` column to `SpectralObservation` and running constraints/index updates could cause table locking, constraint violations, or data corruption.

**Likelihood:** Low — the migration is additive (new column with default, new indexes).
**Impact:** Critical — NDVI is in production with live farm ops usage.

**Mitigation:**
- Rehearse migration on staging with a copy of production data.
- Run migration in a transaction; rollback if it takes > 5 seconds.
- Add `index_type` with `default="NDVI"` — existing rows get the correct value immediately.
- Run constraint/index creation `CONCURRENTLY` (PostgreSQL 12+).
- Monitor table locks during migration window.

### R2: NDVI view regression from model rename

**Description:** Renaming `NdviObservation` → `SpectralObservation` could break NDVI views that import the old name.

**Likelihood:** Medium — affects all NDVI views, serializers, services.
**Impact:** High — NDVI API goes down if a single import breaks.

**Mitigation:**
- Add `NdviObservation = SpectralObservation` alias in `ndvi/models.py`.
- Keep old imports working during transition period (1 release cycle).
- Full test suite must pass before deployment.
- Run NDVI integration tests in staging with the new model before production.

### R3: NDWI quality thresholds produce >20% null rate

**Description:** Initial NDWI thresholds (confidence, outlier detection) may be too aggressive, producing high V2 null rates and unusable data.

**Likelihood:** Medium — thresholds are guess-based until production data is available.
**Impact:** Medium — farm ops gets fewer data points; NDWI is less useful.

**Mitigation:**
- Set initial thresholds conservatively (low null rate, potentially lower quality).
- Monitor `ndwi_v2_null_output_total` by null_reason.
- Plan tuning sprint 2 weeks after launch.
- Provide raw V1 data as fallback when V2 is null.

### R4: NDWI formula band math wrong

**Description:** Using Green (B03) instead of Red (B04) in formula. Easy to accidentally use `(GREEN - NIR) / (GREEN + NIR)` correctly or incorrectly flip constants.

**Likelihood:** Low — the formula is simple and well-documented.
**Impact:** High — produces wrong values that could be used for operational decisions.

**Mitigation:**
- Validate NDWI output against known reference:
  - Open water (lake pixel) → NDWI ≈ 0.3–0.7
  - Vegetation → NDWI < 0
  - Bare soil → NDWI near 0
- Cross-reference with SentinelHub's native NDWI evalscript.
- Unit test engine output against a synthetic array where expected NDWI is pre-computed.

### R5: NDWI cache poisoning from shared Redis keys

**Description:** NDWI cache keys use `ndwi:cache:` prefix — distinct from `ndvi:cache:`. No risk if correctly implemented.

**Likelihood:** Low.
**Impact:** Medium — wrong data served if prefixes collide.

**Mitigation:**
- Code review to ensure `NDWI_CACHE_PREFIX = "ndwi:cache:"` is used throughout.
- Cache key format documented in code.
- Unit test that NDWI keys never match NDVI key pattern.

### R6: Engine cross-contamination

**Description:** A bug in `get_engine()` passes NDWI engine to an NDVI job or vice versa.

**Likelihood:** Low — engine names are distinct (`ndwi_stac` vs. `stac`).
**Impact:** Critical — wrong formula applied to band data.

**Mitigation:**
- `get_engine()` requires explicit `index_type` parameter.
- Engine names are prefixed (`ndwi_*` vs. no prefix for NDVI).
- Validate engine name matches `index_type` before job dispatch.
- Unit test that requesting `index_type="NDWI"` returns `ndwi_*` engine.

### R7: NDWI and NDVI Celery tasks compete for worker capacity

**Description:** Adding daily NDWI refresh tasks doubles the Celery workload.

**Likelihood:** Medium — depends on farm count.
**Impact:** Medium — increased latency for both NDVI and NDWI tasks.

**Mitigation:**
- NDWI tasks go to a separate `ndwi_*` Celery queue.
- Worker pools can be scaled independently.
- Monitor `ndwi_task_runtime_seconds` and `spectral_task_runtime_seconds{index="NDVI"}` during peak.
- Stagger NDWI and NDVI refresh schedules (NDVI at 00:00 UTC, NDWI at 06:00 UTC).

### R8: Dashboards need update for `ndwi_*` metrics

**Description:** Operators need new Grafana panels for NDWI.

**Likelihood:** High — always required for new metrics.
**Impact:** Low — non-breaking; panels can be added incrementally.

**Mitigation:**
- Deploy NDWI metrics first (visible in explorer).
- Add dashboard panels in same release (or next).
- Provide dashboard JSON export in repo.

### R9: STAC API rate limiting

**Description:** STAC API seen by both NDVI and NDWI requests could hit rate limits.

**Likelihood:** Medium — depends on farm count and refresh frequency.
**Impact:** Medium — delayed data but no data loss (retry mechanism).

**Mitigation:**
- NDWI uses the same `StacClient` rate limiter (jitter, interval).
- Circuit breaker is shared per endpoint (not per index).
- Monitor `StacUpstreamError` rate for rate-limit responses.
- Consider dedicated STAC API credentials for NDWI if needed.

### R10: Farm ops misinterprets NDWI negative values

**Description:** NDWI values are negative for vegetation. Operators accustomed to NDVI (positive = healthy) may interpret negative NDWI as "bad" or "error."

**Likelihood:** High — cognitive mismatch.
**Impact:** Low — no system impact, but user confusion.

**Mitigation:**
- Document NDWI interpretation in API response `message` field.
- Provide `quality_flags["ndwi_interpretation"]` classification.
- User documentation / training session.

### R11 (Resolved): MODIS NDWI via MOD09GA

**Description:** MODIS now supports NDWI via the MOD09GA surface reflectance product (band 4 Green + band 2 NIR) instead of the MOD13Q1 pre-computed NDVI product. Resolves the previous coarse-resolution fallback gap.

**Resolution:** Engine uses `modis-09ga-061` STAC collection with `sur_refl_b04` (Green, 500m) and `sur_refl_b02` (NIR, 500m) bands, computed as `(Green - NIR) / (Green + NIR)`.

### R12: Rollback of NDWI data after production writes

**Description:** If NDWI needs to be rolled back after production launch, NDWI data in `SpectralObservation` must be cleaned without touching NDVI data.

**Likelihood:** Low.
**Impact:** Medium — NDWI data loss expected; NDVI must be preserved.

**Mitigation:**
- Rollback script: `DELETE FROM ndvi_spectralobservation WHERE index_type = 'NDWI';`
- Run as separate transaction; verify NDVI row count before commit.
- Backup before migration.

## Risk Response Summary

| Risk ID | Response | Owner | Deadline |
|---------|----------|-------|----------|
| R1 | Rehearse migration, CONCURRENTLY, table lock monitor | Infrastructure | Before launch |
| R2 | Alias + integration tests + staging validation | Engineering | Before each release |
| R3 | Conservative thresholds, monitor, 2-week tuning sprint | Data Science | 2 weeks post-launch |
| R4 | Reference validation + unit tests | Engineering | Before launch |
| R5 | Code review + cache key unit tests | Engineering | Before launch |
| R6 | `get_engine(index_type=)` API + validation | Engineering | Before launch |
| R7 | Separate Celery queue, staggered schedule | Infrastructure | Before launch |
| R8 | Inline with launch or next release | DevOps | At launch |
| R9 | Shared rate limiter, monitor, separate credentials | Engineering | Before launch |
| R10 | Documentation + classification flag | Product | Before launch |
| R11 | Implemented via MOD09GA (band 4 + band 2) | Engineering | Resolved |
| R12 | Rollback script in repo, backup | Infrastructure | Before launch |
