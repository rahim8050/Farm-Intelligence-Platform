# NDVI Retry Policy – Implementation Status

**Date:** April 10, 2026
**Policy Module:** `ndvi/retry_policy.py`
**Related:** `docs/contributing_weather_engines.md`

---

## Executive Summary

The NDVI retry policy has been hardened into a **single source of truth** that
governs retry decisions for all upstream service interactions (STAC, SentinelHub
metrics, SentinelHub raster). Status-code classification is centralized in
`classify_status_code()` and all NDVI error types inherit from
`UpstreamFailureError`, ensuring consistent `retryable`, `category`,
`status_code`, and `delay` attributes across the codebase.

**Phase 1 (policy consolidation) is complete.** Phase 2 (circuit breaker
expansion) and Phase 3 (observability) remain.

---

## Phase 1 — Policy Consolidation

**Status:** ✅ **COMPLETE** (April 10, 2026)

### What's Implemented:

- ✅ **`classify_status_code()` — single source of truth**
  - Located in `ndvi/retry_policy.py`.
  - Canonical truth table for HTTP status → retry category mapping.
  - Documented with full truth table in docstring (13 branches).
  - Backwards-compatible alias `_status_retry_classification` preserved.

- ✅ **Unified exception hierarchy**
  - `StacUpstreamError` → inherits `UpstreamFailureError + StacError`.
  - `SentinelHubUpstreamError` → inherits `UpstreamFailureError`.
  - `SentinelHubRasterError` → inherits `UpstreamFailureError`.
  - `SentinelHubAuthError` → inherits `UpstreamFailureError` (previously plain
    `RuntimeError`).
  - All constructors delegate to `classify_status_code()`.

- ✅ **`should_retry()` — central retry decision function**
  - Reads `retryable`, `category`, `delay`, `status_code` from exceptions.
  - Returns `RetryDecision(retry, delay, reason)`.
  - Handles non-`NdviFailureError` exceptions gracefully (returns
    `retry=False`).

- ✅ **Celery task handler uses shared retry logic**
  - `_handle_retryable_task_failure()` in `ndvi/tasks.py` calls
    `should_retry()` and applies Celery retry with correct countdown.
  - Catches `MaxRetriesExceededError` and marks job as FAILED.

- ✅ **Network error handling fixed**
  - `httpx.RequestError` after inline retry exhaustion is now wrapped in
    the appropriate `UpstreamFailureError` subclass (previously bare `raise`
    caused `should_retry()` to return `retry=False`).

- ✅ **Duplicate code eliminated**
  - Removed `_retry_category_for_status()` from `stac_client.py` (was defined
    **twice** verbatim).
  - Removed inline if/elif chains from `SentinelHubUpstreamError` and
    `SentinelHubRasterError`.

- ✅ **Test coverage: 28 tests**
  - `test_classify_status_code_truth_table`: Parametrized, covers all 13
    branches (400, 401, 403, 422, 429, 500, 502, 503, 504, 200, 201, 204,
    None).
  - 7 edge case tests for `should_retry()` with various exception types.
  - Updated 2 existing tests to expect wrapped exceptions.

### Truth Table (Canonical):

| Status Code | Retryable | Category             |
|-------------|-----------|----------------------|
| 401, 403    | False     | AUTH                 |
| 400, 422    | False     | VALIDATION           |
| 429         | True      | RATE_LIMIT           |
| >= 500      | True      | TRANSIENT_UPSTREAM   |
| Other/None  | False     | UNKNOWN              |

### What's Left Out:

- ⚠️ **No `Retry-After` header parsing** — 429 responses retried with default
  delay instead of server-suggested cooldown.
- ⚠️ **`SentinelHubAuthError` does not extract `status_code` from response** —
  caller must pass it explicitly.

---

## Phase 2 — Circuit Breaker Expansion

**Status:** 🔶 **IN PROGRESS** (STAC only; SentinelHub engines pending)

### What's Implemented:

- ✅ **`_CircuitBreaker` in `ndvi/stac_client.py`**
  - Three-state machine: CLOSED → OPEN → HALF_OPEN.
  - Opens after `NDVI_STAC_CIRCUIT_BREAKER_THRESHOLD` (default: 3) failures.
  - Auto-recovers after `NDVI_STAC_CIRCUIT_BREAKER_TIMEOUT_SECS` (default:
    300s).
  - Logs state transitions at INFO level.
  - Raises `StacUpstreamError(retryable=False)` when circuit is open.

