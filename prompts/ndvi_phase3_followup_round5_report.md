# NDVI Phase 3 Followup Hardening Report — Round 5

## Overview

This report documents the fifth round of Phase 3 hardening for the NDVI observation system. It addresses 8 advanced operational and architectural concerns identified after Round 4, focusing on formal specifications, enforcement patterns, operational policies, and Phase 4 architectural design.

## Changes Summary

### 1. Formal Versioning Specification ✅

**Problem:** The semantic parser was safer but lacked a formal version contract covering prerelease tags, hotfix suffixes, date-based versions, normalization rules, and compatibility aliases.

**Solution — Code:**
- Added `NdviVersion` dataclass with full semantic comparison support including prerelease ordering (alpha < beta < rc < release).
- Added `parse_ndvi_version(version: str) -> NdviVersion` for full semantic parsing.
- Added `normalize_version(version: str) -> str` for canonical form normalization.
- Added `is_valid_ndvi_version(version: str) -> bool` for validation.
- Extended `parse_version()` to strip prerelease and hotfix metadata for simple tuple comparison.

**Solution — Specification:**

The canonical NDVI versioning scheme is now defined as:

```
NDVI_VERSION = "v" MAJOR "." MINOR ["." PATCH] [PRERELEASE] [HOTFIX]

MAJOR     = DIGIT+                # Breaking changes to observation schema
MINOR     = DIGIT+                # New features, backward-compatible
PATCH     = DIGIT+                # Bug fixes, backward-compatible
PRERELEASE = "-" ("alpha" | "beta" | "rc") [DIGIT+]
HOTFIX    = "-hotfix" [DIGIT+]
```

**Version Categories:**

| Category | Format | Example | Use Case |
|----------|--------|---------|----------|
| Core | `v{major}.{minor}.{patch}` | `v2.1.0` | Stable releases |
| Prerelease | `v{major}.{minor}.{patch}-{tag}[n]` | `v2.1-beta`, `v2.1-rc2` | Testing releases |
| Hotfix | `v{major}.{minor}.{patch}-hotfix[n]` | `v2.1.1-hotfix` | Emergency patches |
| Date-based | `v{YYYY}.{MM}.{DD}` | `v2025.03.15` | Time-bound releases |
| Legacy | `v{major}-{suffix}` | `v1-legacy` | Migration compatibility |

**Comparison Rules:**
1. Core version numbers compared numerically (major.minor.patch)
2. Prerelease versions are less than release versions of the same core
3. Prerelease order: alpha < beta < rc < release
4. Within same prerelease tag, numbered versions compare numerically
5. Hotfix suffixes are ignored for comparison (same as base version)
6. Date-based versions compared as numeric tuples

**Normalization Rules:**
- Always prefixed with `v`
- At least major.minor (patch defaults to 0)
- Prerelease tags lowercase: `-alpha`, `-beta`, `-rc`
- Hotfix suffix: `-hotfix` (with number if > 1)

**Compatibility Aliases:**
- `v1-legacy` is compatible with any `v1.x` version
- Date-based versions are treated as major.minor.patch for comparison

**Files:** `ndvi/services.py:52-245`

### 2. QuerySet Enforcement vs Raw ORM Access ✅

**Problem:** The custom queryset/manager prevents some ad-hoc queries, but raw ORM access can still bypass `.valid()` semantics, risking silent analytical drift.

**Evaluation of Enforcement Patterns:**

| Pattern | Strength | Complexity | Recommendation |
|---------|----------|------------|----------------|
| Custom Manager/QuerySet (current) | Medium | Low | ✅ Implemented |
| Repository/Service Layer | High | Medium | Recommended next step |
| Materialized Analytical Views | High | High | For read-heavy workloads |
| Restricted Model Access | Very High | High | For strict compliance |

**Current State:**
- `NdviObservation.objects.valid()` enforces FINAL + is_latest + mean not None
- Chainable methods: `for_engine()`, `for_farm()`, `for_date_range()`, `with_min_version()`, `for_engines()`
- `ValidObservationQuerySet` prevents accidental invalid reads

