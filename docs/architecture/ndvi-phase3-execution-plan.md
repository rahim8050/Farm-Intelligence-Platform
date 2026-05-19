# NDVI Phase 3 — Data Lifecycle & Batch Processing Execution Plan

## Overview

Phase 3 converts NDVI storage from overwrite-based persistence into a versioned, lifecycle-aware, batch-reprocessable system while preserving the existing Phase 2 execution model.

This plan keeps the current dispatch boundary intact:

- Redis Streams remains the optional transport layer for job dispatch.
- Celery remains the execution engine.
- `dispatch_ndvi_job()` remains the single dispatch boundary used by the API and batch jobs.
- Existing API paths, worker entry points, and Phase 2 queue behavior remain operational during rollout.

The implementation must be backward compatible while data is migrated and batch recompute is introduced.

## Status: COMPLETE ✅

All phases (1–9) plus 5 rounds of followup hardening have been implemented and deployed.

### Completed Rounds

| Round | Date | Scope | Commits |
|-------|------|-------|---------|
| Phase 1–9 Core | 2026-05-18 | Storage foundation through rollout strategy | `1fd0f705`–`7e829262` |
| Followup Round 1 | 2026-05-18 | Distributed systems guarantees (circuit breaker, queue isolation) | `20d7bd7e` |
| Followup Round 2 | 2026-05-18 | Operational hardening (provenance, state transitions, validity) | `06d7d2bc`, `8f205618` |
| Followup Round 3 | 2026-05-18 | Semantic versioning, queue isolation modes, fail-closed policy | `2e42547c` |
| Followup Round 4 | 2026-05-18 | Circuit breaker, queue isolation, version-aware validity, recompute | `7e829262` |
| Followup Round 5 | 2026-05-19 | Formal version spec, retry classification, Phase 4 design | `e1f2e430` |
| CI Fixes | 2026-05-19 | Type ignore fix, coverage tests | `6651070c`, `268b67d9`, `c1f316a6`, `073d14e1` |

### Total Impact

- **Files changed:** 30+
- **Lines added:** ~5,000+
- **Tests added:** 400+
- **New metrics:** 7 Prometheus metrics
- **New models/fields:** 10+ lifecycle fields on `NdviObservation`

---

## Guiding Constraints

- Do not break Phase 2 job dispatch or worker execution.
- Do not change API paths, auth, or response envelopes during the storage rollout.
- Do not perform destructive migrations that discard historical NDVI data.
- Do not remove the current write path until append-only persistence is verified.
- Do not expose intermediate or partial data as final API output.
- Make rollback possible through code path switch or configuration toggle.
- Keep all changes idempotent, retry-aware, and observable.

## Phase 1 — Storage Foundation

### Goal

Introduce lifecycle and version fields to NDVI storage without changing runtime behavior.

### Work

- Extend `NdviObservation` with the following fields:
  - `version: CharField`
  - `state: CharField`
  - `is_latest: BooleanField`
  - `computed_at: DateTimeField`
- Apply a non-destructive migration that preserves all existing rows.
- Set migration defaults for existing data:
  - `version = "v1-legacy"`
  - `state = "FINAL"`
  - `is_latest = True`
  - `computed_at = created_at` when available, otherwise `timezone.now()`
- Preserve the current unique constraint during this phase.
- Do not change `upsert_observations()` write semantics yet.
- Add read-only support in serializers and API responses for the new fields if they are already present in the model.

### Exit Criteria

- Migration runs without data loss.
- Existing API behavior remains unchanged.
- All existing observations contain `version`, `state`, `is_latest`, and `computed_at`.
- No write-path code changes are required to deploy this phase.

## Phase 2 — Append-Only Write Semantics

### Goal

Stop destructive overwrites and preserve NDVI history.

### Work

- Replace `update_or_create()` behavior in `upsert_observations()` with append-only insertion logic.
- When writing a new observation:
  - mark the previous latest row for the same `(farm, engine, bucket_date)` as `is_latest=False`
  - insert a new row instead of updating the old row
- Keep a deterministic idempotency guard based on one of:
  - `request_hash`
  - `(job_id, version)`
