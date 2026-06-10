# TDD: Phase 2 — API Layer

---

## 1. Scope

### In-scope
- `NdwiTimeseriesView` — GET `/api/v1/farms/<id>/ndwi/timeseries/`
- `NdwiLatestView` — GET `/api/v1/farms/<id>/ndwi/latest/`
- `NdwiRefreshView` — POST `/api/v1/farms/<id>/ndwi/refresh/`
- `NdwiObservationSerializer` with default `engine="ndwi_stac"`
- URL patterns under `/api/v1/farms/<id>/ndwi/`
- Cache layer with `ndwi:cache:` prefix
- Cooldown mechanism with `ndwi:refresh:` prefix
- `@extend_schema` decorators for Swagger
- `success_response` envelope

### Out-of-scope
- NDWI raster (`raster.png`, `raster/queue`) — Phase 3
- NDWI quality/fusion — Phase 3
- NDWI tasks — Phase 4

### Dependencies
- Phase 1 complete (models have `index_type`, engines work, `get_engine(index_type=)` ready)

---

## 2. Requirements

### Functional
- `GET /ndwi/timeseries/` returns list of NDWI observations filtered to `index_type="NDWI"`
- `GET /ndwi/latest/` returns latest NDWI observation (or null with 200)
- `POST /ndwi/refresh/` enqueues `NDWI_REFRESH_LATEST` job (delegates to Phase 4 task stub)
- All endpoints accept standard query params: `engine`, `start`, `end`, `step_days`, `max_cloud`, `lookback_days`, `representation`
- Cache hit returns 200 with cached payload, no engine call
- Cache miss calls engine, stores result, returns 200
- Cooldown returns 429 if triggered within 900s

### Non-functional
- Response time ≤ 500ms (cache hit), ≤ 10s (cache miss + engine call)
- Swagger docs show request params + response envelope

### Backward Compatibility
- NDVI endpoints completely unchanged
- NDVI cache keys never read by NDWI views

---

## 3. Architecture Assumptions

| # | Assumption | Source | Risky? |
|---|-----------|--------|--------|
| A1 | NDWI views are standalone classes (no base class shared with NDVI). | 10-design-review | No |
| A2 | `NdviObservationSerializer` can be reused for NDWI with different `engine` default. | 11-implementation-readiness | Low — field names are index-agnostic |
| A3 | Cache key format matches NDVI pattern with `ndwi:` prefix. | 11-implementation-readiness | No |
| A4 | `get_engine(index_type="NDWI", engine_name=...)` returns correct engine. | 11-implementation-readiness | No (tested in P1) |
| A5 | `?representation=v2` returns V2 fields. Phase 4 adds quality processing; API endpoint must handle missing V2 gracefully. | 03-api-design | Medium — returns empty V2 fields before Phase 4 |

---

## 4. Open Questions

| # | Question | Owner | Resolved by |
|---|----------|-------|-------------|
| Q1 | Should `NdwiObservationSerializer` be a new class or a parameterized instance of `NdviObservationSerializer`? | Engineering | New class wrapping same field set with different `engine` default. Avoids coupling to NDVI serializer internals. |
| Q2 | What should the Swagger `NdwiEnvelope` serializer look like? | Engineering | `inline_serializer` with `NdwiObservationSerializer()` as `data` field. |
| Q3 | How does V2 injection behave when no V2 data exists? | Engineering | Return empty `v2` key or omit it. Decision: omit (same as NDVI). |

---

## 5. Risk Analysis

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Cache key collision with NDVI | Low | Medium | Prefix `ndwi:cache:` is distinct; unit test that NDVI keys don't match NDWI pattern |
| Cooldown key collision with NDVI | Low | Medium | Prefix `ndwi:refresh:` is distinct |
| V2 injection missing before Phase 4 | Certain | Low | V2 fields return empty/None; API doc says "available after Phase 4" |
| NDWI view mistakenly filters NDVI data | Low | High | Every view method explicitly adds `.filter(index_type="NDWI")` |
| Engine name mismatch (`ndwi_stac` vs `stac`) | Low | Medium | Default engine in serializer is `ndwi_stac`; `get_engine` validates prefix |

---

## 6. Test Matrix

### Unit tests

| Test | Count | File |
|------|-------|------|
| NdwiObservationSerializer default engine is `ndwi_stac` | 1 | `test_views_ndwi.py` |
| NdwiObservationSerializer includes all expected fields | 1 | `test_views_ndwi.py` |
| NdwiObservationSerializer field types match NdviObservationSerializer | 1 | `test_views_ndwi.py` |
| Cache key format matches `ndwi:cache:v2:ts:...` pattern | 2 | `test_views_ndwi.py` |

### Integration tests

| Test | Count | File |
|------|-------|------|
| GET timeseries returns 200 with correct envelope | 1 | `test_views_ndwi.py` |
| GET timeseries filters to `index_type="NDWI"` | 1 | `test_views_ndwi.py` |
| GET timeseries with `?engine=ndwi_stac` returns correct engine | 1 | `test_views_ndwi.py` |
| GET latest returns 200 with latest NDWI | 1 | `test_views_ndwi.py` |
| GET latest returns null when no NDWI data | 1 | `test_views_ndwi.py` |
| POST refresh returns 200 with job_id | 1 | `test_views_ndwi.py` |
| POST refresh cooldown returns 429 within 900s | 1 | `test_views_ndwi.py` |
| Cache hit: second request returns cached data without engine call | 1 | `test_views_ndwi.py` |
| Cache miss: first request calls engine and caches | 1 | `test_views_ndwi.py` |

