# Nextcloud Integration Security (HMAC)

This document defines the HMAC request-signing contract for Nextcloud
instance → `farm-intelligence-platform` calls.

It provides:
- Request integrity (method/path/query/body are signed).
- Replay resistance (timestamp window + nonce cache).
- Instance identity (`client_id` identifies the Nextcloud instance).

This is an **additional layer** that composes with existing JWT and API key
authentication; it does not replace them.

Unified contract reference: `docs/integration_auth.md`.

## Contract

### Required headers

- `X-Client-Id`: preferred client identifier (case-sensitive)
- `X-NC-CLIENT-ID`: legacy alias (still accepted)
- `X-NC-TIMESTAMP`: unix seconds (integer)
- `X-NC-NONCE`: unique random string/UUID per request
- `X-NC-SIGNATURE`: hex HMAC-SHA256 over the canonical string

### Canonical string

The canonical string is built as a newline-separated string, then UTF-8 encoded
to bytes for signing.

`METHOD\nPATH\nCANONICAL_QUERY\nTIMESTAMP\nNONCE\nBODY_SHA256`

Canonical string rules:
- Encoding: UTF-8 bytes.
- Newlines: LF (`\n`, byte `0x0A`) only (never CRLF).
- No trailing newline after `BODY_SHA256`.

Where:
- `METHOD` = `request.method.upper()`
- `PATH` = `request.path` (no scheme/host; trailing slash matters)
- `CANONICAL_QUERY`:
  - parse the raw query string (no leading `?`), preserving duplicates and blank
    values
  - decode using HTML form semantics:
    - percent-decode (`%XX`)
    - treat `+` as space
  - re-encode using RFC3986 (spaces become `%20`, never `+`) with safe characters
    `-_.~`
  - sort by `(encoded_key, encoded_value)` (ASCII) for cross-language stability
  - re-join as `k=v&k=v...` (no leading `?`)
- `TIMESTAMP` = unix seconds (as a string)
- `NONCE` = header value exactly as received
- `BODY_SHA256`:
  - `sha256` hex of raw `request.body` bytes
  - for `GET`, hash the empty byte string (`b""`)

### Signature

Compute:
- `signature = HMAC_SHA256_HEX(secret, canonical_string_utf8_bytes)`

Comparison uses constant-time checks (`django.utils.crypto.constant_time_compare`).

Signature normalization:
- `X-NC-SIGNATURE` is hex HMAC-SHA256; clients should prefer lowercase.
- Verifiers should accept hex case-insensitively by normalizing before compare.

## Replay protection

Replay protection uses Django cache `add()`:
- Cache key: `nc_hmac:{client_id}:{nonce}`
- TTL: `NEXTCLOUD_HMAC_NONCE_TTL_SECONDS` (default `360`)

Rules:
- Reject if the nonce cache key already exists within TTL.
- Reject if `abs(now - timestamp)` exceeds `NEXTCLOUD_HMAC_MAX_SKEW_SECONDS`
  (default `300`).

Production note:
- Use a shared cache backend (Redis/Memcached) in production. `LocMemCache` is
  process-local and cannot provide replay protection across multiple workers.

TTL vs skew recommendation:
- Prefer `NEXTCLOUD_HMAC_NONCE_TTL_SECONDS >= NEXTCLOUD_HMAC_MAX_SKEW_SECONDS + 60`
  (or `2x` skew) so delayed requests cannot bypass replay checks at the edge of
  the timestamp window.

## Configuration (env)

All configuration is environment-driven (from code: `config/settings.py`):

- `NEXTCLOUD_HMAC_ENABLED` (default `True`)
- `NEXTCLOUD_HMAC_MAX_SKEW_SECONDS` (default `300`)
- `NEXTCLOUD_HMAC_NONCE_TTL_SECONDS` (default `360`)
- `NEXTCLOUD_HMAC_CACHE_ALIAS` (default `default`)
- `INTEGRATION_HMAC_CLIENTS_JSON`:
  - stringified JSON mapping `client_id -> secret_b64`
  - `secret_b64` must be strict base64; decoded bytes are used for HMAC
  - example (placeholders only):
    - `{"<uuid-client-id>":"<base64-secret>"}`
- `INTEGRATION_LEGACY_CONFIG_ALLOWED` (default `False`):
  - when `False`, presence of legacy `NEXTCLOUD_HMAC_CLIENTS_JSON` causes a
    hard failure until removed

Never commit shared secrets; set them via environment variables or a secrets
manager.

## Known-good example (test vector)

This vector is intended to be deterministic across Python and PHP and is
validated by tests in this repo (`tests/test_integrations_nextcloud_hmac.py`).

Inputs:
- Secret (decoded): `test-shared-secret`
- `INTEGRATION_HMAC_CLIENTS_JSON` value: `{"<client-id>":"dGVzdC1zaGFyZWQtc2VjcmV0"}`
- METHOD: `GET`
- PATH: `/api/v1/integrations/nextcloud/ping/`
- Raw query: `a=2&b=two%20words&plus=%2B&a=1`
- TIMESTAMP: `1766666666`
- NONCE: `550e8400-e29b-41d4-a716-446655440000`
- BODY: empty bytes