- Maintain compatibility with the existing `dispatch_ndvi_job()` and worker flow.
- Ensure repeated execution of the same job does not create duplicate latest rows.
- Keep old rows queryable for audit and recompute.

### Exit Criteria

- Re-running the same NDVI job creates a new stored row instead of overwriting the prior row.
- Historical NDVI values remain queryable.
- Only one row per `(farm, engine, bucket_date)` has `is_latest=True`.
- Retry paths do not create duplicate latest rows.

## Phase 3 — Versioning Enforcement

### Goal

Make NDVI computation explicitly versioned and deterministic per release.

### Work

- Introduce a constant such as:
  - `NDVI_VERSION = "v2.1-cloud-mask"`
- Attach the version to all new writes from:
  - `run_ndvi_job()`
  - observation creation logic
- Enforce version propagation through the write path so no NDVI row is written without a version.
- Make the version deterministic for a release and stable across retries for the same job execution.
- Use the version to distinguish old and new NDVI logic during recompute and rollout.

### Exit Criteria

- All new NDVI rows carry an explicit version.
- The system can distinguish legacy rows from current rows.
- New writes do not mix versions within the same execution path.
- Repeated retries preserve the same version value.

## Phase 4 — Lifecycle State Introduction

### Goal

Track NDVI data quality and processing stage explicitly.

### Work

- Define lifecycle states with a minimal set:
  - `RAW`
  - `FINAL`
- Set write-path behavior so:
  - `RAW` represents direct engine output before final acceptance, if stored
  - `FINAL` represents data that passed cloud and quality checks
- Update API reads and internal data selection to filter on:
  - `is_latest=True`
  - `state="FINAL"`
- Keep intermediate rows available for audit if they are persisted, but do not expose them as default API data.

### Exit Criteria

- Lifecycle states exist in the database.
- API responses only return `FINAL` data.
- RAW or intermediate rows are not exposed by default endpoints.
- The lifecycle value is deterministic and visible in stored rows.

## Phase 5 — Batch Recompute Engine

### Goal

Enable safe recomputation using the existing dispatch and worker pipeline.

### Work

- Add version-aware recompute logic that triggers when:
  - `observation.version != NDVI_VERSION`
- Update batch jobs so they check version mismatch before dispatching work.
- Keep the existing scheduled jobs intact:
  - daily refresh
  - daily farm-state coverage
  - weekly gap fill
- Add an explicit recompute path for a bounded historical window, such as:
  - “recompute last N days”
- Use `dispatch_ndvi_job()` for recompute work so the transport boundary stays unchanged.
- Ensure batch jobs only dispatch missing or stale windows and do not recompute rows already at the current version unless explicitly requested.

### Exit Criteria

- Historical NDVI can be recomputed safely.
- Old versions remain intact.
- Batch jobs are version-aware.
- Recompute dispatch is explicit and bounded.

## Phase 6 — Data Correctness Hardening

### Goal

Prevent silent bad data propagation into final NDVI outputs.

### Work

- Change cloud handling so `cloud_fraction=None` is not treated as clean final data.
- Apply a deterministic rule for unknown cloud quality:
  - reject for `FINAL`, or
  - keep only as `RAW`
- Enforce quality thresholds before a row can transition to `FINAL`.
- Keep the existing cloud filter behavior compatible where possible, but make the final-state decision explicit.
- Preserve the current upstream ingestion and dispatch architecture while tightening final-state acceptance.

### Exit Criteria

- Unknown-quality data does not enter `FINAL` state.
- Cloud handling is deterministic.
- Final-state acceptance rules are explicit and repeatable.

## Phase 7 — Observability Upgrade

### Goal

Make NDVI data health, version drift, and recompute activity measurable.

### Work

- Extend `ndvi/metrics.py` with:
  - `ndvi_observation_latest_age_seconds`
  - `ndvi_version_mismatch_total`
  - `ndvi_recompute_trigger_total`
  - `ndvi_final_coverage_ratio` if coverage reporting is needed
- Emit metrics from the write path, recompute path, and API read path.
- Add logs that identify:
  - `farm_id`
  - `engine`
  - `bucket_date`
  - `version`
  - `state`
  - `is_latest`
  - `request_hash`
  - `job_id`
