# TDD: Phase 4 — Operationalization

---

## 1. Scope

### In-scope
- `run_ndwi_job` Celery task — dispatches to NDWI engine based on job type
- `enqueue_daily_ndwi_refresh` — iterates active farms, enqueues `NDWI_REFRESH_LATEST` jobs
- `enqueue_weekly_ndwi_gap_fill` — 120-day gap fill for all farms
- `spectral_index_*` Prometheus metrics with `index` label
- Celery Beat schedule additions
- Grafana dashboard panels for NDWI
- `test_no_regression.py` — NDVI/NDWI isolation assertions

### Out-of-scope
- Separate Celery queues for NDWI (deferred — shares NDVI queues)
- Grafana dashboard JSON export (generated outside TDD)
- Alert rule configuration (deployed via Terraform/Grafana API)

### Dependencies
- Phase 3 complete (quality, fusion, raster working)
- Phase 1–2 complete (models, engines, API, serializers)

---

## 2. Requirements

### Functional
- `run_ndwi_job(job_id)` executes engine call, upserts observation with `index_type="NDWI"`
- Task handles all job types: `NDWI_REFRESH_LATEST`, `NDWI_GAP_FILL`, `NDWI_BACKFILL`, `NDWI_RASTER_PNG`
- Daily refresh enqueues one `NDWI_REFRESH_LATEST` job per active farm
- Weekly gap fill enqueues `NDWI_GAP_FILL` for farms with missing dates in last 120 days
- All `spectral_index_*` metrics fire on NDWI API calls and task execution
- Existing `ndvi_*` metrics continue to fire on NDVI calls

### Non-functional
- `run_ndwi_job` must not interfere with concurrently running `run_ndvi_job` (lock isolation via `acquire_lock`)
- NDWI refresh schedule staggered 6h after NDVI (NDVI = 00:00, NDWI = 06:00 UTC)
- NDWI tasks share NDVI queues — no queue isolation yet

### Backward Compatibility
- `run_ndvi_job` continues to work for NDVI jobs
- `ndvi_*` metrics continue to exist (not removed, deprecated in future)
- NDVI Celery Beat schedule entries unchanged

---

## 3. Architecture Assumptions

| # | Assumption | Source | Risky? |
|---|-----------|--------|--------|
| A1 | NDWI tasks share NDVI Celery queues. No separate queue configuration. | 11-implementation-readiness | Low — worker capacity sufficient |
| A2 | NDWI daily refresh at 06:00 UTC avoids STAC rate limit contention with NDVI at 00:00. | 11-implementation-readiness | Medium — depends on farm count |
| A3 | `spectral_index_*` metrics with `index` label do not break existing NDVI dashboards filtering on `ndvi_*`. | 04-metrics-observability | Low — dashboards filter by metric name |
| A4 | Retry policy for NDWI tasks matches NDVI (max_retries=3, delay=60). | 11-implementation-readiness | Low |
| A5 | Lock acquisition for NDWI tasks uses same Redis lock namespace as NDVI (prefix distinguishes by job hash). | 11-implementation-readiness | Low — lock key includes job ID, not metric name |

---

## 4. Open Questions

| # | Question | Owner | Resolved by |
|---|----------|-------|-------------|
| Q1 | Should NDWI tasks use the same lock namespace as NDVI? (Risk: NDWI lock could block NDVI job with same request_hash if engines + dates collide.) | Engineering | Decision: same lock namespace. Collision risk is zero because `request_hash` includes engine name (e.g., `ndwi_stac` vs `stac`). |
| Q2 | Should we run NDWI daily refresh on all farms, or start with an opt-in subset? | Farm Ops | Start with all active farms. Monitor STAC rate limits. Opt-in defer if rate limits hit. |
| Q3 | What is the Grafana dashboard deployment process? JSON export or Terraform? | DevOps | Deploy via repo Grafana JSON export. Terraform integration is future. |

---

## 5. Risk Analysis

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| NDWI + NDVI jobs compete for worker capacity | Medium | Medium — delayed refreshes | Monitor queue depth. Stagger schedules (6h offset). Scale workers if needed. |
| STAC API rate limit from concurrent NDWI + NDVI | Medium | Medium — retry storms | Shared rate limiter in StacClient. Monitor `StacUpstreamError` rate. Consider dedicated STAC credentials. |
| `spectral_index_*` metrics cardinality explosion | Low | Medium — Prometheus storage | Label values limited to 4 index types (NDVI, NDWI, EVI, NBR planned). Acceptable cardinality. |
| `run_ndwi_job` deadlock with `run_ndvi_job` on same farm | Low | Low — lock keys include request hash, not farm ID alone | Verified in lock implementation. |
| Celery Beat schedule drift (NDWI refresh misses window) | Low | Low — missed window caught by next cycle | Gap fill catches missed days. |

