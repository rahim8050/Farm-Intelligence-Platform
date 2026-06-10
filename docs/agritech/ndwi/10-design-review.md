# NDWI Design Review

**Document:** 10-design-review.md
**Review date:** 2026-06-09
**Documents reviewed:** 00-overview through 10-readiness-review

---

## Executive Summary

The NDWI design is fundamentally sound but contains several instances of **over-engineering and unnecessary abstraction** that add migration risk without commensurate benefit. The most significant issue is the proposed table/model rename (`NdviObservation` → `SpectralObservation`), which is the single highest-risk item in the entire plan yet provides zero functional value.

**Verdict: PROCEED WITH MODIFICATIONS.** The design should be simplified before implementation begins. The following review identifies specific items to change, defer, or keep.

---

## Per-Document Review

### 00-overview.md — Strong

| Aspect | Assessment |
|--------|-----------|
| Strong | Use case table (irrigation/flood/drainage/soil moisture) is well-researched and comprehensive |
| Strong | Scope boundaries clearly separate in-scope from out-of-scope |
| Weak | Assumption #3 ("zero changes to STAC client") contradicts P2 which renames `load_ndvi_array()` — inconsistency |
| Weak | No mention of NDWI/NDVI interaction when a STAC item arrives (should we process all indices?) |

### 01-architecture.md — Over-engineered

| Aspect | Assessment |
|--------|-----------|
| Strong | Component diagram and data flow are clear and accurate |
| Strong | Band mapping table (B03/B08/B3/B5 per engine) is precise |
| Weak | Proposes `run_index_job()` replacing `run_ndvi_job()` — this is full generalization (Option B), not hybrid (Option C). The hybrid should keep `run_ndvi_job` and add `run_ndwi_job` separately |
| Weak | "Renamed to `load_index_array()`" — cosmetic rename of stable NDVI code with no benefit |
| Weak | Integration table says "no changes to existing ndvi/ views" but the component diagram shows a shared view layer — impliction |

### 02-data-model.md — Over-engineered (highest-risk item)

| Aspect | Assessment |
|--------|-----------|
| Strong | Migration plan is thorough with explicit pseudocode and rollback scenarios |
| Strong | Unique constraint design correctly includes `index_type` |
| Weak | **Table rename (`ndvi_ndviobservation` → `ndvi_spectralobservation`) adds R2 risk for zero benefit.** Just add the `index_type` column and keep the old table/model name. |
| Weak | Field rename (`selected_ndvi` → `selected_index_value`) is an API change that breaks NDVI consumers expecting `selected_ndvi`. Keep old field name. |
| Weak | Partitioning section is premature optimization — table is nowhere near 100M rows. Defer entirely. |
| **Fix** | Adding `index_type` to `NdviObservation` (no rename) eliminates R2, R12 complexity, and cuts P1 from 1 week to 2 days |

### 03-api-design.md — Sound

| Aspect | Assessment |
|--------|-----------|
| Strong | Clear endpoint definitions with request parameters and response examples |
| Strong | Backward compatibility table covers all NDVI surfaces |
| Weak | Proposes class-based parameterization (`NdwiTimeseriesView(APIView)` with `index_type` attribute). This creates a new pattern not used by existing NDVI views. Simpler: standalone NDWI view classes that call shared service functions. No base class needed. |
| Weak | Shows `NdwiObservationSerializer` but doesn't clarify whether it's identical to `NdviObservationSerializer` or a copy. If identical, just parameterize the existing serializer with `index_type`. |

### 04-metrics-observability.md — Simplifiable

| Aspect | Assessment |
|--------|-----------|
| Strong | Metric catalog is well-organized and comprehensive |
| Strong | SLO/SLI table provides actionable targets |
| Weak | **Option A (separate `ndwi_*` metrics) was chosen, but Option B (unified `spectral_index_*` with `index` label) is clearly superior.** The stated reason "dashboard migration risk" is unconvincing — a label filter (`index="NDVI"`) preserves existing dashboards with zero changes. |
| **Fix** | Reverse the decision: use `spectral_index_*` with `index` label from day one. This avoids creating 37 duplicate metric definitions and eliminates a future migration. |

### 05-quality-fusion.md — Reasonable but duplicative

