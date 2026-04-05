# Stage 1 Implementation Gap Analysis

**Reference:** `docs/architecture/ndvi-phase-2-implementation-plan.md` - Stage 1  
**Date:** April 4, 2026  
**Status:** 🟡 **~70% Complete** - Missing routing switch implementation

---

## Stage 1 Requirements (Per Implementation Plan)

### Requirement 1: Add one NDVI dispatch helper in `ndvi/services.py`

**Status:** ✅ **COMPLETE**

**Evidence:**
- ✅ `dispatch_ndvi_job()` exists at `ndvi/services.py:474`
- ✅ `dispatch_farm_state_coverage()` exists at `ndvi/services.py:482`
- ✅ Both functions have docstrings explaining Stage 1 intent
- ✅ Code comments explicitly note these are routing boundaries for future Streams work

**Implementation:**
```python
def dispatch_ndvi_job(job: NdviJob | int) -> None:
    # Stage 1 keeps the existing Celery dispatch path intact. Later queue
    # backends should branch here instead of at every call site.
    from .tasks import run_ndvi_job
    
    job_id = job.id if isinstance(job, NdviJob) else int(job)
    run_ndvi_job.delay(job_id)
```

---

### Requirement 2: Replace every direct `run_ndvi_job.delay(...)` call

**Status:** ✅ **COMPLETE**

**Evidence:**
- ✅ Only `.delay()` call in `ndvi/` is inside `dispatch_ndvi_job()` itself (line 479)
- ✅ All other code uses `dispatch_ndvi_job()`:
  - `ndvi/views.py:642` - Refresh endpoint
  - `ndvi/views.py:727` - Gap fill endpoint
  - `ndvi/views.py:929` - Raster queue endpoint
  - `ndvi/views.py:981` - Manual refresh endpoint
  - `ndvi/tasks.py:256` - Daily refresh periodic task
  - `ndvi/tasks.py:381` - Weekly gap fill periodic task

**Call Sites Migrated:**
| File | Line | Function | Status |
|------|------|----------|--------|
| `ndvi/views.py` | 642 | `NdviRefreshView.post()` | ✅ Using dispatch_ndvi_job() |
| `ndvi/views.py` | 727 | `NdviGapFillView.post()` | ✅ Using dispatch_ndvi_job() |
| `ndvi/views.py` | 929 | `NdviRasterQueueView.post()` | ✅ Using dispatch_ndvi_job() |
| `ndvi/views.py` | 981 | `NdviManualRefreshView.post()` | ✅ Using dispatch_ndvi_job() |
| `ndvi/tasks.py` | 256 | `enqueue_daily_refresh()` | ✅ Using dispatch_ndvi_job() |
| `ndvi/tasks.py` | 381 | `enqueue_weekly_gap_fill()` | ✅ Using dispatch_ndvi_job() |

---

### Requirement 3: Replace every direct `compute_farm_state_coverage.delay(...)` call

**Status:** ✅ **COMPLETE**

**Evidence:**
- ✅ Only `.delay()` call in `ndvi/` is inside `dispatch_farm_state_coverage()` itself (line 493)
- ✅ All other code uses `dispatch_farm_state_coverage()`:
  - `ndvi/tasks.py:346` - Daily farm state coverage periodic task
  - `ndvi/farm_state.py:273` - Farm state computation helper

**Call Sites Migrated:**
| File | Line | Function | Status |
|------|------|----------|--------|
| `ndvi/tasks.py` | 346 | `enqueue_daily_farm_state_coverage()` | ✅ Using dispatch_farm_state_coverage() |
| `ndvi/farm_state.py` | 273 | `compute_and_dispatch_coverage()` | ✅ Using dispatch_farm_state_coverage() |

---

### Requirement 4: Introduce a routing switch in settings

**Status:** ❌ **INCOMPLETE** - Setting exists but not used for branching

**What's Implemented:**
- ✅ `NDVI_QUEUE_BACKEND` setting defined in `config/settings.py:694`
- ✅ Default value: `"celery"`
- ✅ `get_ndvi_queue_backend()` helper exists in `ndvi/services.py:58`
- ✅ Setting reads from Django settings correctly
- ✅ Test coverage exists: `test_get_ndvi_queue_backend_reads_settings()`

