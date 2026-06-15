# HMAC Integration Spec (Nextcloud ↔ DRF)

This document records the HMAC contract as implemented in the Django/DRF
backend and the Nextcloud app. It is evidence-based and derived from code and
tests in both repos. Do not guess; update this doc only after confirming
behavior in code/tests.

## Sources of truth

- DRF verification: `integrations/hmac.py`, `integrations/permissions.py`
- DRF token endpoint: `integrations/views.py`
- DRF config parsing: `integrations/config.py`
- Nextcloud signer/client: `apps/farm_intelligence_platform/lib/Service/TokenSigner.php`,
  `apps/farm_intelligence_platform/lib/Service/WeatherApiClient.php`
- Golden vector (shared): `tests/fixtures/hmac_test_vector.json`

## Truth table (current behavior)

### Header names (exact)

DRF accepts both Nextcloud-prefixed and integration headers:

- Nextcloud ping:
  - `X-NC-CLIENT-ID`
  - `X-NC-TIMESTAMP`
  - `X-NC-NONCE`
  - `X-NC-SIGNATURE`
- Integration token bootstrap:
  - `X-Client-Id`
  - `X-Timestamp`
  - `X-Nonce`
  - `X-Signature`
- Token endpoint also requires `X-API-Key` (API key auth).

### Canonical string

Canonical string format is a newline-separated string:

1. `METHOD` (uppercase)
2. `PATH` (DRF path only, e.g., `/api/v1/integrations/token/`)
3. Canonicalized query string
4. `TIMESTAMP` (Unix seconds)
5. `NONCE`
6. `BODY_SHA256_HEX`

For an empty query string, line 3 is empty (i.e., two consecutive newlines).

### Query canonicalization

Process:

1. Parse query string into key/value pairs, preserving duplicates.
2. Decode percent-escapes and `+` as space.
3. Re-encode using RFC3986 safe chars `-_.~`.
4. Sort by encoded key, then encoded value.
5. Join as `k=v` pairs with `&`.

### Body hash

- Compute SHA256 hex of the exact raw request body bytes.
- If method is `GET`, use empty bytes regardless of the body.

### Signature

- HMAC-SHA256 of the canonical string.
- Output is lowercase hex.

### Secret decoding

- `INTEGRATION_HMAC_CLIENTS_JSON` maps `client_id` → **base64** secret.
- Decoding uses standard base64 with padding (not base64url).

### Timestamp + replay

- Reject if `abs(now - timestamp) > NEXTCLOUD_HMAC_MAX_SKEW_SECONDS`.
- Reject replayed nonce within `NEXTCLOUD_HMAC_NONCE_TTL_SECONDS`.

## Golden vector

Shared fixture: `tests/fixtures/hmac_test_vector.json`.

Both repos must agree on:
- `expected_body_sha256`
- `expected_canonical`
- `expected_signature`
- `expected_secret_sha256`

## Debug logging (safe, opt-in)

DRF:
- Env: `NEXTCLOUD_HMAC_DEBUG_LOGGING` (default `false`)
- Logs canonical hash, body hash, secret fingerprint, signature + expected.

Nextcloud:
- Env: `WEATHER_APIS_HMAC_DEBUG` (default `false`)
- Logs canonical hash, body hash, secret fingerprint, signature.
