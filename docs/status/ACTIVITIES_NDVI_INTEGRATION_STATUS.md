# Activities NDVI Integration Status

**Date:** May 11, 2026  
**Scope:** `activities/handlers/ndvi_trigger.py` and related activities docs

---

## Summary

`NdviTriggerHandler` is implemented and registered in the Activities subsystem. It reads farm NDVI state through `ndvi.farm_state.build_farm_state()` and returns recommended actions for downstream dispatch.

The handler does not create new activity rows directly. That behavior is intentional and keeps dispatch orchestration outside the handler.

---

## Implemented Behavior

- `NdviTriggerHandler` is available from `activities.handlers`
- The handler accepts `farm_id` from metadata or falls back to `activity.farm_id`
- The handler accepts an optional `action_on_state` mapping in metadata
- Default mappings are:
  - `establishment` -> `fertilizer`, `irrigation`
  - `full_canopy` -> `fertilizer`
  - `decline` -> `irrigation`, `vaccination`
- Missing farm IDs and farm-state lookup failures are handled gracefully
- Duplicate execution is guarded with cache-based idempotency
- `close_old_connections()` is used for Celery compatibility

---

## Code Path

- Handler implementation: `activities/handlers/ndvi_trigger.py`
- Registry export: `activities/handlers/__init__.py`
- Handler lookup: `activities/handlers/registry.py`
- Supporting docs:
  - `activities/README.md`
  - `docs/architecture/activities/README.md`
  - `docs/architecture/activities/01_technical_design.md`

---

## Test Coverage

The handler is covered by the activities test suite in `activities/tests/test_handlers.py`.

Covered cases include:

- handler registration
- missing `farm_id`
- farm-state error handling
- metadata-driven farm selection
- custom action mapping
- default action mapping

---

## Operational Notes

- The handler returns `recommended_actions` in the result metadata.
- Any downstream execution policy should be implemented by the scheduler or a follow-up task.
- The Activities REST API remains authoritative for persisted state.
- Activities hardening is documented in `docs/architecture/activities/02_hardening_review.md` and current app behavior is summarized in `activities/README.md`.
- The current Activities implementation includes scheduler locking, stale recovery, terminal cleanup, a health endpoint, and observability metrics.

---

## Document History

| Version | Date | Notes |
|---------|------|-------|
| 1.0 | May 11, 2026 | Initial status note for implemented NDVI trigger support |
