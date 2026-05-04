# Activities app

Back to root: `../README.md`

## Overview

This app provides activity scheduling for user-owned farms. It manages time-based and event-triggered farm operations like vaccinations, fertilizer re-application, irrigation, and NDVI-triggered actions.

It is responsible for:
- Activity CRUD endpoints and response envelope
- Scheduling and dispatch (phase 2+)
- Handler execution (phase 3+)

It is not responsible for:
- Farm ownership and bounding box persistence (see `farms/`)
- Authentication primitives (see `accounts/` and `api_keys/`)

## Key concepts / data model

Models (from code: `activities/models.py`):

- `Activity`: a scheduled activity for a farm with recurrence support.

Key fields:
| Field | Type | Description |
|-------|------|-------------|
| `owner` | FK | User who owns this activity |
| `farm` | FK | Farm (optional) |
| `type` | CharField | vaccination, fertilizer, irrigation, ndvi_trigger |
| `status` | CharField | created, pending, running, done, failed |
| `scheduled_at` | DateTime | When activity was scheduled |
| `next_due_at` | DateTime | Next execution time |
| `recurrence_type` | CharField | none, interval, cron |
| `interval_days` | PositiveInteger | Days between recurrences |
| `metadata` | JSONField | Type-specific data |
| `last_error` | TextField | Error message on failure |
| `retry_count` | PositiveInteger | Number of retries |

## API surface

Base path: `/api/v1/activities/` (from code: `activities/urls.py` and `config/urls.py`).

All successful JSON responses use the project envelope produced by
`config.api.responses.success_response`:

```json
{ "status": 0, "message": "string", "data": {}, "errors": null }
```

| Method | Path | Auth | Purpose | Key params |
|--------|------|------|--------|------------|
| GET | `/api/v1/activities/` | JWT or `X-API-Key` | List activities (owner-scoped) | none |
| POST | `/api/v1/activities/` | JWT or `X-API-Key` | Create an activity | body: `type`, `scheduled_at`, optional `recurrence_type`, `interval_days`, `farm`, `metadata` |
| GET | `/api/v1/activities/<id>/` | JWT or `X-API-Key` | Retrieve an activity | path: `id` |
| PATCH | `/api/v1/activities/<id>/` | JWT or `X-API-Key` | Update an activity | path: `id` |
| DELETE | `/api/v1/activities/<id>/` | JWT or `X-API-Key` | Delete an activity | path: `id` |

### Activity Types

| Type | Description |
|------|-------------|
| `vaccination` | Vaccination schedule |
| `fertilizer` | Fertilizer re-application |
| `irrigation` | Irrigation activity |
| `ndvi_trigger` | NDVI-triggered action (phase 4) |

### Recurrence Types

| Type | Description |
|------|-------------|
| `none` | One-time activity |
| `interval` | Repeats every `interval_days` |

### Examples

#### Create an activity

```bash
curl -sS -X POST http://localhost:8000/api/v1/activities/ \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "vaccination",
    "scheduled_at": "2026-06-01T09:00:00Z",
    "farm": 1,
    "metadata": {"cattle_id": 123}
  }'
```

Response:

```json
{
  "status": 0,
  "message": "OK",
  "data": {
    "id": 1,
    "type": "vaccination",
    "status": "created",
    "scheduled_at": "2026-06-01T09:00:00+00:00",
    "next_due_at": "2026-06-01T09:00:00+00:00",
    "farm": 1,
    "metadata": {"cattle_id": 123}
  },
  "errors": null
}
```

#### Create a recurring activity

```bash
curl -sS -X POST http://localhost:8000/api/v1/activities/ \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "fertilizer",
    "scheduled_at": "2026-06-01T09:00:00Z",
    "recurrence_type": "interval",
    "interval_days": 30,
    "farm": 1,
    "metadata": {"amount_kg": 50}
  }'
```

#### List activities

```bash
curl -sS http://localhost:8000/api/v1/activities/ \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

Response:

```json
{
  "status": 0,
  "message": "OK",
  "data": [
    {
      "id": 1,
      "type": "vaccination",
      "status": "created",
      "scheduled_at": "2026-06-01T09:00:00+00:00"
    }
  ],
  "errors": null
}
```

## Business logic

- Owner scoping: `ActivityViewSet.get_queryset()` returns only the current user's activities.
- Recurrence computation: `Activity.save()` computes `next_due_at` for interval recurrence.
- Validation: Serializer validates `type` and requires `interval_days` for `interval` recurrence.

## AuthZ / permissions

- Authentication: DRF defaults (JWT or API key)
- Permissions: `IsAuthenticated` (from code: `activities/views.py`)

## Settings / env vars

None specific to this app.

## Background jobs

- Phase 2: Celery Beat poll task (scheduled in phase 2)
- Phase 2: Redis lock implementation (scheduled in phase 2)
- Phase 3: Handler execution via Celery worker

## Metrics / monitoring

None emitted directly by this app (phase 3+).

## Testing

- Tests live in `activities/tests/test_activities.py`.
- Run: `pytest activities/tests/test_activities.py`

## Implementation phases

| Phase | Focus | Status |
|-------|-------|--------|
| Phase 1 | Core API | ✅ Complete |
| Phase 2 | Scheduler + Redis locks | Pending |
| Phase 3 | Worker + WebSocket | Pending |
| Phase 4 | NDVI Integration | Pending |
| Phase 5 | Production hardening | Pending |

See: `docs/architecture/activities/01_technical_design.md` for full design.