**What's Missing:**
- ❌ **No branching logic in `dispatch_ndvi_job()`**
  - Currently always calls `run_ndvi_job.delay()` regardless of `NDVI_QUEUE_BACKEND` value
  - Should check `get_ndvi_queue_backend()` and route accordingly
  
- ❌ **No branching logic in `dispatch_farm_state_coverage()`**
  - Currently always calls `compute_farm_state_coverage.delay()` regardless of setting
  - Should check `get_ndvi_queue_backend()` and route accordingly

**Current Implementation (WRONG):**
```python
def dispatch_ndvi_job(job: NdviJob | int) -> None:
    # Stage 1 keeps the existing Celery dispatch path intact. Later queue
    # backends should branch here instead of at every call site.
    from .tasks import run_ndvi_job

    job_id = job.id if isinstance(job, NdviJob) else int(job)
    run_ndvi_job.delay(job_id)  # ← Always uses Celery, ignores setting!
```

**Expected Implementation (PER PLAN):**
```python
def dispatch_ndvi_job(job: NdviJob | int) -> None:
    backend = get_ndvi_queue_backend()
    
    if backend == "stream":
        # Future: publish to Redis Streams
        from .streams import publish_ndvi_job
        publish_ndvi_job(job)
    else:
        # Default: use Celery
        from .tasks import run_ndvi_job
        job_id = job.id if isinstance(job, NdviJob) else int(job)
        run_ndvi_job.delay(job_id)
```

---

## Stage 1 Expected Outcomes (Per Plan)

### Outcome 1: No runtime behavior change yet

**Status:** ✅ **ACHIEVED**
- All dispatch calls still route to Celery
- No functional changes to job execution
- Tests pass with current behavior

---

### Outcome 2: All NDVI enqueue behavior flows through one place

**Status:** ✅ **ACHIEVED**
- All NDVI job dispatch goes through `dispatch_ndvi_job()`
- All coverage job dispatch goes through `dispatch_farm_state_coverage()`
- No scattered `.delay()` calls remaining in views/tasks

---

### Outcome 3: Future Redis Streams logic can be added without editing every call site

**Status:** 🟡 **PARTIALLY ACHIEVED**
- ✅ Call sites are centralized (only 2 functions to modify)
- ❌ Routing switch not implemented yet
- ⚠️ **Risk:** When stream mode is enabled, both dispatch functions need code changes anyway
- ✅ But the architecture is ready - just need to add the branching logic

---

## File Targets (Per Plan)

| File | Requirement | Status |
|------|-------------|--------|
| `ndvi/services.py` | Add dispatch helpers | ✅ Complete |
| `ndvi/views.py` | Use dispatch helpers | ✅ Complete (4 call sites) |
| `ndvi/tasks.py` | Use dispatch helpers | ✅ Complete (3 call sites) |
| `config/settings.py` | Add NDVI_QUEUE_BACKEND | ✅ Complete |

---

## What's Missing to Complete Stage 1

### Missing Item 1: Routing switch in `dispatch_ndvi_job()`

**File:** `ndvi/services.py:474`  
**Lines to add:** ~8 lines  
**Complexity:** Low

**What needs to happen:**
```python
def dispatch_ndvi_job(job: NdviJob | int) -> None:
    backend = get_ndvi_queue_backend()
    
    if backend == "stream":
        # TODO: Implement in Stage 3
        raise NotImplementedError(
            "Redis Streams backend not yet implemented. "
            "Set NDVI_QUEUE_BACKEND=celery"
        )
    
    # Celery backend (default)
    from .tasks import run_ndvi_job
    job_id = job.id if isinstance(job, NdviJob) else int(job)
    run_ndvi_job.delay(job_id)
```

---

### Missing Item 2: Routing switch in `dispatch_farm_state_coverage()`

**File:** `ndvi/services.py:482`  
**Lines to add:** ~8 lines  
**Complexity:** Low

**What needs to happen:**
```python
def dispatch_farm_state_coverage(
    *,
    farm_id: int,
    engine: str | None = None,
    target_date: date,
    threshold: float,
) -> None:
    backend = get_ndvi_queue_backend()
    
    if backend == "stream":
        # TODO: Implement in Stage 3
        raise NotImplementedError(
            "Redis Streams backend not yet implemented. "
            "Set NDVI_QUEUE_BACKEND=celery"
        )
    
    # Celery backend (default)
    from .tasks import compute_farm_state_coverage
    compute_farm_state_coverage.delay(
        farm_id=farm_id,
        engine=engine,
        target_date=target_date.isoformat(),
        threshold=threshold,
    )
```

