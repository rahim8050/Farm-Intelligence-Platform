# Activities app

Back to root: `../README.md`

## Overview

This app provides activity scheduling for user-owned farms. It manages time-based and event-triggered farm operations like vaccinations, fertilizer re-application, irrigation, and NDVI-triggered actions.

It is responsible for:
- Activity CRUD endpoints and response envelope
- Activity health probe for readiness checks
- Scheduler polling and dispatch
- Worker execution and state transitions
- WebSocket notifications for activity state changes
- Prometheus metrics for activity execution
- Scheduler leader lock and contention tracking
- Stale execution recovery and terminal cleanup

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
| `status` | CharField | created, pending, dispatched, running, success, failed, retry |
| `scheduled_at` | DateTime | When activity was scheduled |
| `next_due_at` | DateTime | Next execution time |
| `recurrence_type` | CharField | none, interval, cron |
| `interval_days` | PositiveInteger | Days between recurrences |
| `cron_expression` | CharField | 5-field cron expression (e.g. `30 9 * * 1-5`) |
| `metadata` | JSONField | Type-specific data |
| `last_error` | TextField | Error message on failure |
| `retry_count` | PositiveInteger | Number of retries |
| `execution_id` | UUIDField | Idempotency token for worker execution |
| `execution_started_at` | DateTime | When worker execution started |
| `execution_completed_at` | DateTime | When worker execution completed |

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
| POST | `/api/v1/activities/` | JWT, API key, or Integration JWT | Create an activity | body: `type`, `scheduled_at`, optional `recurrence_type`, `interval_days`, `cron_expression`, `farm`, `metadata` |
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
| `ndvi_trigger` | NDVI-triggered action that returns recommended follow-up actions |

### Recurrence Types

| Type | Description |
|------|-------------|
| `none` | One-time activity |
| `interval` | Repeats every `interval_days` |
| `cron` | Repeats according to a 5-field cron expression (`cron_expression`) |

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

#### Create a cron-recurring activity

```bash
curl -sS -X POST http://localhost:8000/api/v1/activities/ \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "irrigation",
    "scheduled_at": "2026-06-01T09:00:00Z",
    "recurrence_type": "cron",
    "cron_expression": "30 9 * * 1-5",
    "farm": 1,
    "metadata": {"zone": "A1"}
  }'
```

Runs weekdays at 09:30 UTC. After each successful execution the activity
is automatically rescheduled to the next matching cron time and set back to
`pending`.

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
- Integration scoping: integration tokens must have `read` for GET and `write` or `admin` for write requests.
- Recurrence computation: `Activity.save()` computes `next_due_at` from `scheduled_at` when unset.
- Validation: Serializer validates `type`, requires `interval_days` for `interval` recurrence, and requires a valid 5-field `cron_expression` for `cron` recurrence.
- Execution flow: `activities.scheduler.poll` claims due activities, `activities.execute` runs handlers, and `activities.recover_stale` resets stuck work.
- NDVI trigger handling: `NdviTriggerHandler` reads farm state and returns recommended actions instead of creating activities directly.

## AuthZ / permissions

- Authentication: API key, user JWT, or integration JWT (`FarmObservationAuthentication`)
- Permissions: `IsAuthenticated` with owner/integration scoping
- Integration scope enforcement: `read` scope for GET, `write` or `admin` for POST/PATCH/DELETE
- Integration access: allow-listed per farm via `FarmIntegrationAccess`

## Settings / env vars

- `ACTIVITY_SCHEDULER_LOCK_SECONDS`: scheduler leader-lock TTL in seconds
- `ACTIVITY_RETENTION_DAYS`: terminal activity retention window

## Background jobs

- `activities.scheduler.poll`: Celery Beat task (every 60 seconds) for batch activity polling
- `activities.recover_stale`: Celery Beat task (every 5 minutes) for stuck activity recovery
- `activities.execute`: Celery worker task with 5 minute hard timeout and 4.5 minute soft timeout
- `activities.cleanup_completed`: Retention task (daily) for old terminal activities
- WebSocket notifications via Django Channels (`ActivityConsumer`)

## Health / observability

- `GET /api/v1/activities/health/`: authenticated health snapshot for the activity engine
- `activities_scheduler_runs_total`: scheduler poll outcome counter
- `activities_scheduler_dispatch_latency_seconds`: scheduler dispatch latency histogram
- `activities_websocket_events_total`: WebSocket event counter
- `activities_websocket_failures_total`: WebSocket delivery failure counter
- `activities_lock_contention_total`: claim / execution contention counter
- Correlation metadata is carried in activity metadata and logged during scheduler, worker, and WebSocket handling

## Metrics / monitoring

Prometheus metrics (from `activities/metrics.py`):
- `activities_dispatched_total`: Counter for dispatched activities, labeled by `type` and `status`
- `activity_duration_seconds`: Histogram for execution duration, labeled by `type`
- `activities_active`: Gauge for currently running activities, labeled by `type`

## Testing

- Tests live in `activities/tests/test_activities.py`.
- Run: `pytest activities/tests/test_activities.py`

## Implementation notes

- The app currently uses the persisted `Activity` row as the source of truth for state transitions.
- Worker execution is idempotent through `execution_id` validation.
- The scheduler uses a cache-based lock to avoid multiple concurrent pollers.
- `activities.cleanup_completed` removes old terminal activities.
- The NDVI trigger handler is implemented, but it returns recommendations for downstream dispatch rather than creating new activity records itself.
- Cron recurrence uses a built-in 5-field parser (standard cron format). After successful execution, `reschedule_recurring()` computes the next matching datetime and transitions the activity back to `PENDING` for the scheduler to pick up.

## Related docs

- [Architecture index](docs/architecture/activities/README.md)
- [Technical design](docs/architecture/activities/01_technical_design.md)
- [Hardening review](docs/architecture/activities/02_hardening_review.md)
- [NDVI integration status](docs/status/ACTIVITIES_NDVI_INTEGRATION_STATUS.md)
