# NDVI Phase 3 — Multi-Engine Fallback Execution Plan

## Overview

Phase 3 implements the multi-engine fallback system defined in the master architecture spec (`ndvi-system-evolution-phased-spec.md`). It enables the NDVI pipeline to maintain coverage when Sentinel-2 is unavailable or unreliable by falling back to Landsat and MODIS sources with controlled confidence degradation.

**Note:** The existing `ndvi-phase3-execution-plan.md` covers "Data Lifecycle & Batch Processing" (versioning, append-only storage, recompute). That document has been renamed to `ndvi-data-lifecycle-plan.md` to avoid the naming conflict.

## Status: IN PROGRESS

### Architecture Spec Reference

See `docs/architecture/ndvi-system-evolution-phased-spec.md` Section 8 (Phase 3 - Multi-Engine Fallback).

## Guiding Constraints

- Do not break Phase 2 V2 quality engine or its existing behavior.
- Do not change API paths, auth, or response envelopes.
- All fallback decisions must be deterministic and explainable.
- Never blend across sources — select the single best candidate.
- Confidence degradation must apply on fallback selection.
- Source disagreement must produce NULL, not averaged values.

## Phase 1 — Fusion Service

### Goal

Implement the fusion service that gathers candidate V2 observations for a `(farm, bucket_date)` and selects the best one using the deterministic decision tree.

### Work

- Create `ndvi/fusion.py` with:
  - `gather_candidates(farm_id, bucket_date)` → list of V2 candidates
  - `select_best_candidate(candidates)` → selected V2 or None
  - Confidence degradation multipliers: Landsat ×0.90, MODIS ×0.80
  - Conflict rule: top-2 NDVI diff ≥0.10 and neither ≥0.75 → NULL
- Source priority order: Sentinel-2 → Landsat → MODIS

### Exit Criteria

- Fusion service passes all unit tests.
- Deterministic selection for all candidate combinations.
- Confidence degradation applied correctly.
- Conflict rule produces NULL when triggered.

## Phase 2 — Fallback Selector

### Goal

Implement the fallback selector that orchestrates the decision tree with explicit threshold checks.

### Work

- Decision tree:
  1. Score every candidate through V2 quality engine
  2. Discard candidates where confidence < 0.50 or null conditions trigger
  3. If one Sentinel-2 candidate remains and confidence >= 0.75 → select it
  4. Else if one Landsat candidate remains and confidence >= 0.70 → select it
  5. Else if one MODIS candidate remains and confidence >= 0.60 → select it
  6. Else select highest confidence remaining
  7. Tie-break by source priority
  8. No survivor → NULL

### Exit Criteria

- Fallback selector passes all unit tests.
- Each threshold level is tested independently.
- Tie-breaking is deterministic.

## Phase 3 — Engine Adapter Stubs

### Goal

Create Landsat and MODIS engine adapter stubs that conform to the `NDVIEngine` protocol, enabling the fallback system to work even before full upstream integrations are built.

### Work

- Create `ndvi/engines/landsat.py` with `LandsatEngine` stub
- Create `ndvi/engines/modis.py` with `ModisEngine` stub
- Both return empty results by default (no upstream connectivity)
- Register in engine registry/factory

### Exit Criteria

- Engine stubs pass protocol compliance tests.
- Fusion service can process candidates from all three engines.

## Phase 4 — Integration Tests

### Goal

Write end-to-end tests covering the full fallback flow.

### Work

- Test Sentinel-2 → Landsat fallback when S2 confidence < 0.75
- Test Landsat → MODIS fallback when Landsat confidence < 0.70
- Test NULL return when all sources fail
- Test conflict rule (NDVI disagreement)
- Test confidence degradation chain

### Exit Criteria

- All integration tests pass.
- Coverage meets project standards.

## Phase 5 — Documentation Update

### Goal

Resolve the Phase 3 naming conflict and update architecture docs.

### Work

- Rename `ndvi-phase3-execution-plan.md` → `ndvi-data-lifecycle-plan.md`
- Update references in status docs
- Add Phase 3 exit criteria to master spec

### Exit Criteria

- No ambiguous "Phase 3" references remain.
- Master spec is the single source of truth for phase numbering.

## Definition of Done

- [ ] Fusion service implemented and tested
- [ ] Fallback selector implemented and tested
- [ ] Landsat and MODIS engine stubs created
- [ ] Integration tests pass
- [ ] Documentation updated and naming conflict resolved
- [ ] Pre-commit hooks pass
- [ ] Pushed to main

## Next: Phase 4 — Fusion and Intelligence

After Phase 3, Phase 4 will add cross-source disagreement detection, Sentinel-1 context for wet soil, and rule-based fusion.
