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
| Phase 2 | Scheduler | ⏳ PENDING | TDD Section 4, 5, 6 |
| Phase 3 | WebSocket + Execution | ⏳ PENDING | TDD Section 8, Hardening Review |
| Phase 4 | NDVI Integration | ⏳ PENDING | TDD Section 13 |
| Phase 5 | Hardening | ⏳ PENDING | Hardening Review |

## Implementation Status

**Phase 1: Core API** - ✅ COMPLETE (May 4, 2026)

Files created:
- `activities/models.py` - Activity model (166 lines)
- `activities/serializers.py` - Validation serializers
- `activities/views.py` - ActivityViewSet (CRUD)
- `activities/urls.py` - Router registration
- `activities/tests.py` - 13 test cases
- `activities/migrations/0001_initial.py` - Database migration

API endpoints:
- POST /api/v1/activities/ - Create
- GET /api/v1/activities/ - List
- GET /api/v1/activities/{id}/ - Retrieve
- PATCH /api/v1/activities/{id}/ - Update
- DELETE /api/v1/activities/{id}/ - Delete

See: `prompts/implementation_status.md` for full details.

## Next Up

**Phase 2: Scheduler** - TBD

## WebSocket Details

The WebSocket implementation is detailed in:

1. **TDD Section 8:** WebSocket Event Schema
   - Django Channels setup
   - Consumer implementation
   - Event payload schema

2. **Hardening Review:** Section 5
   - JWT authentication (CRITICAL)
   - Store-and-forward (CRITICAL)
   - Notification acknowledgment

## Constraints

- No Celery dispatch from GET endpoints
- No blocking lock waits
- Django cache API only
- Response schema unchanged
- UTC everywhere

## Cache Strategy

Cache stampede protection is documented in TDD Section 10B:

- Mutex via `cache.add()`
- 6-hour TTL with jitter
- Non-blocking wait (500ms max)
- Stale-but-safe fallback