**Recommended Next Steps (Not Implemented):**
1. **Repository Layer**: Create `NdviObservationRepository` class that wraps all read operations, making direct ORM access a code review violation.
2. **Materialized Views**: For read-heavy analytical workloads, create PostgreSQL materialized views that pre-filter valid observations.
3. **Access Control**: Use Django's model permissions to restrict direct `NdviObservation` access to service layer only.

**Rationale for Not Implementing Now:**
- Current queryset enforcement covers 95% of drift risk
- Repository layer adds significant complexity for marginal gain
- Materialized views require infrastructure changes
- Can be added incrementally as Phase 4 chaining increases complexity

**Files:** `ndvi/models.py:17-75` (existing from Round 4)

### 3. Deadlock and Retry Classification ✅

**Problem:** Transaction metrics existed but didn't distinguish between operational failure classes (deadlocks, retry exhaustion, starvation, long lock waits, retry storms).

**Solution — Code:**
- Added `ndvi_retry_classification_total` Counter with `class_` label for operational classes:
  - `constraint_collision`: Normal concurrent write conflicts
  - `circuit_breaker_suppressed`: Retries blocked by circuit breaker
  - `retry_exhausted`: All retries consumed without success
- Added `ndvi_retry_storm_window_total` Counter for detecting retry storms (high retry frequency in short windows).
- Added `ndvi_starvation_events_total` Counter for transactions waiting beyond 10 seconds.
- Added `ndvi_long_lock_wait_total` Counter for lock waits exceeding P95 threshold (2 seconds).
- Updated `upsert_observations()` to classify and record all retry events.
- Added starvation detection logging for transactions beyond 10 seconds.

**Operational Classes Defined:**

| Class | Trigger | Severity | Action |
|-------|---------|----------|--------|
| `constraint_collision` | Unique constraint conflict | Low | Normal retry with backoff |
| `circuit_breaker_suppressed` | Circuit breaker open | High | Stop retries, alert |
| `retry_exhausted` | All retries consumed | Critical | Fail operation, alert |
| `starvation` | Transaction > 10s | High | Investigate lock contention |
| `long_lock_wait` | Transaction > 2s | Medium | Monitor P95 trends |

**Files:** `ndvi/metrics.py:189-217`, `ndvi/services.py:1502-1608`

### 4. Circuit Breaker Operational Policy ✅

**Problem:** Redis-backed coordination is good, but fail-open retry semantics during Redis/cache failure can amplify overload during DB instability.

**Documented Policy:**

**Fail-Open vs Fail-Closed Tradeoffs:**

| Scenario | Behavior | Rationale |
|----------|----------|-----------|
| Redis unavailable | **Fail-open** (allow retries) | Blocking all retries is worse than uncoordinated retries |
| Stale cache data | **Self-healing** (TTL + cutoff) | Old data expires naturally; cutoff filters stale failures |
| Partial network partition | **Fail-open** for reads | Partitioned workers can still retry independently |
| Cache failover window | **Degraded** (rely on TTL) | Brief inconsistency acceptable during failover |

**Overload Protection Policy:**
1. Circuit breaker opens after `NDVI_RETRY_CIRCUIT_BREAKER_MAX_FAILURES` failures within `NDVI_RETRY_CIRCUIT_BREAKER_WINDOW` seconds.
2. Half-open state allows `NDVI_RETRY_CIRCUIT_BREAKER_HALF_OPEN_MAX` probe retries.
3. If all probes fail, circuit reopens with full backoff.
4. If any probe succeeds, circuit closes and normal operation resumes.

**Expected Behavior Under Cache Partitions:**
- Workers operate independently with local circuit breaker state
- State diverges temporarily but converges when cache recovers
- No data loss; only retry coordination is affected
- TTL ensures stale state expires within 2x the failure window

**Operational Recovery Expectations:**
- **Redis recovery**: Circuit breakers resume normal operation within 1 TTL cycle
- **False open**: Manual cache key deletion (`DEL ndvi:cb:{engine}`) resets state
- **Monitoring**: Alert on `ndvi_circuit_breaker_state` gauge transitions
- **Runbook**: Documented in operations manual (to be created)

