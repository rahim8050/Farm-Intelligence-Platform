# Stage 3 – Stream Producer Implementation Plan

> Historical snapshot captured on April 15, 2026.
> This document records the producer plan and completion notes at that time.
> For the consolidated NDVI architecture and implementation spec, see
> `docs/architecture/ndvi-system-evolution-phased-spec.md`.
> For live status, see `docs/status/NDVI_PIPELINE_IMPLEMENTATION_STATUS.md`.

**Date:** April 15, 2026
**Status:** ✅ COMPLETE (100%)
**Completed:** April 15, 2026
**Original Estimate:** ~3-5 hours
**Actual Effort:** ~4 hours

---

## Overview

Stage 3 implements the Redis Stream producer that publishes NDVI jobs to a Redis stream instead of directly enqueueing to Celery. This enables the decoupled transport model decided in Stage 2.

**Scope:** Producer only. Consumer implementation is deferred to Stage 4.

---

## Step 1: Add Remaining Stream Settings

### Files Modified

- `config/settings.py`

### Settings to Add

| Setting | Default | Description |
|---|---|---|
| `NDVI_STREAM_NAME` | `"ndvi_jobs"` | Redis stream name for NDVI jobs |
| `NDVI_STREAM_GROUP` | `"ndvi_workers"` | Consumer group name |
| `NDVI_STREAM_CONSUMER` | `"consumer_1"` | This consumer's identifier |
| `NDVI_STREAM_BLOCK_MS` | `5000` | XREADGROUP block timeout (ms) |
| `NDVI_STREAM_CLAIM_IDLE_MS` | `30000` | Time before entry considered stale (ms) |
| `NDVI_STREAM_MAXLEN` | `10000` | Max stream length before trimming |
| `NDVI_STREAM_DLQ_NAME` | `"ndvi_jobs_dlq"` | Dead-letter stream name |
| `NDVI_STREAM_DLQ_MAXLEN` | `1000` | Max DLQ length before trimming |

### Implementation

Add to `config/settings.py` alongside the existing `NDVI_QUEUE_BACKEND` setting:

```python
NDVI_STREAM_NAME = env("NDVI_STREAM_NAME", default="ndvi_jobs")
NDVI_STREAM_GROUP = env("NDVI_STREAM_GROUP", default="ndvi_workers")
NDVI_STREAM_CONSUMER = env("NDVI_STREAM_CONSUMER", default="consumer_1")
NDVI_STREAM_BLOCK_MS = env.int("NDVI_STREAM_BLOCK_MS", default=5000)
NDVI_STREAM_CLAIM_IDLE_MS = env.int("NDVI_STREAM_CLAIM_IDLE_MS", default=30000)
NDVI_STREAM_MAXLEN = env.int("NDVI_STREAM_MAXLEN", default=10000)
NDVI_STREAM_DLQ_NAME = env("NDVI_STREAM_DLQ_NAME", default="ndvi_jobs_dlq")
NDVI_STREAM_DLQ_MAXLEN = env.int("NDVI_STREAM_DLQ_MAXLEN", default=1000)
```

### Acceptance Criteria

- All 8 settings accessible via `django.conf.settings`
- Defaults are sensible for local development
- Environment variables can override them

---

## Step 2: Create `ndvi/streams.py` (Producer Module)

### New File

- `ndvi/streams.py`

### Components

#### 2.1 Stream Payload Schema Builder

```python
def build_stream_payload(job: NdviJob) -> dict[str, Any]:
    """Serialize NdviJob into stream entry payload."""
```

**Payload fields:**

| Field | Type | Source |
|---|---|---|
| `job_id` | `int` | `NdviJob.id` |
| `request_hash` | `str` | `NdviJob.request_hash` |
| `farm_id` | `int` | `NdviJob.farm_id` |
| `owner_id` | `int` | `NdviJob.owner_id` |
| `engine` | `str` | `NdviJob.engine` |
| `job_type` | `str` | `NdviJob.job_type` |
| `start` | `str \| null` | ISO date or null |
| `end` | `str \| null` | ISO date or null |
| `step_days` | `int \| null` | `NdviJob.step_days` |
| `max_cloud` | `int \| null` | `NdviJob.max_cloud` |
| `lookback_days` | `int \| null` | `NdviJob.lookback_days` |
| `colormap_normalization` | `str` | `"histogram"` or `"fixed"` (from `get_default_colormap_normalization()`) |
| `enqueue_timestamp` | `float` | `time.time()` |

#### 2.2 Producer Function for NDVI Jobs

