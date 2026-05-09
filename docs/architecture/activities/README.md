# Activity Scheduling Architecture

This directory contains the technical design and implementation documentation for the Activity Scheduling + Notification Engine.

## Document Index

| # | Document | Description |
|---|----------|-------------|
| 01 | Technical Design Document (TDD) |
| 02 | Production Hardening Review |
| 03 | WebSocket Implementation Guide |
| 04 | API Specification |

## Architecture Overview

The Activity Engine is an event-driven subsystem for managing:

- Vaccinations
- Fertilizer re-application
- Irrigation
- NDVI-triggered farm operations

### System Flow

```
API (Django + DRF)
    │
    ▼
PostgreSQL (Activity Model)
    │
    ├──▶ Scheduler (Celery Beat) ──▶ Redis Queue ──▶ Worker
    │                                                    │
    │                                                    ▼
    │                                            Django Channels
    │                                                    │
    └─────────────────────────────────────────────────────▶ WebSocket
```

## Key Components

| Component | File Reference |
|-----------|---------------|
| TDD | 01_technical_design.md |
| Hardening Review | 02_hardening_review.md |
| WebSocket Details | 01_technical_design.md Section 8 |

## Quick Links

- [Technical Design Document](./01_technical_design.md)
- [Hardening Review](./02_hardening_review.md)

## Document Relationship

```
ndvi_tdd.md (prompts/)
         │
         ├──▶ 01_technical_design.md (Architecture)
         │         └── Contains complete TDD with all sections
         │
         └──▶ 02_hardening_review.md (Architecture)
                  └── Contains production hardening fixes
```

The TDD (01_technical_design.md) already includes cache strategy in Section 10B.

## Implementation Phases

| Phase | Focus | Status | Document Section |
|-------|-------|--------|------------------|
| Phase 1 | Core API | ✅ IMPLEMENTED | TDD Section 9 + Appendix B,C |
| Phase 2 | Scheduler + Service Layer | ✅ IMPLEMENTED | TDD Section 4, 5, 6 + services.py |
| Phase 3 | WebSocket + Execution + Handlers | ✅ IMPLEMENTED | TDD Section 8, Hardening Review |
| Phase 4 | NDVI Integration | ✅ IMPLEMENTED | TDD Section 13 |

## Implementation Status

**Phase 1: Core API** - ✅ COMPLETE (May 4, 2026)

Files created:
- `activities/models.py` - Activity model (111 lines)
- `activities/serializers.py` - Validation serializers
- `activities/views.py` - ActivityViewSet (CRUD)
- `activities/urls.py` - Router registration
- `activities/tests/test_activities.py` - Test cases
- `activities/migrations/0001_initial.py` - Database migration

API endpoints:
- POST /api/v1/activities/ - Create
- GET /api/v1/activities/ - List
- GET /api/v1/activities/{id}/ - Retrieve
- PATCH /api/v1/activities/{id}/ - Update
- DELETE /api/v1/activities/{id}/ - Delete

**Phase 2: Scheduler** - ✅ COMPLETE (May 5-6, 2026)

Files created:
- `activities/tasks.py` - Celery tasks (poll_activities, execute_activity, recover_stale)
- `activities/services.py` - Service layer with atomic claim and state machine
- `activities/migrations/0002_execution_model.py` - execution_id, execution_started_at, execution_completed_at

Implemented:
- `claim_activity()` - Atomic UPDATE with status=PENDING condition
- `ActivityStateMachine` - Enforces allowed state transitions
- `validate_execution()` - execution_id validation for idempotency
- `poll_activities()` - Scheduler task with batch polling
- `execute_activity()` - Worker task with time_limit=300s
- `recover_stale_activities()` - Recovery task for stuck activities
- `transition_to_running/success/failed/retry()` - State transition helpers

**Phase 3: WebSocket + Handlers** - ✅ COMPLETE (May 6-7, 2026)