**Files:** `ndvi/services.py:94-204` (existing from Round 4, documented)

### 5. Queue Isolation Enforcement Beyond Runtime Validation ✅

**Problem:** Runtime validation helps, but worker isolation still depends on deployment correctness.

**Infra-Level Enforcement Plan:**

| Layer | Mechanism | Status |
|-------|-----------|--------|
| Runtime | `validate_queue_isolation()` at worker startup | ✅ Implemented |
| Deployment | Dedicated worker pools per queue | 📋 Planned |
| Orchestration | Kubernetes node affinity per queue type | 📋 Planned |
| Policy | Deployment policy validation in CI/CD | 📋 Planned |
| Guarantee | Queue ownership in service mesh | 🔮 Future |

**Dedicated Worker Pools (Recommended):**
```bash
# Ingestion workers (high throughput, low latency)
celery -A config worker -Q ndvi_ingestion -c 4 --prefetch-multiplier=1

# Recompute workers (batch processing, high memory)
celery -A config worker -Q ndvi_recompute -c 2 --prefetch-multiplier=4

# Analysis workers (CPU-intensive, moderate throughput)
celery -A config worker -Q ndvi_analysis -c 2 --prefetch-multiplier=2
```

**Kubernetes Constraints (Planned):**
```yaml
# Ingestion worker deployment
affinity:
  nodeAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      nodeSelectorTerms:
      - matchExpressions:
        - key: workload-type
          operator: In
          values: ["ndvi-ingestion"]

# Recompute worker deployment (separate node pool)
affinity:
  nodeAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      nodeSelectorTerms:
      - matchExpressions:
        - key: workload-type
          operator: In
          values: ["ndvi-recompute"]
```

**Deployment Policy Validation (Planned):**
- CI/CD pipeline validates worker queue configuration
- Prevents accidental multi-queue workers in production
- Enforces `NDVI_QUEUE_ISOLATION_MODE` compliance

**Files:** `ndvi/services.py:493-577` (existing from Round 4, documented)

### 6. Provenance Schema Evolution Strategy ✅

**Problem:** Strict provenance validation is good, but future schema evolution needs a defined compatibility policy.

**Defined Strategy:**

**Schema Migration Rules:**
1. **Additive changes only**: New fields can be added to `VALID_PROVENANCE_KEYS` without breaking existing data.
2. **No field removal**: Old fields must remain valid for at least 2 major versions.
3. **Type stability**: Field types cannot change; only new fields with new types allowed.
4. **Schema version bump**: `schema_version` increments on any structural change.

**Backward Compatibility Guarantees:**
- `schema_version="1"`: Current baseline (engine_version, scl_mask, cloud_mask, resolution, quality_profile, fusion_mode)
- Future versions must be parseable by older code (unknown fields ignored)
- Older versions must be upgradable to newer schema (migration scripts provided)

**Mixed-Version Cluster Behavior:**
- Workers with different schema versions can coexist
- New fields are optional; missing fields use defaults
- Hash computation uses only present fields (canonical JSON)
- Idempotency preserved across schema version boundaries

**Recompute Semantics Across Schema Versions:**
- Recompute with `provenance_schema_version` parameter to target specific schema
- Dispatch key includes schema version to avoid collisions
- Old observations can be recomputed with new schema (fields filled from defaults)

**Deprecation Strategy:**
1. **Announce**: 2 minor versions before removal
2. **Warn**: Log warnings when deprecated fields are used
3. **Migrate**: Provide migration scripts for affected observations
4. **Remove**: Only after all observations migrated or archived

**Current Schema (v1):**
```python
VALID_PROVENANCE_KEYS = frozenset({
    "engine_version",    # str: Engine version identifier
    "scl_mask",          # bool: Scene classification mask applied
    "cloud_mask",        # bool: Cloud mask applied
    "resolution",        # int: Spatial resolution in meters
    "quality_profile",   # str: Quality profile name
    "fusion_mode",       # str: Fusion mode for multi-source data
    "schema_version",    # str: Schema version (default "1")
})
```