| Aspect | Assessment |
|--------|-----------|
| Strong | Rationale for NDWI-specific thresholds is well-reasoned and documented |
| Strong | Water classification (open_water/wet_soil/dry_soil/vegetation) adds real farm ops value |
| Weak | Entire module duplicates NDVI's quality fusion structure with different constants. A `QualityConfig` dataclass per index would eliminate the duplication. |
| Weak | Water classification thresholds (0.20, 0.0, -0.30) are presented as settled but are untested. Add explicit validation requirement against ground truth data. |
| Suggestion | Consider a parameterized quality engine: `QualityConfig(index_type, source_weights, confidence_weights, thresholds)` — one codebase, per-index config objects. Reduces test surface and maintenance. |

### 06-risks.md — Strong, but one risk is self-inflicted

| Aspect | Assessment |
|--------|-----------|
| Strong | Comprehensive (12 risks), well-organized with specific mitigations |
| Strong | Risk response summary table with owners and deadlines is actionable |
| Weak | **R2 (model rename regression) is entirely self-inflicted.** Skipping the rename eliminates this risk and R12 complexity. |
| Weak | R10 (user misunderstanding) mitigation (training sessions) is expensive. Stronger mitigation: document NDWI range semantics in API response `message` field and Swagger. |
| Missing | No risk for quality threshold tuning (these are guesses). R3 covers null rate but not accuracy. |

### 07-phased-delivery-plan.md — Overly granular

| Aspect | Assessment |
|--------|-----------|
| Strong | Clear phase boundaries with acceptance criteria |
| Strong | Resource plan and timeline are realistic |
| Weak | **7 phases for 5-6 weeks is too many.** Merging P4+P5+P6 into a single "Processing Pipeline" phase (1 week) reduces ceremony without losing visibility. |
| Weak | P2 calls for `load_ndvi_array()` → `load_index_array()` — cosmetic rename with risk. Keep `load_ndvi_array`, add `load_ndwi_array`. |
| Weak | Separate Celery queues (`ndwi_ingestion`, etc.) are premature. Share NDVI queues initially with `index_type` task parameter. |
| **Revised estimate** | With simplifications: **4-5 weeks** (down from 5-6) |

### 08-test-strategy.md — Sound

| Aspect | Assessment |
|--------|-----------|
| Strong | Clear coverage targets per layer, specific test counts, good fixtures |
| Strong | Pre-submission checklist covers all tooling |
| Weak | Uses `NdwiPoint` dataclass but design docs haven't defined it — should match `NdviPoint` structure or be an alias |
| Missing | No load/performance test strategy for doubled STAC API traffic |

### 09-adr.md — Strong, but cost estimates need basis

| Aspect | Assessment |
|--------|-----------|
| Strong | Three options clearly described with pros/cons |
| Strong | Trade-off matrix is useful decision-making tool |
| Weak | Cost estimates ($180k/$60k/$80k) appear fabricated — no methodology provided. Remove or add basis. |
| Weak | Does not acknowledge that Option C as described (model rename, metric duplication) is closer to Option B than a true hybrid |

### 10-readiness-review.md — Weak (rubber stamp)

| Aspect | Assessment |
|--------|-----------|
| Weak | "PROCEED" verdict with no critical analysis — reads as a rubber stamp |
| Weak | None of the over-engineering issues identified in this design review are mentioned |
| Weak | No "conditions" or "modifications required" for proceeding |
| **Fix** | Add a "conditions for proceed" section with the simplifications identified in this review |

---

## Top 10 Design Risks

| # | Risk | Source | Severity | Fix |
|---|------|--------|----------|-----|
| 1 | Model rename breaks NDVI in production | 02-data-model.md | High | **Skip rename.** Add `index_type` only. |
| 2 | 37 duplicate metrics double maintenance | 04-metrics-observability.md | Medium | Use unified `spectral_index_*` with `index` label. |
| 3 | Quality thresholds are pure guesses | 05-quality-fusion.md | Medium | Add ground-truth validation requirement before production. |
| 4 | `load_ndvi_array()` rename breaks NDVI | 01-architecture.md | High | Keep name, add `load_ndwi_array()` separately. |
| 5 | `run_ndvi_job` → `run_index_job` generalization creep | 01-architecture.md | Medium | Keep separate tasks; no rename. |
| 6 | Field rename breaks NDVI API consumers | 02-data-model.md | High | Keep `selected_ndvi`, don't rename. |
| 7 | 7-phase overhead delays time to value | 07-phased-delivery-plan.md | Low | Merge P4+P5+P6 into one phase. |
| 8 | Separate Celery queues underutilized | 07-phased-delivery-plan.md | Low | Defer; share NDVI queues initially. |
| 9 | STAC rate limits degrade NDVI + NDWI | 06-risks.md (R9) | Medium | Add explicit rate monitoring in P7. |
| 10 | NDWI + NDVI STAC item race condition | 00-overview.md | Low | Document processing order (deterministic by index_type). |