Files created:
- `activities/consumers.py` - Django Channels WebSocket consumer
- `activities/handlers/base.py` - HandlerResult dataclass, ActivityHandler base
- `activities/handlers/registry.py` - Handler registry with get_handler()
- `activities/handlers/vaccination.py` - VaccinationHandler
- `activities/handlers/fertilizer.py` - FertilizerHandler
- `activities/handlers/irrigation.py` - IrrigationHandler
- `activities/metrics.py` - Prometheus metrics

Implemented:
- `ActivityConsumer` - WebSocket consumer with user group isolation
- `emit_activity_event()` - Best-effort notification emitter
- Handler registry with DefaultHandler fallback
- Activity handlers for vaccination, fertilizer, irrigation types
- Prometheus metrics: activities_dispatched, activity_duration_seconds, activities_active

**Phase 4: NDVI Integration** - ✅ COMPLETE (May 9, 2026)

Files created:
- `activities/handlers/ndvi_trigger.py` - NdviTriggerHandler

Implemented:
- `NdviTriggerHandler` - Handler that reads farm NDVI state and returns recommendations
- Integrates with `ndvi.farm_state.build_farm_state()` for state classification
- Supports custom action_on_state mapping for state-based follow-up actions
- Default state action mapping: establishment -> fertilizer/irrigation, decline -> irrigation/vaccination
- Graceful error handling when farm state cannot be computed

## WebSocket Details

The WebSocket implementation is implemented in `activities/consumers.py`:

1. **TDD Section 8:** WebSocket Event Schema
   - Django Channels setup - ✅ Implemented
   - Consumer implementation - ✅ Implemented
   - Event payload schema - ✅ Implemented

2. **Hardening Review:** Section 5
   - JWT authentication - ⚠️ Uses AuthMiddlewareStack (no custom JWT)
   - Store-and-forward - ⚠️ Best-effort only (PostgreSQL is authoritative)
   - Notification acknowledgment - ⚠️ Not implemented (client polls REST API)

## Constraints

- No Celery dispatch from GET endpoints
- No blocking lock waits
- Django cache API only
- Response schema unchanged
- UTC everywhere

## Hardening Review Alignment

All critical/high issues from `02_hardening_review.md` have been addressed:

| Issue | Status | Implementation |
|-------|--------|----------------|
| Split-brain locking (1.1) | ✅ Fixed | Atomic dispatch via `claim_activity()` with single UPDATE |
| Check-then-act race (1.2) | ✅ Fixed | Atomic UPDATE with status=PENDING condition |
| No execution timeout (2.1) | ✅ Fixed | `time_limit=300, soft_time_limit=270` on execute_activity |
| Lost activity recovery (2.2) | ✅ Fixed | `recover_stale_activities` task scheduled every 5 min |
| Fire-and-forget WebSocket (3.1) | ✅ Documented | Best-effort only, PostgreSQL is authoritative |
| JWT WebSocket auth (5.1) | ⚠️ Partial | Uses AuthMiddlewareStack (no custom JWT middleware) |
| Dispatch race (7.1) | ✅ Fixed | Atomic claim + execution_id validation |
| Rate limiting (8.1) | ⚠️ Partial | Uses default DRF throttling |
| DB growth/archive (4.1) | ❌ Not implemented | No auto-cleanup for DONE activities |
| Store-and-forward (3.1) | ⚠️ Documented | Best-effort WebSocket, REST API polling for state |

## Cache Strategy

Cache stampede protection is documented in TDD Section 10B:

- Mutex via `cache.add()`
- 6-hour TTL with jitter
- Non-blocking wait (500ms max)
- Stale-but-safe fallback

---

## Document Control

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | May 3, 2026 | opencode | Initial architecture README |
| 1.1 | May 9, 2026 | opencode | Updated implementation status: Phase 1-4 complete, hardening alignment |
| 1.2 | May 9, 2026 | opencode | Phase 4 NDVI Integration complete |