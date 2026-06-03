# NDVI Retry Policy – Implementation Status

**Date:** April 10, 2026 (Phase 1)
**Date:** April 12, 2026 (Phases 2 and 3)
**Date:** June 03, 2026 (re-verified)
**Policy Module:** `ndvi/retry_policy.py`
**Related:** `docs/contributing_weather_engines.md`, `NDVI_PIPELINE_IMPLEMENTATION_STATUS.md`

> **Re-verification (June 03, 2026):** This document was last rewritten on
> April 12, 2026. Re-verified against the current code: all three phases
> remain complete. The shared `CircuitBreaker` is in
> `ndvi/circuit_breaker.py:46`; the admin reset endpoint is at
> `ndvi/views.py:1309`; the upstream health endpoint is at
> `ndvi/views.py:1383`; circuit-breaker metrics are in
> `ndvi/metrics.py:69-80`. No drift detected.

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

**Status:** ✅ **COMPLETE** (April 11, 2026)

### What's Implemented:

- ✅ **`CircuitBreaker` in `ndvi/circuit_breaker.py`** — generic, reusable
  - Three-state machine: CLOSED → OPEN → HALF_OPEN.
  - Opens after `NDVI_{ENGINE}_CIRCUIT_BREAKER_THRESHOLD` (default: 3) failures.
  - Auto-recovers after `NDVI_{ENGINE}_CIRCUIT_BREAKER_TIMEOUT_SECS` (default: 300s).
  - Logs state transitions at INFO/WARNING level.
  - Raises `UpstreamFailureError(retryable=False)` when circuit is open.
  - 20 comprehensive unit tests for all state transitions.
  - Prometheus metrics: gauge + transition counter.

- ✅ **All 3 engines protected**
  - `StacClient` → `engine="stac"`
  - `SentinelHubEngine` → `engine="sentinelhub"`
  - `SentinelHubRasterEngine` → `engine="sentinelhub_raster"`

- ✅ **Circuit breakers registered at Django startup**
  - `NdviConfig.ready()` eagerly initializes all 3
  - Engines reuse existing instances (no duplicates)

- ✅ **Celery task respects circuit breaker**
  - `run_ndvi_job` catches `StacUpstreamError` with `retryable=False` and
    marks job as FAILED (no wasted retries).
  - Test: `test_run_ndvi_job_stac_circuit_breaker_persists_across_retries`.

- ✅ **`.env.example` updated** with SentinelHub circuit breaker settings.

- ✅ **Admin endpoint** to manually reset circuit breakers
  - `POST /api/v1/ndvi/circuit-breaker/reset/`

---

## Phase 3 — Observability & Admin Controls

**Status:** ✅ **COMPLETE** (April 12, 2026)

### Step 1: Prometheus Metrics ✅ **COMPLETE**

- ✅ **`ndvi_circuit_breaker_state{engine}`** gauge exported
  - Values: 0 (CLOSED), 1 (OPEN), 2 (HALF_OPEN)
  - Auto-initialized on CircuitBreaker creation
  - Updated on every state transition

- ✅ **`ndvi_circuit_breaker_transitions_total{engine, from_state, to_state}`** counter
  - Increments on CLOSED→OPEN, OPEN→HALF_OPEN, HALF_OPEN→CLOSED, HALF_OPEN→OPEN, manual resets

- ✅ **Grafana dashboard updated** (`weather-apis-observability.json`)
  - Panel 23-25: Stat panels for STAC, SentinelHub, SH Raster circuit breaker state
    - Color-coded: green (CLOSED), red (OPEN), yellow (HALF_OPEN)
  - Panel 26: Time series for state transition rate (5m rate, `or vector(0)` fallback)
  - Panel 27: Time series for upstream request failure rate (5m rate, `or vector(0)` fallback)

### Step 2: Retry-After Header Parsing ✅ **COMPLETE**