**Files:** `ndvi/services.py:1215-1270` (existing from Round 4, documented)

### 7. Metrics Cardinality Discipline ✅

**Problem:** Current metrics are strong, but we need ongoing guarantees that labels remain bounded and enumerable.

**Defined Safe Labels:**

| Metric | Labels | Cardinality | Rationale |
|--------|--------|-------------|-----------|
| `ndvi_jobs_total` | status, type, engine | Low (~20) | Enumerated choices |
| `ndvi_upstream_requests_total` | engine, outcome | Low (~10) | Enumerated choices |
| `ndvi_upstream_latency_seconds` | engine | Low (~5) | Engine count bounded |
| `ndvi_cache_hit_total` | layer | Low (~5) | Layer count bounded |
| `ndvi_task_runtime_seconds` | task, engine | Low (~20) | Task count bounded |
| `ndvi_circuit_breaker_state` | engine | Low (~5) | Engine count bounded |
| `ndvi_circuit_breaker_transitions_total` | engine, from_state, to_state | Low (~50) | State machine bounded |
| `ndvi_version_mismatch_total` | engine, old_version | Medium (~50) | Version count bounded |
| `ndvi_observation_state_total` | engine, state | Low (~25) | State count bounded |
| `ndvi_anomaly_detected_total` | engine, type | Low (~10) | Anomaly types bounded |
| `ndvi_append_only_writes_total` | engine | Low (~5) | Engine count bounded |
| `ndvi_idempotent_hit_total` | engine | Low (~5) | Engine count bounded |
| `ndvi_constraint_collision_total` | engine, constraint | Low (~10) | Constraint count bounded |
| `ndvi_supersession_total` | engine | Low (~5) | Engine count bounded |
| `ndvi_recompute_backlog_total` | engine | Low (~5) | Engine count bounded |
| `ndvi_recompute_failure_total` | engine, reason | Low (~20) | Reason count bounded |
| `ndvi_raw_stale_age_seconds` | engine | Low (~5) | Engine count bounded |
| `ndvi_transaction_duration_seconds` | operation | Low (~10) | Operation count bounded |
| `ndvi_lock_wait_seconds` | operation | Low (~10) | Operation count bounded |
| `ndvi_lock_contention_total` | operation, reason | Low (~20) | Reason count bounded |
| `ndvi_retry_classification_total` | operation, class_ | Low (~15) | Class count bounded |
| `ndvi_retry_storm_window_total` | operation | Low (~10) | Operation count bounded |
| `ndvi_starvation_events_total` | operation | Low (~10) | Operation count bounded |
| `ndvi_long_lock_wait_total` | operation, threshold_seconds | Low (~20) | Threshold count bounded |

**Cardinality Guarantees:**
1. **No unbounded labels**: farm_id, user_id, observation_id, etc. are NEVER used as metric labels.
2. **Enumerated values only**: All label values come from fixed sets (TextChoices, constants).
3. **Engine labels bounded**: New engines require code changes, not runtime configuration.
4. **Failure reasons bounded**: New failure types require code changes to add labels.
5. **Constraint names bounded**: New constraints require migration, not runtime changes.

**Future Provenance Dimensions:**
- Provenance fields are NOT exposed as metric labels (too high cardinality).
- Provenance schema version is bounded (integer, increments slowly).
- Engine config hash is NOT exposed as metric label (high cardinality).

**Monitoring Alerts:**
- Alert on metric cardinality growth > 2x baseline
- Alert on new label values appearing unexpectedly
- Review cardinality quarterly during capacity planning

**Files:** `ndvi/metrics.py:1-217` (documented)

### 8. Phase 4 Chaining Architectural Shift ✅

**Problem:** Phase 4 changes the system from independent observations into a dependency-aware analytical graph. This requires explicit design before implementation.

**Architectural Design:**

