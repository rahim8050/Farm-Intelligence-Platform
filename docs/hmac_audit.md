# HMAC Audit (Nextcloud <-> DRF)

This audit documents the request-signing contract between the Nextcloud app
(`apps/farm_intelligence_platform`) and the DRF backend (`farm-intelligence-platform`).

## Nextcloud signing (apps/farm_intelligence_platform)

Source of truth:
- `apps/farm_intelligence_platform/lib/Service/TokenSigner.php`
  - `TokenSigner::buildCanonicalString`
  - `TokenSigner::canonicalizeQuery`
  - `TokenSigner::bodySha256Hex`
- `apps/farm_intelligence_platform/lib/Service/WeatherApiClient.php`
  - `WeatherApiClient::ping`
  - `WeatherApiClient::mintToken`
- `apps/farm_intelligence_platform/lib/Service/IntegrationConfig.php`
  - `IntegrationConfig::getSecretBytes`
  - `IntegrationConfig::setCredentials`
- `apps/farm_intelligence_platform/lib/Controller/AdminConfigController.php`
  - `AdminConfigController::generateHmacSecretB64`

### Headers sent by Nextcloud

Ping (`GET /api/v1/integrations/nextcloud/ping/`):
- `X-NC-CLIENT-ID`
- `X-NC-TIMESTAMP`
- `X-NC-NONCE`
- `X-NC-SIGNATURE`
- Optional alias: `X-Client-Id`
- Correlation: `X-Request-Id`

Token bootstrap (`POST /api/v1/integrations/token/`):
- `X-API-Key`
- `X-Client-Id`
- `X-Timestamp`
- `X-Nonce`
- `X-Signature`
- Correlation: `X-Request-Id`

### Canonical string and signature (Nextcloud)

Canonical string format (newline-separated):
`METHOD\nPATH\nCANONICAL_QUERY\nTIMESTAMP\nNONCE\nBODY_SHA256`

Rules from `TokenSigner`:
- `METHOD`: uppercased.
- `PATH`: exact request path (trailing slash matters).
- `CANONICAL_QUERY`:
  - split raw query string on `&`, preserve duplicates
  - decode with `rawurldecode` after converting `+` to space
  - re-encode with `rawurlencode`, then replace `%7E` with `~`
  - sort by `(encoded_key, encoded_value)`
  - join as `k=v&k=v` with no leading `?`
- `TIMESTAMP`: unix seconds, provided as string.
- `NONCE`: random string (default: 32 hex chars from `random_bytes(16)`).
- `BODY_SHA256`:
  - SHA-256 hex of raw body
  - for `GET`, hash the empty body

Signature:
- `hash_hmac('sha256', $canonical, $secret)` (hex)
- `secret` is the decoded base64 bytes from config

### Secret handling (Nextcloud)

- Admin generates a secret with `base64_encode(random_bytes(32))`.
- `INTEGRATION_HMAC_CLIENTS_JSON` is stored encrypted via `ICrypto`.
- `IntegrationConfig::getSecretBytes` decrypts JSON and `base64_decode(..., true)`.

## DRF verification (farm-intelligence-platform)

Source of truth:
- `integrations/hmac.py` (canonicalization + verification)
- `integrations/permissions.py` (permission enforcement)
- `integrations/views.py` (endpoint auth/permissions)
- `integrations/config.py` (secret loading/decoding)
- `integrations/urls.py` (token endpoint path)

### Required headers (DRF accepts)

Preferred headers:
- `X-Client-Id`
- `X-Timestamp`
- `X-Nonce`
- `X-Signature`

Legacy aliases still accepted:
- `X-NC-CLIENT-ID`
- `X-NC-TIMESTAMP`
- `X-NC-NONCE`
- `X-NC-SIGNATURE`

Endpoint auth requirements:
- `POST /api/v1/integrations/token/`:
  - `ApiKeyAuthentication` (header `X-API-Key`)
  - `IntegrationHMACPermission`
- `GET /api/v1/integrations/nextcloud/ping/`:
  - `NextcloudHMACPermission`

### Canonical string and signature (DRF)

Canonical string format (newline-separated):
`METHOD\nPATH\nCANONICAL_QUERY\nTIMESTAMP\nNONCE\nBODY_SHA256`

Rules from `integrations/hmac.py`:
- `METHOD`: `request.method.upper()`.
- `PATH`: `request.path` (trailing slash matters).
- `CANONICAL_QUERY`:
  - parse raw query string (`QUERY_STRING`), preserve duplicates and blanks
  - decode using form semantics (`+` becomes space)
  - re-encode using RFC3986 with safe `-_.~`
  - sort by `(encoded_key, encoded_value)`
- `TIMESTAMP`: header value cast to `int`.
- `NONCE`: header value as-is.
- `BODY_SHA256`:
  - sha256 hex of raw body bytes
  - for `GET`, hash empty bytes

Signature:
- HMAC-SHA256 over UTF-8 canonical string bytes
- hex-encoded signature, lowercased before compare
- constant-time comparison

### Secret handling (DRF)

- `INTEGRATION_HMAC_CLIENTS_JSON` is required.
- JSON mapping: `client_id -> secret_b64`.
- `secret_b64` is decoded with `base64.b64decode(..., validate=True)`.

## Diff table (Nextcloud vs DRF)

| Rule | Nextcloud | DRF | Notes |
| --- | --- | --- | --- |
| Canonical string | `METHOD\nPATH\nCANONICAL_QUERY\nTIMESTAMP\nNONCE\nBODY_SHA256` | Same | Aligned (golden vector tests on both sides). |
| Method | Uppercase | Uppercase | Aligned. |
| Path | Exact path, trailing slash significant | Same | Aligned. |
| Query normalization | `+` -> space, RFC3986 encode, sort | Same | Aligned. |
| Body hash | SHA256 hex, GET uses empty | Same | Aligned. |
| Signature | HMAC-SHA256 hex | HMAC-SHA256 hex | Aligned. |
| Secret decoding | strict base64 | strict base64 | Aligned (standard alphabet). |
| Header names | `X-NC-*` for ping, `X-*` for token | Accepts both | Aligned via aliases. |

## Golden vector tests

- DRF: `tests/test_integrations_nextcloud_hmac.py`
- Nextcloud: `apps/farm_intelligence_platform/tests/unit/Service/TokenSignerTest.php`