- Track freshness and drift using the same existing observability stack.

### Exit Criteria

- Stale data can be detected.
- Recompute activity is visible.
- Version drift is measurable.
- Final coverage health can be monitored if enabled.

## Phase 8 — API Contract Update

### Goal

Expose lifecycle-aware NDVI data without breaking existing clients.

### Work

- Update serializers to include:
  - `value`
  - `version`
  - `state`
  - `is_latest`
  - `computed_at`
- Apply read filters so API responses only use:
  - `is_latest=True`
  - `state="FINAL"`
- Preserve existing response fields for backward compatibility.
- Add new fields without removing current fields.
- Keep `dispatch_ndvi_job()` as the only path for enqueuing refresh or recompute work.

### Exit Criteria

- API returns versioned NDVI rows.
- Consumers can understand data freshness and lifecycle state.
- Existing client contracts remain compatible.

## Phase 9 — Rollout Strategy

### Goal

Roll out Phase 3 safely without corrupting historical NDVI data.

### Work

1. Deploy Phase 1 migration only.
2. Deploy Phase 2 append-only writes behind a code path switch if needed.
3. Deploy Phase 3 and Phase 4 together only after storage compatibility is confirmed.
4. Enable Phase 5 recompute logic after versioned writes are stable.
5. Tighten Phase 6 data correctness rules after recompute is operational.
6. Enable Phase 7 observability changes alongside recompute and validation.
7. Deploy Phase 8 API changes only after lifecycle-aware reads are stable.

### Rollback Plan

- Revert to the previous write path if append-only behavior causes instability.
- Disable recompute triggers if versioning or batch reruns produce incorrect output.
- Keep all historical rows intact so rollback does not require data restoration.
- Use a configuration switch or code-path switch rather than destructive schema changes.

### Exit Criteria

- The rollout can be reversed without data loss.
- Existing NDVI data remains intact throughout deployment.
- Phase 3 can be enabled incrementally without breaking Phase 2 execution.

## Definition of Done

Phase 3 is complete when:

- [x] NDVI storage is append-only.
- [x] Versioning is enforced on all new writes.
- [x] Lifecycle states are stored and respected.
- [x] Batch recompute is operational and version-aware.
- [x] API responses are lifecycle-aware without breaking existing clients.
- [x] Data correctness rules prevent invalid final-state rows.
- [x] Observability covers freshness, version drift, and recompute activity.
- [x] Circuit breaker with Redis-backed coordination is operational.
- [x] Queue isolation with multiple modes (single, subset, all) is enforced.
- [x] Semantic version parsing supports prerelease, hotfix, and date-based versions.
- [x] Provenance hashing rejects unsupported types (no silent coercion).
- [x] Retry classification distinguishes deadlock, exhaustion, starvation, and storms.
- [x] Phase 4 chaining architecture is designed (DAG, cascades, lineage, cycle prevention).

## Final Outcome

After Phase 3:

- NDVI history is preserved.
- Recomputed values do not overwrite prior results.
- The system can distinguish legacy and current NDVI outputs.
- Lifecycle state is explicit and queryable.
- Batch processing becomes safe to rerun and safe to audit.
- Circuit breaker prevents retry storms during instability.
- Queue isolation prevents workload mixing across queue types.
- Version parsing correctly handles prerelease, hotfix, and date-based versions.
- Provenance hashing is strict and deterministic.
- Retry metrics provide operational visibility into lock contention and starvation.
- Phase 4 chaining design is ready for implementation.

## Next: Phase 4 — NDVI Chaining

Phase 4 will transform the system from independent observations into a dependency-aware analytical graph. Key design decisions are documented in `prompts/ndvi_phase3_followup_round5_report.md` (Section 8).

### Planned Work

1. Add `dependency_chain` and `recompute_generation` fields to `NdviObservation`
2. Create `NdviObservationChain` model for explicit dependency tracking
3. Implement cascade trigger on observation state change
4. Add topological sort for recompute ordering
5. Update read path to handle mid-cascade observations
6. Add cascade metrics and monitoring