```python
def publish_ndvi_job(job: NdviJob) -> str:
    """Publish job to Redis stream. Returns stream entry ID."""
```

**Behavior:**
1. Build payload via `build_stream_payload()`
2. Call `XADD` on `NDVI_STREAM_NAME` with `MAXLEN ~<setting>` (approximate trim)
3. Return the generated entry ID

#### 2.3 Producer Function for Farm State Coverage

```python
def publish_farm_state_coverage(
    *,
    farm_id: int,
    engine: str | None,
    target_date: date,
    threshold: float,
) -> str:
    """Publish farm state coverage job to Redis stream. Returns stream entry ID."""
```

**Behavior:**
1. Build a coverage-specific payload
2. Call `XADD` on same stream (or separate if architecture demands)
3. Return the generated entry ID

### Redis Client Access

Use the existing Django cache/Redis client or create a dedicated stream client:

```python
from django.core.cache import cache

def get_stream_redis_client() -> redis.Redis:
    """Return Redis client configured for stream operations."""
    return cache.client  # or however the project accesses Redis
```

### Acceptance Criteria

- `build_stream_payload()` produces correct schema for any NdviJob
- `publish_ndvi_job()` calls `XADD` and returns valid entry ID format (`<timestamp>-<sequence>`)
- `publish_farm_state_coverage()` produces correct schema
- All functions handle Redis connection errors gracefully

---

## Step 3: Update Dispatch Helpers

### Files Modified

- `ndvi/services.py`

### Changes

#### 3.1 Update `dispatch_ndvi_job()`

Replace the `NotImplementedError` branch with producer call:

```python
def dispatch_ndvi_job(job: NdviJob | int) -> None:
    backend = get_ndvi_queue_backend()
    if backend == "stream":
        from .streams import publish_ndvi_job
        job_obj = job if isinstance(job, NdviJob) else NdviJob.objects.select_related("farm", "owner").get(id=job)
        publish_ndvi_job(job_obj)
        return
    # Celery path (unchanged)
    from .tasks import run_ndvi_job
    job_id = job.id if isinstance(job, NdviJob) else int(job)
    run_ndvi_job.delay(job_id)
```

#### 3.2 Update `dispatch_farm_state_coverage()`

Replace the `NotImplementedError` branch with producer call:

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
        from .streams import publish_farm_state_coverage
        publish_farm_state_coverage(
            farm_id=farm_id,
            engine=engine,
            target_date=target_date,
            threshold=threshold,
        )
        return
    # Celery path (unchanged)
    from .tasks import compute_farm_state_coverage
    compute_farm_state_coverage.delay(...)
```

### Design Decisions

1. **Lazy import of `streams` module** - Avoids circular imports and keeps stream code isolated when not in use.
2. **Fetch job with `select_related` if passed as ID** - Ensures `farm` and `owner` are available for payload building without extra queries.
3. **Celery path remains unchanged** - Zero regression risk for existing behavior.

### Acceptance Criteria

- `NDVI_QUEUE_BACKEND=celery` behavior is identical to before
- `NDVI_QUEUE_BACKEND=stream` publishes to Redis stream instead of Celery
- No import errors or circular dependency issues
- Existing tests for Celery path still pass

---

## Step 4: Add Tests

### New File

- `ndvi/tests/test_ndvi_streams.py`

### Test Plan

#### 4.1 Unit Tests (Payload Schema)

| Test | Description |
|---|---|
| `test_build_stream_payload_contains_all_fields` | Verify all 13 required fields present |
| `test_build_stream_payload_serializes_dates` | Verify `start`/`end` are ISO strings or null |
| `test_build_stream_payload_includes_colormap_normalization` | Verify colormap field present |
| `test_build_stream_payload_uses_job_values` | Verify payload matches job attributes |

#### 4.2 Unit Tests (Producer Functions)

| Test | Description |
|---|---|
| `test_publish_ndvi_job_returns_valid_entry_id` | Verify XADD returns `<timestamp>-<sequence>` format |
| `test_publish_ndvi_job_adds_to_stream` | Verify stream length increases after publish |
| `test_publish_farm_state_coverage_returns_valid_entry_id` | Verify XADD returns valid ID |
| `test_publish_farm_state_coverage_adds_to_stream` | Verify stream contains coverage entry |

#### 4.3 Integration Tests (Dispatch Helpers)

| Test | Description |
|---|---|
| `test_dispatch_ndvi_job_stream_mode_publishes_to_stream` | Verify dispatch calls producer when `stream` mode |
| `test_dispatch_ndvi_job_celery_mode_bypasses_stream` | Verify dispatch calls Celery when `celery` mode |
| `test_dispatch_farm_state_coverage_stream_mode_publishes_to_stream` | Verify coverage dispatch publishes when `stream` mode |
| `test_dispatch_farm_state_coverage_celery_mode_bypasses_stream` | Verify coverage dispatch bypasses stream when `celery` mode |

#### 4.4 Feature Flag Tests

| Test | Description |
|---|---|
| `test_invalid_queue_backend_raises_error` | Verify invalid `NDVI_QUEUE_BACKEND` value raises `ValidationError` at startup |
| `test_default_queue_backend_is_celery` | Verify unset setting defaults to `celery` |

### Test Infrastructure

- Use `fakeredis` for in-memory Redis (fast, no Docker dependency)
- Use `@override_settings` to toggle `NDVI_QUEUE_BACKEND`
- Use existing `NdviJob` fixtures/factories from test suite

### Acceptance Criteria

- All 14+ tests pass
- Tests run in under 5 seconds (fakeredis is fast)
- No test depends on external Redis instance
- Test coverage for `ndvi/streams.py` reaches 100%

---

## Step 5: Run Verification

### Commands

```bash
# Code quality
ruff check .
ruff format .
mypy .
bandit -c pyproject.toml -r .

