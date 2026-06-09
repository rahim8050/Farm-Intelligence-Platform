# NDWI Test Strategy

**Document:** 08-test-strategy.md
**Stage:** Design (pre-implementation)
**Status:** Draft for review

---

## Coverage Expectations

| Layer | Target coverage | Notes |
|-------|----------------|-------|
| Engines | 100% | Same as NDVI engines |
| Services | 96% | Integration with DB, cache mocked |
| Models | 100% | Constraints, indexes, QuerySet methods |
| Views | 90% | Request/response, auth, permissions, cache |
| Quality | 100% | Formula, thresholds, edge cases |
| Fusion | 100% | Decision tree branches, conflict, fallback |
| Raster | 100% | Colormap, normalization, PNG bytes |
| Tasks | 80% | Job dispatch, Celery integration |
| Metrics | 80% | Metrics fire, no exception |

**Overall target:** ≥ 96% for new NDWI code (matching AGENTS.md requirement).

## Test Types

### Unit Tests

| Group | What | How many | Location |
|-------|------|----------|----------|
| Engine formula | NDWI formula produces correct values for known inputs (open water=0.5, vegetation=-0.3, bare soil=0.0) | 6 | `ndwi/tests/test_engines.py` |
| Engine SCL mask | SCL masking works with NDWI data (same masking as NDVI, different interpretation) | 4 | `ndwi/tests/test_engines.py` |
| Engine factory | All ndwi engine factories return valid engines | 4 | `ndwi/tests/test_services.py` |
| Quality confidence | Confidence formula with all weights | 5 | `ndwi/tests/test_quality.py` |
| Quality outlier | Outlier detection with NDWI-specific thresholds | 4 | `ndwi/tests/test_quality.py` |
| Quality null conditions | All null conditions (low_valid_pixel, low_confidence, missing_ndwi, etc.) | 6 | `ndwi/tests/test_quality.py` |
| Fusion decision tree | All branches (primary, fallback, sort, null) | 5 | `ndwi/tests/test_fusion.py` |
| Fusion conflict | Conflict detection, disagreement | 3 | `ndwi/tests/test_fusion.py` |
| Raster colormap | Blue colormap control points, normalization | 3 | `ndwi/tests/test_raster.py` |
| Model constraints | Unique constraint with index_type, QuerySet filtering | 5 | `ndwi/tests/test_models.py` |

### Integration Tests

| Group | What | How many | Location |
|-------|------|----------|----------|
| View timeseries | Full request → engine mock → response | 3 | `ndwi/tests/test_views.py` |
| View latest | Full request → engine mock → response | 3 | `ndwi/tests/test_views.py` |
| View refresh | POST request → job enqueue | 2 | `ndwi/tests/test_views.py` |
| View raster PNG | GET raster.png → PNG bytes | 2 | `ndwi/tests/test_views.py` |
| View auth | Unauthenticated request → 401 | 2 | `ndwi/tests/test_views.py` |
| View cache | Cache hit → no engine call, cache miss → engine call | 2 | `ndwi/tests/test_views.py` |
| View V2 representation | `?representation=v2` returns V2 fields | 2 | `ndwi/tests/test_views.py` |
| Task job dispatch | `run_ndwi_job` with mock engine | 3 | `ndwi/tests/test_tasks.py` |

### Migration Tests

| What | How | Location |
|------|-----|----------|
| `0003` forward | Apply migration, verify `index_type` column exists with correct default | `ndwi/tests/test_migrations.py` |
| `0003` backward | Rollback migration, verify `index_type` column removed | `ndwi/tests/test_migrations.py` |
| Data preservation | Existing rows have `index_type="NDVI"` after migration | `ndwi/tests/test_migrations.py` |
| Constraint integrity | New unique constraints work with both NDVI and NDWI rows | `ndwi/tests/test_migrations.py` |

### Regression Tests

| What | How | Location |
|------|-----|----------|
| All NDVI engine tests pass | Run existing NDVI engine test suite | `ndvi/tests/` (unchanged) |
| All NDVI view tests pass | Run existing NDVI view test suite | `ndvi/tests/` (unchanged) |
| All NDVI fusion tests pass | Run existing NDVI fusion test suite | `ndvi/tests/` (unchanged) |
| NDVI cache not affected | NDVI cache keys start with `ndvi:cache:`, not `ndwi:cache:` | `ndwi/tests/test_no_regression.py` |
| NDVI Celery tasks not affected | NDVI tasks use `ndvi_ingestion` queue, not `ndwi_ingestion` | `ndwi/tests/test_no_regression.py` |

## Key Test Fixtures

### Synthetic NDWI Array

```python
@pytest.fixture
def synthetic_ndwi():
    """6×6 array with known NDWI values."""
    return np.array([
        [0.50, 0.30, 0.10, -0.10, -0.30, -0.50],  # water → dry
        [0.50, 0.30, 0.10, -0.10, -0.30, -0.50],
        [0.50, 0.30, 0.10, -0.10, -0.30, -0.50],
        [0.50, 0.30, 0.10, -0.10, -0.30, -0.50],
        [0.50, 0.30, 0.10, -0.10, -0.30, -0.50],
        [0.50, 0.30, 0.10, -0.10, -0.30, -0.50],
    ], dtype=np.float32)
```

Expected stats: mean=0.00, min=-0.50, max=0.50, sample_count=36.

### Engine Mock

```python
class MockNdwiEngine:
    engine_name = "ndwi_stac"

    def get_timeseries(self, *, bbox, start, end, step_days, max_cloud):
        return [
            NdwiPoint(
                date=date(2026, 6, 1), mean=0.15, min=-0.05, max=0.42,
                sample_count=850, cloud_fraction=0.05,
                valid_pixel_fraction=0.92, quality_flags={},
            ),
        ]

    def get_latest(self, *, bbox, lookback_days, max_cloud):
        return NdwiPoint(
            date=date(2026, 6, 8), mean=0.22, min=0.0, max=0.55,
            sample_count=920, cloud_fraction=0.02,
            valid_pixel_fraction=0.95, quality_flags={},
        )
```

## Pre-submission Checklist

Before claiming any phase complete:

1. [ ] Ruff lint + format: `ruff check ndwi/ && ruff format ndwi/`
2. [ ] MyPy: `mypy ndwi/` (no new type errors)
3. [ ] Bandit: `bandit -c pyproject.toml -r ndwi/` (no high-severity issues)
4. [ ] Existing NDVI tests: `python -m pytest ndvi/tests/ -x --tb=short`
5. [ ] New NDWI tests: `python -m pytest ndwi/tests/ -x --tb=short --cov=ndwi --cov-fail-under=96`
6. [ ] Migration dry-run: `python manage.py migrate --dry-run`
7. [ ] Schema regen: `python manage.py spectacular --file /tmp/schema.yml`
8. [ ] No regression on existing endpoints (run comparison against staging)