---

### Missing Item 3: Tests for routing switch

**File:** `ndvi/tests/test_ndvi_services.py`  
**Lines to add:** ~30 lines  
**Complexity:** Low

**Tests needed:**
1. `test_dispatch_ndvi_job_routes_to_celery_when_backend_is_celery()`
2. `test_dispatch_ndvi_job_raises_when_backend_is_stream()` (until Stage 3 complete)
3. `test_dispatch_farm_state_coverage_routes_to_celery_when_backend_is_celery()`
4. `test_dispatch_farm_state_coverage_raises_when_backend_is_stream()` (until Stage 3 complete)

---

## Stage 1 Completion Checklist

- [x] Add `dispatch_ndvi_job()` helper
- [x] Add `dispatch_farm_state_coverage()` helper
- [x] Replace all `run_ndvi_job.delay()` calls with `dispatch_ndvi_job()`
- [x] Replace all `compute_farm_state_coverage.delay()` calls with `dispatch_farm_state_coverage()`
- [x] Add `NDVI_QUEUE_BACKEND` setting
- [x] Add `get_ndvi_queue_backend()` helper
- [ ] **Add routing switch in `dispatch_ndvi_job()`** ← MISSING
- [ ] **Add routing switch in `dispatch_farm_state_coverage()`** ← MISSING
- [ ] **Add tests for routing behavior** ← MISSING

---

## Effort Estimate to Complete Stage 1

| Task | Lines of Code | Time |
|------|---------------|------|
| Add routing switch to `dispatch_ndvi_job()` | ~8 lines | 15 minutes |
| Add routing switch to `dispatch_farm_state_coverage()` | ~8 lines | 15 minutes |
| Add routing tests (4 tests) | ~30 lines | 30 minutes |
| **Total** | **~46 lines** | **~1 hour** |

---

## Recommended Implementation Order

1. **Add routing switch to `dispatch_ndvi_job()`** (15 min)
   - Check `get_ndvi_queue_backend()`
   - Branch to Celery (default) or raise NotImplementedError for stream
   - Add comment noting this is where Stage 3 stream logic will go

2. **Add routing switch to `dispatch_farm_state_coverage()`** (15 min)
   - Same pattern as above
   - Keep existing Celery logic as default branch

3. **Add tests** (30 min)
   - Test Celery routing works when `NDVI_QUEUE_BACKEND=celery`
   - Test stream routing raises NotImplementedError when `NDVI_QUEUE_BACKEND=stream`
   - Use `@override_settings(NDVI_QUEUE_BACKEND="stream")` for test

4. **Run full test suite** (5 min)
   - Ensure no regressions
   - Verify all existing tests still pass

**Total time to complete Stage 1: ~1 hour**

---

## Blocking Dependencies for Stage 2+

Once Stage 1 is complete, the following are unblocked:

- ✅ **Stage 3 (Stream Producer)**: Can implement `publish_ndvi_job()` independently
- ✅ **Stage 4 (Stream Consumer)**: Can implement consumer logic independently
- ✅ **Stage 5 (Settings)**: Can add remaining stream settings
- ⚠️ **Stage 6 (Observability)**: Partially blocked (can add Celery metrics, but stream metrics need Stage 3-4)
- ⚠️ **Stage 7 (Tests)**: Partially blocked (stream tests need Stage 3-4)
- ⚠️ **Stage 8 (Rollout)**: Blocked until Stage 3-4 complete

---

## Conclusion

**Stage 1 is 70% complete.** The heavy lifting (centralizing dispatch, migrating all call sites) is done. What's missing is just the routing switch logic (~46 lines of code, ~1 hour of work).

**Priority:** HIGH - Completing Stage 1 unblocks the entire Phase 2 implementation path and makes the routing boundary explicit rather than implicit.

**Risk if not completed:** When Stage 3 (stream producer) is implemented, both dispatch functions will need to be modified anyway. Adding the routing switch now makes that change cleaner and more intentional.
