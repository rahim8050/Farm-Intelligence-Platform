# Stage 1 Implementation Gap Analysis (Resolved)

> Historical snapshot captured on April 4, 2026.
> This document preserves the pre-implementation gap analysis for Stage 1 and
> should not be read as current implementation state.
> For the consolidated NDVI architecture and implementation spec, see
> `docs/architecture/ndvi-system-evolution-phased-spec.md`.
> For live status, see `docs/status/NDVI_PIPELINE_IMPLEMENTATION_STATUS.md`.

**Reference:** `docs/architecture/ndvi-phase-2-implementation-plan.md` - Stage 1  
**Date:** April 4, 2026  
**Status:** ✅ **Resolved** - Routing switch implementation is complete

> Historical note: this document records the gap that existed before Stage 1
> was completed. The code now implements the dispatch boundary and the
> remaining Redis Streams work starts at Stage 3.

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

**Status:** ✅ **COMPLETE** - Setting exists and is used for branching

**What's Implemented:**
- ✅ `NDVI_QUEUE_BACKEND` setting defined in `config/settings.py:694`
- ✅ Default value: `"celery"`
- ✅ `get_ndvi_queue_backend()` helper exists in `ndvi/services.py:58`
- ✅ Setting reads from Django settings correctly
- ✅ Test coverage exists: `test_get_ndvi_queue_backend_reads_settings()`

- ✅ `dispatch_ndvi_job()` branches on `NDVI_QUEUE_BACKEND`
- ✅ `dispatch_farm_state_coverage()` branches on `NDVI_QUEUE_BACKEND`
- ✅ `stream` currently raises `NotImplementedError` until Stage 3 producer work exists

---

## Stage 1 Expected Outcomes (Per Plan)

### Outcome 1: No runtime behavior change yet

**Status:** ✅ **ACHIEVED**
- All dispatch calls still route to Celery by default
- No functional changes to job execution for the default backend
- Tests pass with current behavior

---

### Outcome 2: All NDVI enqueue behavior flows through one place

**Status:** ✅ **ACHIEVED**
- All NDVI job dispatch goes through `dispatch_ndvi_job()`
- All coverage job dispatch goes through `dispatch_farm_state_coverage()`
- No scattered `.delay()` calls remaining in views/tasks

---

### Outcome 3: Future Redis Streams logic can be added without editing every call site

**Status:** ✅ **ACHIEVED**
- ✅ Call sites are centralized (only 2 functions to modify)
- ✅ Routing switch is implemented
- ⚠️ Stream mode still needs producer implementation before it can be enabled
- ✅ The architecture is ready for Stage 3 without further call-site edits

---

## File Targets (Per Plan)

| File | Requirement | Status |
|------|-------------|--------|
| `ndvi/services.py` | Add dispatch helpers | ✅ Complete |
| `ndvi/views.py` | Use dispatch helpers | ✅ Complete (4 call sites) |
| `ndvi/tasks.py` | Use dispatch helpers | ✅ Complete (3 call sites) |
| `config/settings.py` | Add NDVI_QUEUE_BACKEND | ✅ Complete |

---

## Stage 1 Completion Checklist

- [x] Add `dispatch_ndvi_job()` helper
- [x] Add `dispatch_farm_state_coverage()` helper
- [x] Replace all `run_ndvi_job.delay()` calls with `dispatch_ndvi_job()`
- [x] Replace all `compute_farm_state_coverage.delay()` calls with `dispatch_farm_state_coverage()`
- [x] Add `NDVI_QUEUE_BACKEND` setting
- [x] Add `get_ndvi_queue_backend()` helper
- [x] Add routing switch in `dispatch_ndvi_job()`
- [x] Add routing switch in `dispatch_farm_state_coverage()`
- [x] Add tests for routing behavior

## Conclusion

**Stage 1 is complete.** The dispatch boundary is centralized, the routing
switch exists, and the Stage 1 gap has been closed. The next work is Stage 3
producer and Stage 4 consumer implementation.
