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
| GET | `/api/v1/activities/` | JWT, API key, or Integration JWT | List activities (owner/integration-scoped) | none |
| POST | `/api/v1/activities/` | JWT, API key, or Integration JWT | Create an activity | body: `type`, `scheduled_at`, optional `recurrence_type`, `interval_days`, `farm`, `metadata` |
| GET | `/api/v1/activities/<id>/` | JWT, API key, or Integration JWT | Retrieve an activity | path: `id` |
| PATCH | `/api/v1/activities/<id>/` | JWT, API key, or Integration JWT | Update an activity | path: `id` |
| DELETE | `/api/v1/activities/<id>/` | JWT, API key, or Integration JWT | Delete an activity | path: `id` |

**Integration JWT scope requirements:**
- GET requests require `read`, `write`, or `admin` scope
- POST/PATCH/DELETE require `write` or `admin` scope

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

- Authentication: API key, user JWT, or integration JWT (`FarmObservationAuthentication`)
- Permissions: `IsAuthenticated` with owner/integration scoping
- Integration scope enforcement: `read` scope for GET, `write` scope for POST/PATCH/DELETE
- Integration access: allow-listed per farm via `FarmIntegrationAccess`

## Settings / env vars

None specific to this app.

## Background jobs

- `poll_activities`: Celery Beat task (every minute) for batch activity polling
- `execute_activity`: Celery worker task (5min timeout) for handler execution
- `recover_stale_activities`: Recovery task (every 5 min) for stuck activities
- WebSocket notifications via Django Channels (`ActivityConsumer`)

## Metrics / monitoring

Prometheus metrics (from `activities/metrics.py`):
- `activities_dispatched`: Counter for dispatched activities
- `activity_duration_seconds`: Histogram for execution duration
- `activities_active`: Gauge for currently running activities

## Testing

- Tests live in `activities/tests/test_activities.py`.
- Run: `pytest activities/tests/test_activities.py`

## Implementation phases

| Phase | Focus | Status |
|-------|-------|--------|
| Phase 1 | Core API | ✅ Complete |
| Phase 2 | Scheduler + Redis locks | ✅ Complete |
| Phase 3 | Worker + WebSocket | ✅ Complete |
| Phase 4 | NDVI Integration | ✅ Complete |
| Phase 5 | Production hardening | ✅ Complete |

See: `docs/architecture/activities/01_technical_design.md` for full design.