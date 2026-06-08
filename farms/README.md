# Farms app

Back to root: `../README.md`

## Overview

This app manages user-owned `Farm` resources and the spatial metadata used by
other subsystems (notably NDVI bounding boxes).

It does not perform NDVI or weather lookups itself (see `ndvi/` and `weather/`).

## Key concepts / data model

- `farms.models.Farm`: a farm owned by a user, with optional centroid and
  optional bounding box (AOI) for NDVI queries.

Key fields (from code: `farms/models.py`):
- `owner` (FK), `name`, `slug`
- Optional centroid: `centroid_lat`, `centroid_lon`
- Optional AOI bbox: `bbox_south`, `bbox_west`, `bbox_north`, `bbox_east`
- `is_active`, timestamps

- `farms.models.FarmObservation`: log entry for a farm with `observed_at`,
  `event_type`, optional `note` + `metadata`, and creator attribution.
- `farms.models.FarmIntegrationAccess`: allow-list record mapping an
  integration client ID to a farm.

Validation notes:
- Centroid requires both lat and lon (serializer mirrors model validation).
- Bounding box requires all four edges, and must satisfy south < north and
  west < east (from code: `farms/models.py` and `farms/serializers.py`).

## API surface

Base path: `/api/v1/farms/` (from code: `farms/urls.py` and `config/urls.py`).

The farm CRUD endpoints are implemented as a DRF `ModelViewSet` and return
standard DRF serializer JSON (they do not use `success_response`; from code:
`farms/views.py`). The farm observation endpoints below return the standard
`success_response` envelope (from code: `farms/observation_views.py`).

| Method | Path | Auth | Purpose | Key params |
| --- | --- | --- | --- | --- |
| GET | `/api/v1/farms/` | JWT, `X-API-Key`, or integration JWT | List farms (owner-scoped) | none |
| POST | `/api/v1/farms/` | JWT, `X-API-Key`, or integration JWT | Create a farm | body: `name`, optional spatial fields |
| GET | `/api/v1/farms/<id>/` | JWT, `X-API-Key`, or integration JWT | Retrieve a farm (owner-only) | path: `id` |
| PATCH | `/api/v1/farms/<id>/` | JWT, `X-API-Key`, or integration JWT | Update a farm (owner-only) | path: `id` |
| DELETE | `/api/v1/farms/<id>/` | JWT, `X-API-Key`, or integration JWT | Delete a farm (owner-only) | path: `id` |

### Farm observations

These endpoints return the `success_response` envelope and support
JWT, `X-API-Key`, or integration JWT authentication. Integration JWT access
requires the integration client to be allow-listed for the farm via
`farms.models.FarmIntegrationAccess`. Integration JWT scope must include
`read` for GET and `write` for POST/PATCH/DELETE.

| Method | Path | Auth | Purpose | Key params |
| --- | --- | --- | --- | --- |
| GET | `/api/v1/farms/<farm_id>/observations/` | JWT, `X-API-Key`, or integration JWT | List observations (owner-only for user/API key) | path: `farm_id`, optional query: `start`, `end`, `event_type`, `limit`, `offset` |
| POST | `/api/v1/farms/<farm_id>/observations/` | JWT, `X-API-Key`, or integration JWT | Create an observation | body: `observed_at`, `event_type`, optional `note`, optional `metadata` |
| GET | `/api/v1/farms/<farm_id>/observations/<id>/` | JWT, `X-API-Key`, or integration JWT | Retrieve an observation | path: `farm_id`, `id` |
| PATCH | `/api/v1/farms/<farm_id>/observations/<id>/` | JWT, `X-API-Key`, or integration JWT | Update an observation | path: `farm_id`, `id` |
| DELETE | `/api/v1/farms/<farm_id>/observations/<id>/` | JWT, `X-API-Key`, or integration JWT | Delete an observation | path: `farm_id`, `id` |

### Farm activities

These endpoints return the `success_response` envelope and support
JWT, `X-API-Key`, or integration JWT authentication. Integration JWT access
requires the integration client to be allow-listed for the farm via
`farms.models.FarmIntegrationAccess`. Integration JWT scope must include
`read` for GET and `write` for POST/PATCH/DELETE.

| Method | Path | Auth | Purpose | Key params |
| --- | --- | --- | --- | --- |
| GET | `/api/v1/farms/<farm_id>/activities/` | JWT, `X-API-Key`, or integration JWT | List activities | path: `farm_id`, optional query: `status`, `type`, `limit`, `offset` |
| POST | `/api/v1/farms/<farm_id>/activities/` | JWT, `X-API-Key`, or integration JWT | Create an activity | body: `type`, `scheduled_at`, optional `recurrence_type`, `interval_days`, `cron_expression`, `farm`, `metadata` |
| GET | `/api/v1/farms/<farm_id>/activities/<id>/` | JWT, `X-API-Key`, or integration JWT | Retrieve an activity | path: `farm_id`, `id` |
| PATCH | `/api/v1/farms/<farm_id>/activities/<id>/` | JWT, `X-API-Key`, or integration JWT | Update an activity | path: `farm_id`, `id` |
| DELETE | `/api/v1/farms/<farm_id>/activities/<id>/` | JWT, `X-API-Key`, or integration JWT | Delete an activity | path: `farm_id`, `id` |

### Examples

#### List farms

```bash
curl -sS http://localhost:8000/api/v1/farms/ \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

Response (list of farms):

```json
[
  {
    "id": 1,
    "name": "Farm A",
    "slug": "farm-a",
    "bbox_south": "0.0",
    "bbox_west": "0.0",
    "bbox_north": "0.2",
    "bbox_east": "0.2"
  }
]
```

#### Create a farm

```bash
curl -sS -X POST http://localhost:8000/api/v1/farms/ \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Farm A","bbox_south":0.0,"bbox_west":0.0,"bbox_north":0.2,"bbox_east":0.2}'
```

Response (created farm object):

```json
{ "id": 1, "name": "Farm A", "slug": "farm-a", "bbox_south": "0.0" }
```

#### Retrieve/update/delete

```bash
curl -sS http://localhost:8000/api/v1/farms/1/ \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

Response (farm object):

```json
{ "id": 1, "name": "Farm A", "slug": "farm-a" }
```

## Business logic

- Owner scoping: `FarmViewSet.get_queryset()` returns only the current user’s
  farms (from code: `farms/views.py`).
- Owner enforcement on create: `perform_create()` always sets `owner` from the
  authenticated user and ignores any client-supplied owner (from code:
  `farms/views.py`).
- Spatial validation is performed in both model `clean()` and serializer
  `validate()` (from code: `farms/models.py`, `farms/serializers.py`).

## AuthZ / permissions

- Authentication: DRF defaults (JWT or API key; from code: `config/settings.py`)
- Permissions:
  - `IsAuthenticated`
  - `IsFarmOwner` object permission for retrieve/update/delete (from code:
    `farms/views.py`, `farms/permissions.py`)

## Settings / env vars

None specific to this app.

## Background jobs

None.

## Metrics / monitoring

None emitted directly by this app.

## Testing

- Tests live in `farms/tests/test_farms_api.py`.
- Run: `pytest farms/tests/test_farms_api.py`