---

## Top 10 Simplifications

| # | Simplification | Affects | Effort saved | Risk reduced |
|---|---------------|---------|-------------|-------------|
| 1 | **Add `index_type` to `NdviObservation` — skip table/model rename** | 02-data-model.md, P1 | 3 days | Eliminates R2, R12 |
| 2 | **Unified `spectral_index_*` metrics with `index` label** | 04-metrics-observability.md, P7 | 2 days | Eliminates future migration |
| 3 | **Merge P4+P5+P6 into one "Processing Pipeline" phase** | 07-phased-delivery-plan.md | 1 week schedule | Less ceremony |
| 4 | **Co-locate NDWI code in `ndvi/` app (no separate `ndwi/` namespace)** | 01-architecture.md | 1 day | Simpler imports |
| 5 | **Keep `load_ndvi_array` name; add `load_ndwi_array`** | 01-architecture.md, P2 | 1 day | Eliminates NDVI break risk |
| 6 | **Keep `run_ndvi_job` name; add `run_ndwi_job`** | 01-architecture.md, P7 | 1 day | Eliminates NDVI task risk |
| 7 | **Keep field names (`selected_ndvi`)** | 02-data-model.md | 1 day | Eliminates API break risk |
| 8 | **Defer separate Celery queues** | 07-phased-delivery-plan.md, P7 | 2 days | Simpler initial deployment |
| 9 | **Remove partitioning section from design** | 02-data-model.md | — | Cleaner document |
| 10 | **Parameterize quality engine with `QualityConfig`** | 05-quality-fusion.md | 2 days (over NDVI too) | Eliminates duplicate module |

---

## Items to Defer

| Item | Defer to | Reason |
|------|----------|--------|
| Separate Celery queues | When NDWI traffic exceeds 50% of NDVI | Premature optimization |
| Partitioning by `index_type` | >100M rows in SpectralObservation | Premature optimization |
| MODIS NDWI (MCD43A4) | Phase 8+ | Not a blocker; Sentinel-2 covers |
| Per-region outlier thresholds | Future data science sprint | Requires production data |
| Metric unification (`ndwi_*` → `spectral_index_*`) | **Reconsider: do now, not later** | More painful later |
| Model rename (`NdviObservation` → `SpectralObservation`) | **Never** | No benefit, real risk |

---

## Items to Keep (as designed)

| Item | Rationale |
|------|-----------|
| Engine parameterization (`asset_green` param + `NDWI_FORMULA`) | Clean, minimal change per engine class |
| `get_engine(index_type=)` API | Clear contract, prevents cross-contamination |
| NDWI-specific thresholds in quality/fusion | Genuinely index-specific; not shareable |
| Water classification post-processing | Real farm ops value |
| SCL mask reuse | Works identically for NDWI |
| Cache isolation (`ndwi:cache:` prefix) | Prevents cache poisoning |
| Backward-compatible NDVI endpoints | Non-negotiable |
| Phased delivery (consolidated to fewer phases) | Incremental deployability |

---

## Final Recommendation

**PROCEED WITH MODIFICATIONS** — implement the top 10 simplifications before starting code.

### Revised phases (4 phases, 4-5 weeks):

| Phase | Duration | Deliverables |
|-------|----------|-------------|
| **P1: Model + Engine** | 1.5 weeks | Add `index_type` to `NdviObservation` (no rename). Parameterize engines with Green band + NDWI formula. Add `ndwi_*` factories to `ENGINE_FACTORIES`. |
| **P2: API Layer** | 1 week | Standalone NDWI views under `/ndwi/` prefix. Shared service functions. `ndwi:cache:` isolation. |
| **P3: Processing Pipeline** | 1 week | V2 quality with NDWI thresholds. Multi-source fusion. Blue-colormap raster PNG. |
| **P4: Operationalize** | 1 week | `run_ndwi_job` task. `spectral_index_*` metrics with `index` label. Daily Celery Beat schedule. Grafana panels. |

**Total: 4.5 weeks** (down from 5-6), with **zero risk to NDVI production** from model/table renames.

### Required before P1 start:
1. [ ] Confirm `index_type` added to `NdviObservation` — no table rename, no model rename
2. [ ] Confirm unified `spectral_index_*` metrics with `index` label instead of `ndwi_*`
3. [ ] Approve co-location of NDWI code in `ndvi/` app (no separate Django app)
4. [ ] Approve revised 4-phase delivery plan
5. [ ] Schedule ground-truth validation of water classification thresholds
