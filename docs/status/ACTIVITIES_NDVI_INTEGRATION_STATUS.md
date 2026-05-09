# Activities System - Phase 4: NDVI Integration

**Date:** May 9, 2026
**Architecture Document:** `docs/architecture/activities/01_technical_design.md`
**Implementation:** Phase 4 of Activities system evolution

---

## Executive Summary

Phase 4 introduces `NdviTriggerHandler` to the activities system, enabling automated activity dispatch based on NDVI farm state analysis. The handler reads farm state from the NDVI engine and returns recommended actions, with dispatch handled externally.

---

## What's Implemented

- ✅ **`NdviTriggerHandler` class**
  - Located in `activities/handlers/ndvi_trigger.py`
  - Integrates with `ndvi.farm_state.build_farm_state()`
  - Reads `farm_id` from activity metadata or `activity.farm_id`
  - Reads `action_on_state` mapping from metadata (or uses defaults)
  - Returns `HandlerResult` with recommended_actions

- ✅ **Default state action mapping**
  - `establishment` → `[fertilizer, irrigation]`
  - `full_canopy` → `[fertilizer]`
  - `decline` → `[irrigation, vaccination]`

- ✅ **Error handling**
  - Graceful handling of farm not found (HTTP 404 → warning result)
  - Graceful handling of NDVI service errors (5xx → warning result)
  - `close_old_connections()` call for Celery task compatibility

- ✅ **Handler registration**
  - Exported as `ndvi_trigger` from `activities/handlers/__init__.py`
  - Registered in `activities/handlers/registry.py`

- ✅ **Test coverage**
  - 7 new tests in `activities/tests/test_handlers.py`:
    - `test_handler_type`
    - `test_execute_missing_farm_id`
    - `test_execute_handles_farm_state_error`
    - `test_execute_with_farm_id_from_metadata`
    - `test_execute_with_custom_action_mapping`
    - `test_state_action_mapping_defaults`
    - `test_get_handler_ndvi_trigger`

- ✅ **Documentation updated**
  - `docs/architecture/activities/README.md`: Phase 4 marked complete
  - `docs/architecture/activities/01_technical_design.md`: Checklist updated

---

## Code Quality

| Tool | Status |
|------|--------|
| ruff check | ✅ Pass |
| ruff format | ✅ Pass |
| mypy | ✅ Pass |
| bandit | ✅ Pass |
| pytest (23 handlers tests) | ✅ Pass |

---

## Key Design Decisions

1. **Handler does NOT create activities directly**
   - Returns `recommended_actions` in metadata
   - Dispatch logic handled externally (Celery tasks, etc.)

2. **Farm validation via ORM query**
   - `Farm.objects.get(id=farm_id)` validates farm exists
   - Raises `Farm.DoesNotExist` → caught and returned as warning result

3. **Metadata-driven configuration**
   - `farm_id`: Required, from metadata or fallback to `activity.farm_id`
   - `action_on_state`: Optional mapping to override defaults

4. **Default actions map to existing handlers**
   - `fertilizer` → `FertilizerHandler`
   - `irrigation` → `IrrigationHandler`
   - `vaccination` → `VaccinationHandler`

---

## Files Changed/Added

- `activities/handlers/ndvi_trigger.py` (new)
- `activities/handlers/__init__.py` (updated imports)
- `activities/tests/test_handlers.py` (7 new tests)
- `docs/architecture/activities/README.md` (Phase 4 status)
- `docs/architecture/activities/01_technical_design.md` (checklist updated)
- `docs/status/ACTIVITIES_NDVI_INTEGRATION_STATUS.md` (this file)

---

## Next Steps (if any)

- Commit Phase 4 changes
- Consider adding integration tests for full dispatch flow
- Document dispatch pattern for NDVI-triggered activities