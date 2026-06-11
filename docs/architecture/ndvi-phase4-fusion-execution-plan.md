# NDVI Phase 4 — Fusion and Intelligence Execution Plan

## Overview

Phase 4 adds cross-source disagreement detection, Sentinel-1 context for wet soil and anomaly explanation, and rule-based fusion on top of the Phase 3 multi-engine fallback. Sentinel-1 only affects context and flags — it never produces NDVI values.

**Architecture Reference:** `docs/architecture/ndvi-system-evolution-phased-spec.md` Section 9

## Status: COMPLETE ✅

## Guiding Constraints

- Sentinel-1 must never produce NDVI values (signal-only).
- Source disagreement must set `source_disagreement` flag and may return NULL.
- If one source has higher confidence, use it — no blind averaging.
- Anomaly explanations must be traceable to quality flags.
- Existing Phase 3 decision tree must remain intact.

## Phase 1 — Sentinel-1 Context Module

### Goal

Create a context module that provides Sentinel-1 SAR-derived flags for anomaly explanation.

### Work

- Create `ndvi/sentinel1_context.py`:
  - `Sentinel1Context` dataclass with flags: `wet_soil`, `flooding`, `rough_surface`, `urban_interference`
  - `fetch_sentinel1_context()` queries CDSE STAC API for S1 GRD items and derives flags
  - `merge_s1_context_flags()` to merge context into quality flags
  - `detect_anomaly()` using NDVI value + S1 context

### Exit Criteria

- Context module passes unit tests.
- All flags are prefixed with `s1_` for namespace safety.
- Sentinel-1 context is never used for NDVI selection.
- `fetch_sentinel1_context()` queries CDSE STAC for S1 GRD items using farm bbox and bucket date.
- Context flags are derived from item metadata (polarizations, orbit state, item density).

## Phase 2 — Enhanced Fusion Flags

### Goal

Add Phase 4 quality flags (source_disagreement, fallback_used, anomaly) to FusionResult and propagate them through the selection pipeline.

### Work

- Add `quality_flags` to FusionResult.
- Set `source_disagreement = True` when conflict detected.
- Set `fallback_used = True` when Landsat or MODIS selected.
- Merge Sentinel-1 context flags into final quality flags.
- Add anomaly detection to identify possible_flooding, wet_soil_depression, urban_artifact.

### Exit Criteria

- FusionResult carries quality_flags.
- source_disagreement propagated correctly.
- fallback_used propagated correctly.
- Sentinel-1 context flags merged.

## Phase 3 — Tests

### Goal

Write comprehensive tests for Phase 4.

### Work

- Test Sentinel1Context dataclass and to_flags()
- Test `_classify_s1_items()` for various item combinations
- Test `fetch_sentinel1_context()` with mocked farm bbox and STAC response
- Test merge_s1_context_flags()
- Test detect_anomaly() for flooding, wet soil, urban artifact, no anomaly
- Test FusionResult quality_flags propagation

### Exit Criteria

- All new tests pass.
- Phase 3 tests remain unaffected.
- `fetch_sentinel1_context()` returns empty context for farms without bbox or no S1 items.
- `fetch_sentinel1_context()` returns flagged context when S1 items detected.

## Definition of Done

- [x] Sentinel-1 context module created and tested (`ndvi/sentinel1_context.py`)
- [x] Fusion quality flags enhanced with Phase 4 flags (`source_disagreement`, `fallback_used`, anomaly flags in `ndvi/fusion.py:_build_result_flags`)
- [x] Anomaly detection integrated (`detect_anomaly()` called from `_build_result_flags()`)
- [x] All gates pass (ruff, mypy, bandit) + new tests
- [x] Implementation report completed (this document)