---

## 6. Test Matrix

### Unit tests — Tasks

| Test | Count | File |
|------|-------|------|
| `run_ndwi_job(REFRESH_LATEST)` calls engine get_latest and upserts | 1 | `test_tasks_ndwi.py` |
| `run_ndwi_job(GAP_FILL)` calls engine get_timeseries and upserts | 1 | `test_tasks_ndwi.py` |
| `run_ndwi_job(BACKFILL)` calls engine get_timeseries and upserts | 1 | `test_tasks_ndwi.py` |
| `run_ndwi_job(RASTER_PNG)` calls raster render and saves artifact | 1 | `test_tasks_ndwi.py` |
| `run_ndwi_job` with already-successful job returns early | 1 | `test_tasks_ndwi.py` |
| `run_ndwi_job` with failed engine marks job FAILED + logs error | 1 | `test_tasks_ndwi.py` |
| `run_ndwi_job` retries on retryable errors (max_retries=3) | 1 | `test_tasks_ndwi.py` |
| `run_ndwi_job` does not retry on non-retryable errors | 1 | `test_tasks_ndwi.py` |
| `enqueue_daily_ndwi_refresh` iterates active farms | 1 | `test_tasks_ndwi.py` |
| `enqueue_weekly_ndwi_gap_fill` enqueues gap fill per farm | 1 | `test_tasks_ndwi.py` |
| Lock acquisition prevents duplicate execution | 1 | `test_tasks_ndwi.py` |
| Lock release on task completion | 1 | `test_tasks_ndwi.py` |

### Unit tests — Metrics

| Test | Count | File |
|------|-------|------|
| `spectral_jobs_total{index="NDWI"}` increments on NDWI task | 1 | `test_tasks_ndwi.py` |
| `spectral_jobs_total{index="NDVI"}` increments on NDVI task (unchanged) | 1 | `test_tasks_ndwi.py` |
| `spectral_upstream_requests_total{index="NDWI"}` increments on NDWI engine call | 1 | `test_tasks_ndwi.py` |
| Existing `ndvi_*` metrics continue to fire on NDVI operations | 3 | `test_no_regression.py` |

### Regression tests

| Test | Count | File |
|------|-------|------|
| `run_ndvi_job` unchanged (same params, same behavior) | 1 | `test_no_regression.py` |
| `enqueue_daily_refresh` unchanged (still triggers NDVI refresh) | 1 | `test_no_regression.py` |
| `spectral_jobs_total{index="NDVI"}` metric unchanged | 1 | `test_no_regression.py` |
| NDVI Celery Beat schedule unchanged | 1 | `test_no_regression.py` |
| NDWI refresh does not create NDVI jobs | 1 | `test_no_regression.py` |
| NDWI task does not modify NDVI observations | 1 | `test_no_regression.py` |
| NDVI lock acquisition not affected by NDWI locks | 1 | `test_no_regression.py` |

### Integration tests

| Test | Count | File |
|------|-------|------|
| End-to-end: refresh → task runs → observation written | 1 | `test_tasks_ndwi.py` |
| End-to-end: daily refresh schedule enqueues correct jobs | 1 | `test_tasks_ndwi.py` |

---

## 7. Acceptance Criteria

| # | Criterion | Verification |
|---|-----------|-------------|
| AC1 | `run_ndwi_job` writes `NdviObservation(index_type="NDWI")` | Unit test |
| AC2 | Daily refresh enqueues `NDWI_REFRESH_LATEST` for each active farm | Unit test |
| AC3 | Weekly gap fill enqueues `NDWI_GAP_FILL` for missing dates | Unit test |
| AC4 | All `spectral_index_*` metrics fire (Counter increments, Histogram observes) | Integration test |
| AC5 | All `ndvi_*` metrics unchanged | Regression tests |
| AC6 | NDWI task retries on transient errors | Unit test |
| AC7 | NDWI task does not retry on auth errors | Unit test |
| AC8 | NDWI and NDVI tasks run concurrently without deadlock | Integration test |
| AC9 | NDWI Celery Beat schedule is active at 06:00 UTC | Config inspection |

---

## 8. Rollback Criteria

### Conditions requiring rollback
- NDWI task crashes with unhandled exception on first farm
- NDWI + NDVI queue contention causes NDVI task latency > 2x baseline
- `spectral_index_*` metrics break existing dashboards (missing `ndvi_*` metrics)