- ✅ **Celery task respects circuit breaker**
  - `run_ndvi_job` catches `StacUpstreamError` with `retryable=False` and
    marks job as FAILED (no wasted retries).
  - Test: `test_run_ndvi_job_stac_circuit_breaker_persists_across_retries`.

### What's Left:

- ⏳ **Extract `_CircuitBreaker` to shared module**
  - Move from `ndvi/stac_client.py` to `ndvi/circuit_breaker.py`.
  - Add unit tests for all state transitions.
  - Make class generic (not STAC-specific).

- ⏳ **Add circuit breaker to SentinelHub metrics engine**
  - `ndvi/engines/sentinelhub.py` — `_request_with_retry()`.
  - Settings: `NDVI_SENTINELHUB_CIRCUIT_BREAKER_THRESHOLD`,
    `NDVI_SENTINELHUB_CIRCUIT_BREAKER_TIMEOUT_SECS`.

- ⏳ **Add circuit breaker to SentinelHub raster engine**
  - `ndvi/raster/sentinelhub_engine.py` — `_request_with_retry()`.
  - Same settings pattern as metrics engine.

- ⏳ **Update `.env.example`** with new SentinelHub circuit breaker settings.

- ⏳ **Integration tests** that mock circuit breaker state transitions for
  SentinelHub engines.

---

## Phase 3 — Observability & Admin Controls

**Status:** 🔴 **NOT STARTED**

### What's Planned:

- 🔲 **Prometheus metrics for circuit breaker state**
  - Gauge: `ndvi_circuit_breaker_state{engine, upstream}`.
  - Values: 0 (CLOSED), 1 (OPEN), 2 (HALF_OPEN).
  - Counter: `ndvi_circuit_breaker_transitions_total{engine, from_state,
    to_state}`.

- 🔲 **Admin endpoint to reset circuit breaker**
  - `POST /api/v1/ndvi/circuit-breaker/reset` with body
    `{"engine": "stac"|"sentinelhub"}`.
  - Auth: `IsAdminUser` or superuser.
  - Returns envelope with previous state and new state.

- 🔲 **Health check endpoint**
  - `GET /api/v1/ndvi/health/upstream` returns status of all upstream
    services and their circuit breaker states.
  - Useful for dashboards and alerting.

- 🔲 **Retry-After header parsing**
  - Parse `Retry-After` from 429 responses.
  - Pass as `delay` to exception constructors.
  - Celery task handler uses `decision.delay` as countdown when available.

---

## Files Modified (Phase 1)

| File | Lines Changed | Description |
|------|---------------|-------------|
| `ndvi/retry_policy.py` | NEW (56) | Canonical retry policy with truth table |
| `ndvi/stac_client.py` | +24/-15 | Simplified, removed duplicates |
| `ndvi/engines/sentinelhub.py` | +24/-6 | Added `SentinelHubUpstreamError` |
| `ndvi/raster/sentinelhub_engine.py` | +11/-4 | Added `SentinelHubRasterError` |
| `ndvi/tasks.py` | +58/-38 | Extracted shared retry helper |
| `ndvi/tests/test_ndvi_retry_policy.py` | NEW (84) | 28 comprehensive tests |
| `ndvi/tests/test_ndvi_sentinelhub_engine.py` | +2/-1 | Updated assertion |
| `ndvi/tests/test_ndvi_raster_engines.py` | +1/-1 | Updated assertion |

**Total:** ~201 lines added across 8 files (2 new files).

---

## Verification

- ✅ pre-commit (ruff, ruff format, bandit, mypy) passed.
- ✅ 110 tests passed across retry policy, STAC client, raster engines,
  SentinelHub engine, and task tests.
- ✅ No regressions in existing functionality.

---

## Recommended Next Steps

1. **Extract `_CircuitBreaker`** to `ndvi/circuit_breaker.py` and add unit
   tests for all state transitions.
2. **Add circuit breaker** to SentinelHub metrics and raster engines.
3. **Add Prometheus gauge** for circuit breaker state per engine.
4. **Implement Retry-After parsing** for 429 rate-limit responses.
5. **Add admin endpoint** `POST /api/v1/ndvi/circuit-breaker/reset`.
6. **Add health check** `GET /api/v1/ndvi/health/upstream`.

---

## Commit

- `2c1c12d` refactor(ndvi): harden retry policy into single source of truth