Derived values:
- Canonical query: `a=1&a=2&b=two%20words&plus=%2B`
- BODY_SHA256(empty): `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`

Canonical string (LF newlines, no trailing newline):

```text
GET
/api/v1/integrations/nextcloud/ping/
a=1&a=2&b=two%20words&plus=%2B
1766666666
550e8400-e29b-41d4-a716-446655440000
e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
```

Expected signature (hex HMAC-SHA256 over UTF-8 canonical string bytes):
`60a6b6568842ac371ba78655d6788e841d61b251dc75157d0dfe4a39f57cc362`

## Endpoints

### Ping (HMAC-only)

- `GET /api/v1/integrations/nextcloud/ping/`
- Authentication: none (no JWT/API key)
- Permissions: `integrations.permissions.NextcloudHMACPermission`

Success response (project envelope):

```json
{
  "status": 0,
  "message": "OK",
  "data": { "ok": true, "client_id": "nc-dev-1" },
  "errors": null
}
```

Failure response:
- `403 Forbidden` with a non-sensitive message like `"Invalid Nextcloud signature"`.

### Integration token bootstrap (API key + HMAC)

- `POST /api/v1/integrations/token/`
- Authentication: `X-API-Key` plus HMAC signature headers.
- Required headers:
  - `X-API-Key`: plaintext API key (service account)
  - `X-Client-Id`: integration client UUID
  - `X-Timestamp`: unix seconds (integer)
  - `X-Nonce`: unique per request
  - `X-Signature`: hex HMAC-SHA256 of the canonical string
- Canonical string: same format as above.

Success response (project envelope):

```json
{
  "status": 0,
  "message": "OK",
  "data": {
    "access": "<jwt>",
    "token_type": "Bearer",
    "expires_in": 300
  },
  "errors": null
}
```

Environment variables (from code: `config/settings.py`):
- `INTEGRATION_JWT_ACCESS_MINUTES` (default `5`)
- `SIMPLE_JWT_ISSUER` (default `farm-intelligence-platform`)
- `SIMPLE_JWT_AUDIENCE` (default `nextcloud`)

## Integration clients (legacy) + rotation strategy

Admin-only endpoints under `/api/v1/integrations/clients/` remain for legacy
metadata, but HMAC verification uses `INTEGRATION_HMAC_CLIENTS_JSON` as the
source of truth. Do not rely on IntegrationClient secrets for request signing.

Rotation strategy for `INTEGRATION_HMAC_CLIENTS_JSON`:
- add a new `client_id -> secret_b64` entry (keep the old entry temporarily)
- update Nextcloud to use the new `client_id` and secret
- remove the old entry once requests are confirmed on the new client_id

## Composing HMAC with existing auth (Option B)

### v1 (service-account style): HMAC + JWT or API key

Use when Nextcloud calls should authenticate as a service identity or when
either JWT or API key is acceptable for satisfying `IsAuthenticated`.

Recommended composition:
- keep default authentication classes (JWT + API key)
- `permission_classes = (NextcloudHMACPermission, IsAuthenticated)`

### v2 (per-user linking): HMAC + JWT only

Use when the caller must be a specific user session and API keys must not be
able to authenticate the route.

Recommended composition:
- `authentication_classes = (JWTAuthentication,)`
- `permission_classes = (NextcloudHMACPermission, IsAuthenticated)`

Important: explicitly forcing `JWTAuthentication` is required so API keys cannot
authenticate v2 endpoints.

## Troubleshooting (common 403 causes)

Inspect `errors.code` in the response:
- `missing_headers`: required signature headers are missing.
- `missing_config` / `bad_json`: `INTEGRATION_HMAC_CLIENTS_JSON` missing/invalid.
- `bad_base64`: `secret_b64` is not strict base64.
- `unknown_client`: `X-Client-Id` not present in `INTEGRATION_HMAC_CLIENTS_JSON`.
- `sig_mismatch`: canonical string mismatch or wrong secret.
- `body_hash_mismatch`: body hash does not match the raw bytes sent.
- `path_mismatch`: signed path differs (often trailing slash or proxy rewrite).
- `method_mismatch`: signed method differs from request method.
- `skew`: `X-NC-TIMESTAMP` outside `NEXTCLOUD_HMAC_MAX_SKEW_SECONDS`.
- `replay`: nonce reused within `NEXTCLOUD_HMAC_NONCE_TTL_SECONDS`.

Canonicalization mismatches are the most common cause:
- `PATH` must match exactly (including trailing slash).
- `CANONICAL_QUERY` must:
  - decode with form semantics (`+` becomes space)
  - re-encode with RFC3986 (spaces become `%20`, never `+`)
  - sort by `(encoded_key, encoded_value)` (ASCII)
- `BODY_SHA256` must be computed from the exact raw bytes sent on the wire.