- ✅ **`parse_retry_after()`** helper in `retry_policy.py`
  - Supports numeric delay (e.g., `Retry-After: 120`)
  - Supports HTTP-date format (e.g., `Retry-After: Wed, 21 Oct 2026 07:28:00 GMT`)
  - Case-insensitive header lookup
  - Returns `0.0` for past dates, `None` for absent/invalid

- ✅ **`should_retry()`** accepts optional `response_headers`
  - Extracts `Retry-After` delay for 429 responses only
  - Non-429 responses ignore the header
  - Backward compatible: existing callers work without changes

### Step 3: Admin Endpoint ✅ **COMPLETE**

- ✅ **Circuit breaker registry** in `circuit_breaker.py`
  - `register_circuit_breaker(cb)` — registers by engine name
  - `get_circuit_breaker(engine)` — lookup by name
  - `list_circuit_breakers()` — returns full registry

- ✅ **All engines register on init**
  - `StacClient` → `"stac"`
  - `SentinelHubEngine` → `"sentinelhub"`
  - `SentinelHubRasterEngine` → `"sentinelhub_raster"`

- ✅ **`POST /api/v1/ndvi/circuit-breaker/reset/`**
  - Auth: `IsAdminUser` only
  - Request: `{"engine": "stac"|"sentinelhub"|"sentinelhub_raster"}`
  - Response: envelope with `previous_state` and `new_state`
  - OpenAPI fully documented
  - 4 tests (auth, invalid engine, success, noop)

### Step 4: Health Check Endpoint ✅ **COMPLETE**

- ✅ **`GET /api/v1/ndvi/health/upstream/`** — returns status of all upstream services
  - Auth: `IsAuthenticated`
  - Response: envelope with per-engine circuit breaker status
  - Returns all registered engines with: state, failure_count, threshold, timeout
  - OpenAPI fully documented
  - 4 tests (auth, returns all engines, field validation, state reflection)

---

## Verification

- ✅ pre-commit (ruff, ruff format, bandit, mypy) passed.
- ✅ All 572+ tests passed across the full codebase.
- ✅ No regressions in existing functionality.

---

## Recommended Next Steps

**All three phases are fully complete.** No further retry policy work is required unless new requirements emerge.

**Potential future enhancements:**
- Add alerting rules for circuit breaker OPEN state
- Integrate with NDVI Pipeline Phase 2 (Redis Streams) observability
- Consider adding circuit breaker metrics to existing Grafana alerts

---

## Commits

- `2c1c12d` refactor(ndvi): harden retry policy into single source of truth
- `da63e83` docs: add daily report for 2026-04-10 retry policy hardening
- `d739685` docs: add NDVI retry policy implementation status
- `3effcb8` docs: update NDVI status docs with implementation roadmap
- `3d8e104` feat(ndvi): add circuit breakers to SentinelHub engines
- `a0a7d76` feat(ndvi): add Prometheus metrics for circuit breaker state
- `760279e` feat(ndvi): add Retry-After header parsing for 429 responses
- `32b81d7` feat(ndvi): add admin endpoint to reset circuit breakers
- `e8bbb95` fix(ndvi): initialize circuit breakers at Django startup for metrics
- `ec663c4` fix(grafana): show 0 instead of no-data for circuit breaker time series
- `25f7a44` feat(ndvi): add upstream health check endpoint (Phase 3 complete)
- `fd4baf4` fix(ndvi): make farm state GET read-only with cache layer
- `bdca6b7` fix(ndvi): harden farm state cache with stampede protection
- `6e59ed6` fix(ndvi): align test assertions with _safe_error_message() codes

---

## Document History

| Version | Date | Notes |
|---------|------|-------|
| 1.0 | April 10, 2026 | Phase 1 (policy consolidation) complete. |
| 1.1 | April 11, 2026 | Phase 2 (circuit breaker expansion) complete. |
| 1.2 | April 12, 2026 | Phase 3 (observability + admin controls) complete. |
| 1.3 | June 03, 2026 | Re-verified; no drift from code.
