# API keys app

Back to root: `../README.md`

## Overview

This app implements first-party API keys that authenticate requests via the
`X-API-Key` header and associate each key with a Django user.

It also provides JWT-only endpoints to create/list/revoke/rotate API keys under
`/api/v1/keys/`. API-key callers cannot manage API keys (from code:
`api_keys/views.py` uses `JWTAuthentication` only).

## Key concepts / data model

Models (from code: `api_keys/models.py`):
- `ApiKey`: stores a user-owned key as a peppered hash plus metadata.
- `ApiKeyScope`: `read`, `write`, `admin` (stored in `ApiKey.scope`).

Storage and security properties:
- Plaintext API keys are generated with prefix `wk_live_…` and stored only as a
  hash in `ApiKey.key_hash` (from code: `api_keys/auth.py` and
  `api_keys/serializers.py`).
- Plaintext is returned once at creation/rotation time via serializer fields
  only; it is not persisted (from code: `api_keys/serializers.py`,
  `api_keys/views.py`).

## API surface

Base path: `/api/v1/keys/` (from code: `config/urls.py` and `api_keys/urls.py`).

All successful responses use the project envelope produced by
`config.api.responses.success_response`:

```json
{
  "status": 0,
  "message": "string",
  "data": {},
  "errors": null,
  "request_id": "req_..."
}
```

| Method | Path | Auth | Purpose | Key params |
| --- | --- | --- | --- | --- |
| GET | `/api/v1/keys/` | JWT | List API keys (metadata only) | none |
| POST | `/api/v1/keys/` | JWT | Create API key (returns plaintext once) | body: `name`, optional `scope`, optional `expires_at` |
| DELETE | `/api/v1/keys/<uuid>/` | JWT | Revoke API key | path: `uuid` |
| POST | `/api/v1/keys/<uuid>/rotate/` | JWT | Rotate API key (returns plaintext once) | path: `uuid`; body: optional overrides |

### Examples

#### List keys

```bash
curl -sS http://localhost:8000/api/v1/keys/ \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

Response:

```json
{
  "status": 0,
  "message": "API keys",
  "data": [{ "id": "..." }],
  "errors": null,
  "request_id": "req_..."
}
```

#### Create key

```bash
curl -sS -X POST http://localhost:8000/api/v1/keys/ \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"My Key","scope":"read","expires_at":null}'
```

Response (plaintext returned once):

```json
{
  "status": 0,
  "message": "API key created",
  "data": { "id": "...", "api_key": "wk_live_..." },
  "errors": null,
  "request_id": "req_..."
}
```

#### Revoke key

```bash
curl -sS -X DELETE http://localhost:8000/api/v1/keys/$KEY_ID/ \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

Response:

```json
{
  "status": 0,
  "message": "API key revoked",
  "data": null,
  "errors": null,
  "request_id": "req_..."
}
```

#### Rotate key

```bash
curl -sS -X POST http://localhost:8000/api/v1/keys/$KEY_ID/rotate/ \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Rotated Key","scope":"write"}'
```

Response (plaintext returned once):

```json
{
  "status": 0,
  "message": "API key rotated",
  "data": { "api_key": "wk_live_..." },
  "errors": null,
  "request_id": "req_..."
}
```

## Business logic

- Key generation: `generate_plaintext_key()` uses `secrets.token_urlsafe` and a
  fixed prefix (from code: `api_keys/auth.py`).
- Peppering + hashing:
  - Pepper comes from `DJANGO_API_KEY_PEPPER`
  - Hash stored via Django password hasher (`make_password`)
  - Verification via `check_password` (from code: `api_keys/auth.py`)
- `last_used_at` tracking:
  - On successful API-key authentication, `ApiKey.last_used_at` is updated at
    most once per 5 minutes per key to reduce write load (from code:
    `api_keys/auth.py`).
- Throttling:
  - `ApiKeyRateThrottle` uses the `throttle` cache and keys by `ApiKey.id`
    (from code: `api_keys/throttling.py`).

Optional utilities:
- `ApiKeyScopePermission` can restrict unsafe methods when the caller is an API
  key (JWT callers pass through unchanged; from code: `api_keys/permissions.py`).

## AuthZ / permissions

- Key lifecycle endpoints: JWT only (`JWTAuthentication`; from code:
  `api_keys/views.py`).
- API key authentication for other endpoints:
  - Authentication class: `api_keys.auth.ApiKeyAuthentication` (exported as
    `api_keys.authentication.ApiKeyAuthentication`; from code:
    `api_keys/auth.py`, `api_keys/authentication.py`)
  - Header: `X-API-Key`

## Settings / env vars

From code: `config/settings.py` and `api_keys/auth.py`:

- `DJANGO_API_KEY_PEPPER` (required for staging/production; used to pepper API keys)
- `API_KEY_THROTTLE_RATE` (DRF throttle rate for `api_key` scope)

## Background jobs

None.

## Metrics / monitoring

No custom Prometheus metrics are emitted directly by this app, but API-key
auth is logged (from code: `api_keys/auth.py`).

## Testing

- Tests live in `tests/test_api_keys.py`.
- Run: `pytest tests/test_api_keys.py`