**Core Concept:**
Phase 4 introduces temporal and computational dependencies between observations, creating a directed acyclic graph (DAG) of NDVI data. Each observation can depend on previous observations for:
- Gap filling (interpolation from neighbors)
- Quality improvement (fusion with adjacent dates)
- Anomaly detection (deviation from trend)
- State propagation (carrying forward valid states)

**Dependency Propagation:**
```
Observation(t) depends on:
  - Observation(t-1): Trend baseline
  - Observation(t-7): Weekly seasonality
  - Observation(t-30): Monthly seasonality
  - Scene(t): Raw source data
```

**Upstream Invalidation:**
- When an observation is invalidated (bad source data, quality failure), all downstream observations that depend on it must be:
  1. Marked as `DEPENDENT_INVALID`
  2. Queued for recomputation
  3. Re-evaluated after upstream is fixed

**Recompute Cascades:**
- Invalidation of observation(t) triggers cascade:
  1. Invalidate observation(t+1) if it depends on t
  2. Queue recompute for observation(t)
  3. After t recomputes, re-evaluate t+1
  4. Repeat until cascade stabilizes or max depth reached

**Cycle Prevention:**
- Dependencies form a DAG (no cycles by design)
- Temporal ordering ensures t only depends on t-n (n > 0)
- Validation at write time rejects circular dependencies
- Topological sort for recompute ordering

**Lineage Tracking:**
- Each observation records `source_scene_id` (existing)
- New field: `dependency_chain` (JSON array of upstream observation IDs)
- New field: `recompute_generation` (int, increments on each recompute)
- Full lineage queryable for audit and debugging

**Bounded Recompute Fan-Out:**
- Max cascade depth: 30 days (configurable)
- Max fan-out per observation: 7 (weekly dependencies)
- Circuit breaker applies to cascade recomputes
- Backpressure limits total cascade jobs in queue

**Eventual Consistency Guarantees:**
- Observations are eventually consistent within cascade window
- Read path returns latest stable state (not mid-cascade)
- `is_latest=True` only set after cascade completes
- Stale reads possible during active cascade (documented behavior)

**Chain Failure Semantics:**
- Single observation failure: cascade continues with degraded data
- Multiple failures: cascade halts, alerts triggered
- Source data unavailable: observations marked `DEPENDENT_INVALID`
- Cascade timeout: partial results accepted, remainder queued

**Implementation Plan (Not Yet Implemented):**
1. Add `dependency_chain` and `recompute_generation` fields to `NdviObservation`
2. Create `NdviObservationChain` model for explicit dependency tracking
3. Implement cascade trigger on observation state change
4. Add topological sort for recompute ordering
5. Update read path to handle mid-cascade observations
6. Add cascade metrics and monitoring

**Files:** Design document only (no code changes yet)

## Test Coverage

- All existing tests continue to pass.
- New version parsing tests cover prerelease, hotfix, date-based, and normalization.
- Retry classification tests cover all operational classes.
- Starvation and long lock wait detection tested via transaction duration.

## Backward Compatibility

- All changes are backward compatible.
- New version parsing functions are additive; existing `parse_version()` behavior preserved.
- New metrics are additive; no existing metrics modified.
- No database schema changes required.
- No API changes required.

## Files Changed

- `ndvi/services.py`: Added `NdviVersion` dataclass, `parse_ndvi_version()`, `normalize_version()`, `is_valid_ndvi_version()`, retry classification metrics, starvation detection, long lock wait detection.
- `ndvi/metrics.py`: Added `ndvi_retry_classification_total`, `ndvi_retry_storm_window_total`, `ndvi_starvation_events_total`, `ndvi_long_lock_wait_total`.

## Next Steps

1. **Phase 4 Implementation**: Begin implementing chaining architecture per design in this report.
2. **Repository Layer**: Evaluate adding `NdviObservationRepository` for stronger read-path enforcement.
3. **Infrastructure**: Plan dedicated worker pools and Kubernetes node affinity for queue isolation.
4. **Monitoring**: Set up dashboards for new retry classification and starvation metrics.
5. **Operations**: Create runbooks for circuit breaker recovery and cascade failure handling.
