# Accounts app

Back to root: `../README.md`

## Overview

This app provides user authentication and “my profile” endpoints under
`/api/v1/auth/`.

It is not responsible for API key lifecycle management (see `api_keys/`).

## Key concepts / data model

- User model: `django.contrib.auth.models.User` (used directly in this app’s
  code; see `accounts/views.py` and `accounts/serializers.py`).
- Login identifier: `identifier` can be username or email, resolved by
  `accounts.auth_backends.UsernameOrEmailBackend`.

## API surface

Base path: `/api/v1/auth/` (from code: `config/urls.py` and `accounts/urls.py`).

All successful responses from this app use the project envelope produced by
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

Error responses are wrapped by the global exception handler in
`config/api/exceptions.py` and use the same envelope shape with
`status: 1`, `data: null`, and a populated `errors` payload.

| Method | Path | Auth | Purpose | Key params |
| --- | --- | --- | --- | --- |
| POST | `/api/v1/auth/register/` | none | Create user + return tokens | body: `username`, `email`, `password`, `password2` |
| POST | `/api/v1/auth/login/` | none | Authenticate + return tokens | body: `identifier`, `password` |
| POST | `/api/v1/auth/token/refresh/` | none | Refresh access token | body: `refresh` |
| GET | `/api/v1/auth/me/` | JWT or `X-API-Key` | Return current user profile | header: `Authorization` or `X-API-Key` |
| POST | `/api/v1/auth/password/change/` | JWT or `X-API-Key` | Change password | body: `old_password`, `new_password`, `new_password2` |
| POST | `/api/v1/auth/password/reset/` | none | Request password reset email | body: `email` |
| POST | `/api/v1/auth/password/reset/confirm/` | none | Confirm reset + set new password | body: `uid`, `token`, `new_password` |

### Examples

#### Register

```bash
curl -sS -X POST http://localhost:8000/api/v1/auth/register/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"alice","email":"alice@example.com","password":"StrongPass123!","password2":"StrongPass123!"}'
```

Response (success envelope):

```json
{
  "status": 0,
  "message": "Registered successfully",
  "data": { "user": {}, "tokens": { "access": "...", "refresh": "..." } },
  "errors": null,
  "request_id": "req_..."
}
```

#### Login

```bash
curl -sS -X POST http://localhost:8000/api/v1/auth/login/ \
  -H 'Content-Type: application/json' \
  -d '{"identifier":"alice@example.com","password":"StrongPass123!"}'
```

Response:

```json
{
  "status": 0,
  "message": "Login successful",
  "data": { "user": {}, "tokens": { "access": "...", "refresh": "..." } },
  "errors": null,
  "request_id": "req_..."
}
```

#### Token refresh

```bash
curl -sS -X POST http://localhost:8000/api/v1/auth/token/refresh/ \
  -H 'Content-Type: application/json' \
  -d '{"refresh":"..."}'
```

Response:

```json
{
  "status": 0,
  "message": "Token refreshed",
  "data": { "access": "..." },
  "errors": null,
  "request_id": "req_..."
}
```

#### Me

```bash
curl -sS http://localhost:8000/api/v1/auth/me/ \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

Response:

```json
{
  "status": 0,
  "message": "User profile",
  "data": { "id": 123, "username": "alice", "email": "alice@example.com" },
  "errors": null,
  "request_id": "req_..."
}
```

#### Password change

```bash
curl -sS -X POST http://localhost:8000/api/v1/auth/password/change/ \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"old_password":"...","new_password":"...","new_password2":"..."}'
```

Response:

```json
{
  "status": 0,
  "message": "Password changed",
  "data": null,
  "errors": null,
  "request_id": "req_..."
}
```

#### Password reset request

```bash
curl -sS -X POST http://localhost:8000/api/v1/auth/password/reset/ \
  -H 'Content-Type: application/json' \
  -d '{"email":"alice@example.com"}'
```

Response (always 200, generic message):

```json
{
  "status": 0,
  "message": "If an account exists for this email, a reset link has been sent.",
  "data": null,
  "errors": null,
  "request_id": "req_..."
}
```

#### Password reset confirm

```bash
curl -sS -X POST http://localhost:8000/api/v1/auth/password/reset/confirm/ \
  -H 'Content-Type: application/json' \
  -d '{"uid":"<uidb64>","token":"<token>","new_password":"StrongPass123!"}'
```

Success response:

```json
{
  "status": 0,
  "message": "Password has been reset.",
  "data": null,
  "errors": null,
  "request_id": "req_..."
}
```

Invalid/expired token response:

```json
{
  "status": 1,
  "message": "Invalid or expired reset link.",
  "data": null,
  "errors": { "token": ["Invalid or expired token."] },
  "request_id": "req_..."
}
```

## Business logic

- Username/email login resolution: `accounts/auth_backends.py`
- JWT issuance: `_build_tokens` in `accounts/views.py`
- Password reset uses Django `default_token_generator` with `uidb64`, and
  builds links as `"<FRONTEND_RESET_URL>?uid=<uidb64>&token=<token>"`

## AuthZ / permissions

- Register/login/refresh: `AllowAny` (no authentication)
- Me/password change: `IsAuthenticated` (auth from DRF defaults; see
  `config/settings.py`)
- Password reset request/confirm: `AllowAny`

## Throttling scopes

- `password_reset`: `5/min` (from `DEFAULT_THROTTLE_RATES`)
- `password_reset_confirm`: `10/min` (from `DEFAULT_THROTTLE_RATES`)

## Settings / env vars

- `SIMPLE_JWT_ACCESS_MINUTES`, `SIMPLE_JWT_REFRESH_DAYS` (from code:
  `config/settings.py`)
- `DJANGO_SECRET_KEY` (required; loaded in `config/settings.py`)
- `FRONTEND_RESET_URL` (used to build
  `"<FRONTEND_RESET_URL>?uid=<uidb64>&token=<token>"`)
- `DEFAULT_FROM_EMAIL` and standard Django email settings:
  `EMAIL_BACKEND`, `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`,
  `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS`, `EMAIL_USE_SSL`

## Background jobs

None.

## Metrics / monitoring

None emitted directly by this app.

## Testing

- Tests live in `tests/test_accounts.py`.
- Run: `pytest tests/test_accounts.py`
- Password reset tests use the locmem email backend and include a throttle
  regression check.