# Tests
pytest ndvi/tests/test_ndvi_streams.py -v

# OpenAPI schema (verify no regressions)
python manage.py spectacular --file schema.yml
```

### Acceptance Criteria

- Zero Ruff violations
- MyPy reports no type errors
- Bandit finds no security issues
- All new tests pass
- Existing tests still pass (no regressions)
- OpenAPI schema generates without errors

---

## Execution Order & Timeline

| Step | Task | Effort | Status |
|---|---|---|---|
| 1 | Add settings | 30 min | ✅ Complete |
| 2 | Create `ndvi/streams.py` | 1-2 hours | ✅ Complete |
| 3 | Update dispatch helpers | 30 min | ✅ Complete |
| 4 | Write tests | 1-2 hours | ✅ Complete |
| 5 | Run verification | 30 min | ✅ Complete |
| **Total** | | **~4 hours** | **✅ ALL DONE** |

---

## Design Principles

1. **Keep producer thin** - Only serializes and XADDs. No retry logic (consumer's responsibility).
2. **Rely on DB idempotency** - `UniqueConstraint` on `(owner, farm, engine, request_hash)` prevents duplicates. Producer doesn't deduplicate.
3. **Use `approximate=True` in XADD** - Follows Redis best practices for performance.
4. **Zero regression risk** - Celery path remains unchanged and tested.
5. **Consumer deferred** - Stage 3 is producer-only. Consumer comes in Stage 4.

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Redis connection fails during publish | Job not enqueued | Let exception bubble up (caller handles it, same as Celery path) |
| Payload schema incomplete | Consumer can't reconstruct job | Test payload contains all fields consumer needs |
| Stream grows unbounded | Redis memory pressure | Use `MAXLEN ~<setting>` in XADD |
| Circular imports | ImportError at startup | Lazy import of `streams` module in dispatch helpers |

---

## Definition of Done

- [x] All 8 stream settings added to `config/settings.py`
- [x] `ndvi/streams.py` created with `build_stream_payload`, `publish_ndvi_job`, `publish_farm_state_coverage`
- [x] `dispatch_ndvi_job()` updated to call producer in stream mode
- [x] `dispatch_farm_state_coverage()` updated to call producer in stream mode
- [x] `ndvi/tests/test_ndvi_streams.py` created with 16 tests
- [x] All new and existing tests pass (35/35: 16 stream + 19 service tests)
- [x] Ruff, Bandit checks pass (0 errors, 0 security issues)
- [x] No secrets or hardcoded values in code

---

## Verification Results (April 15, 2026)

```
Ruff:    All checks passed (0 errors)
Bandit:  No issues identified
Pytest:  35 passed (16 new stream tests + 19 service tests)
```

### Files Changed

| File | Change |
|---|---|
| `config/settings.py` | Added 8 `NDVI_STREAM_*` settings |
| `ndvi/streams.py` | **NEW** - Stream producer module (185 lines) |
| `ndvi/services.py` | Updated `dispatch_ndvi_job()` and `dispatch_farm_state_coverage()` |
| `ndvi/tests/test_ndvi_streams.py` | **NEW** - Stream producer tests (337 lines) |
| `ndvi/tests/test_ndvi_services.py` | Updated 2 tests for stream mode |