### Auth tests

| Test | Count | File |
|------|-------|------|
| Unauthenticated request to timeseries → 401 | 1 | `test_views_ndwi.py` |
| Unauthenticated request to latest → 401 | 1 | `test_views_ndwi.py` |
| Unauthenticated request to refresh → 401 | 1 | `test_views_ndwi.py` |

### Negative tests

| Test | Count | File |
|------|-------|------|
| Invalid engine name → 400 | 1 | `test_views_ndwi.py` |
| Invalid date format → 400 | 1 | `test_views_ndwi.py` |
| Missing `engine` param → uses default `ndwi_stac` | 1 | `test_views_ndwi.py` |
| Unsupported `representation` value → 400 | 1 | `test_views_ndwi.py` |

### V2 representation tests

| Test | Count | File |
|------|-------|------|
| `?representation=v2` returns V2 envelope with empty V2 fields before Phase 4 | 1 | `test_views_ndwi.py` |

### Regression tests

| Test | Count | File |
|------|-------|------|
| NDVI timeseries endpoint returns same results (no NDWI leakage) | 1 | `test_no_regression.py` |
| NDVI latest endpoint returns same results | 1 | `test_no_regression.py` |
| NDVI refresh endpoint works (no cooldown key collision) | 1 | `test_no_regression.py` |
| NDVI cache keys not affected by NDWI cache writes | 1 | `test_no_regression.py` |

---

## 7. Acceptance Criteria

| # | Criterion | Verification |
|---|-----------|-------------|
| AC1 | All 5 endpoints respond with correct HTTP status codes | Integration tests |
| AC2 | Swagger shows all endpoints with request params and response envelope | `python manage.py spectacular --file /tmp/schema.yml` |
| AC3 | Cache works (second request < 50ms, no engine call) | Integration test |
| AC4 | Cooldown works (second refresh within 900s → 429) | Integration test |
| AC5 | All NDVI endpoints unchanged | Regression tests |
| AC6 | `success_response` envelope is `{"success": 0, "message": "...", "data": ...}` | Integration test |
| AC7 | V2 representation returns valid envelope (empty V2 fields before Phase 4) | Integration test |

---

## 8. Rollback Criteria

### Conditions requiring rollback
- Any NDVI endpoint returns different results
- NDWI endpoints cause 500 errors on valid requests
- Cache key collision detected (NDVI data served for NDWI request)

### Rollback procedure
```bash
# Remove NDWI URL patterns
# git revert the commit adding ndwi/ views + serializers + URL patterns
git revert <commit>
```

### Verification after rollback
- [ ] `/api/v1/farms/<id>/ndwi/*` returns 404
- [ ] All NDVI endpoints return pre-deployment results
- [ ] No orphaned `ndwi:cache:` keys in Redis

---

## A. Semantic Field Review

**Question:** The response serializer includes fields like `mean`, `min`, `max`, `cloud_fraction`, `valid_pixel_fraction`. Are these semantics acceptable for NDWI?

**Decision: Keep as-is.**

All serialized fields are generic: they describe statistical properties of the observation, not the index value interpretation. `mean` is the mean NDWI value for that pixel group. The field name doesn't encode "NDVI" — it encodes "mean of observed values."

**No change needed.**

---

## B. Migration Validation

N/A for Phase 2. No DB changes.

---

## C. Future Index Extensibility

**Question:** What changes are required to add NDMI after NDWI for Phase 2?

| NDMI requires | Add |
|--------------|-----|
| Serializer with `engine` default `ndmi_stac` | Copy `NdwiObservationSerializer` → `NdmiObservationSerializer` |
| Views with `index_type="NDMI"` | Copy NDWI view pattern with `index_type="NDMI"` |
| URLs `/api/v1/farms/<id>/ndmi/` | Add URL patterns |
| Cache prefix `ndmi:cache:` | Define `CACHE_PREFIX = "ndmi:cache:"` |

**Remaining coupling:** Views are currently copy-pasted per index. A future refactoring could introduce a parameterized `IndexView` base class where `index_type`, `engine_prefix`, and `cache_prefix` are class attributes. This would reduce NDMI Phase 2 effort from 3 days to 1 day.

---

## D. Metrics Strategy Validation

N/A for Phase 2 — metrics are Phase 4.

---

## E. API Compatibility Validation

### Existing NDVI endpoint behavior (baseline)
- All endpoints return data filtered by engine name
- No `index_type` filtering on NDVI views (historically all data is NDVI)

### Expected NDWI behavior
- NDWI views filter on `index_type="NDWI"`
- NDWI views use engine prefix `ndwi_*` (e.g., `ndwi_stac`, `ndwi_sentinelhub`)
- NDWI views use cache prefix `ndwi:cache:`
- NDWI views use cooldown prefix `ndwi:refresh:`

### Regression coverage required before approval
- Run all NDVI endpoint integration tests
- Compare response bodies byte-for-byte with pre-Phase-2 baseline

---

## F. Data Integrity Validation

| Test | What it validates |
|------|-------------------|
| NDWI view returns only `index_type="NDWI"` rows | `index_type` isolation |
| NDWI view with explicit `engine="stac"` returns 400 or filters correctly | Engine prefix validation |
| NDWI + NDVI both present; NDWI view returns only NDWI | Row-level isolation |
| Cache isolation: NDWI cache miss does not populate NDVI cache | Key prefix isolation |
| Cooldown isolation: NDWI cooldown does not affect NDVI refresh | Key prefix isolation |
| Serialization: NDWI values stored in `mean` field are returned as-is | Field isolation |