### Rollback procedure
```bash
# Remove Celery Beat entries from settings.py
# Remove task functions
# Revert metrics.py changes
git revert <phase-4-commit>
# Deploy, restart Celery workers
```

### Verification after rollback
- [ ] NDWI tasks no longer execute
- [ ] `ndvi_*` metrics continue to fire (if reverted)
- [ ] `spectral_index_*` metrics no longer appear in Prometheus
- [ ] NDVI Celery Beat schedule restored to pre-deployment state

---

## A. Semantic Field Review

**Question:** Task function names (`run_ndwi_job`, `enqueue_daily_ndwi_refresh`) encode `ndwi` in their names. Acceptable?

**Decision: Keep as-is.**

Task names include the index name for clarity in Celery logs and monitoring. Future index (`NDMI`) gets `run_ndmi_job`. This is explicit and debuggable — no semantic ambiguity.

---

## B. Migration Validation

N/A for Phase 4. No DB changes.

---

## C. Future Index Extensibility

**Question:** What changes are required to add NDMI after NDWI for Phase 4?

| NDMI requires | Add |
|--------------|-----|
| `run_ndmi_job` task | New `@shared_task` (copy `run_ndwi_job` pattern, swap engine factory prefix) |
| `enqueue_daily_ndmi_refresh` | New Celery Beat schedule entry |
| `enqueue_weekly_ndmi_gap_fill` | New Celery Beat schedule entry |
| Metrics: `spectral_index_*` already has `index="NDMI"` label | **None needed** — label works automatically |

**Remaining coupling:** Tasks are copy-paste per index. Could be generalized to `run_index_job(index_type, job_id)` in a future refactoring, but per-index tasks are explicit and easier to debug in Celery logs.

**Metrics are zero-coupling.** The `spectral_index_*` metric family with `index` label means adding NDMI requires zero new metric definitions.

---

## D. Metrics Strategy Validation

### Why `spectral_index_*` metrics were chosen
- 37 duplicate metric definitions eliminated (NDVI + NDWI + future indices)
- Single Grafana panel works for all indices by filtering on `index` label
- Less Prometheus cardinality than separate metric families
- Easier to add alert rules that cover all indices

### Dashboard compatibility strategy
- Existing NDVI dashboards filter on metric name `ndvi_*` — continue to work
- New NDWI dashboards filter on `spectral_index_*{index="NDWI"}` and also on `ndvi_*{index="NDVI"}` for comparison
- Transition path: `ndvi_*` metrics gain `index` label; `spectral_index_*` is the canonical long-term name

### Alert migration strategy
- Existing NDVI alerts on `ndvi_*` metrics unchanged
- New NDWI alerts on `spectral_index_*{index="NDWI"}`
- Future: migrate NDVI alerts to `spectral_index_*{index="NDVI"}` at own pace

### Verification tests
- [ ] `spectral_jobs_total{index="NDVI"}` exists and increments
- [ ] `spectral_jobs_total{index="NDWI"}` exists and increments
- [ ] `spectral_jobs_total{index="NDVI"}` exists and increments
- [ ] Dashboard query `rate(spectral_jobs_total{index="NDWI"}[5m])` returns non-zero
- [ ] Dashboard query `rate(spectral_jobs_total{index="NDVI"}[5m])` returns non-zero

---

## E. API Compatibility Validation

### Existing NDVI endpoint behavior
- `POST /api/v1/farms/<id>/ndvi/refresh/` enqueues NDVI refresh job
- Refresh cooldown is per-user per-farm (900s)

### Expected NDWI behavior
- `POST /api/v1/farms/<id>/ndwi/refresh/` enqueues NDWI refresh job
- Refresh cooldown is independent of NDVI cooldown

### Regression coverage required before approval
- [ ] NDVI refresh still enqueues NDVI job (not NDWI)
- [ ] NDVI cooldown not affected by NDWI refresh requests
- [ ] NDWI refresh cooldown not affected by NDVI refresh requests

---

## F. Data Integrity Validation

| Test | What it validates |
|------|-------------------|
| NDWI task writes `index_type="NDWI"` | Row isolation |
| NDWI task does not modify NDVI rows | Write isolation |
| NDWI + NDVI concurrent refresh on same farm → both succeed | Lock isolation |
| `spectral_index_*` metric for NDWI does not overwrite NDVI metric value | Metric isolation |
| Celery Beat schedule register NDWI tasks under distinct names | Schedule isolation |
| NDWI daily refresh does not enqueue NDVI jobs | Job isolation |
