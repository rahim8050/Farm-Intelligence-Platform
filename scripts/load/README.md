# k6 load testing scripts

These scripts simulate high concurrency with a single account by reusing one or
more API keys.

## Prerequisites

- k6 installed (`k6 version`)
- API server reachable (example: `http://127.0.0.1:8001`)
- At least one valid API key (`X-API-Key`)

For integration token tests, you also need:
- `INTEGRATION_CLIENT_ID`
- `INTEGRATION_CLIENT_SECRET_B64`

## Safety notes

- Run on staging first.
- Temporarily raise throttles for realistic load numbers, then restore.
- Watch Grafana/Prometheus while tests run (p95/p99 latency, 4xx/5xx, DB/Redis).

## Common env vars

- `BASE_URL` (required)
- `API_KEY` or `API_KEY_LIST` (required)
- `SLEEP_SECONDS` (optional, default `0`)

`API_KEY_LIST` format:

```bash
API_KEY_LIST="wk_live_key1,wk_live_key2,wk_live_key3"
```

## 1) Hot cache load (same request repeatedly)

Script: `weather-hot-cache.js`

```bash
k6 run \
  -e BASE_URL=http://127.0.0.1:8001 \
  -e API_KEY=wk_live_xxx \
  -e HOT_RATE=40 \
  -e HOT_DURATION=5m \
  scripts/load/weather-hot-cache.js
```

Optional vars:
- `API_PATH` (default `/api/v1/weather/current/`)
- `PATH` (legacy fallback; avoid using because it can conflict with shell `PATH`)
- `LAT` (default `-1.2864`)
- `LON` (default `36.8172`)
- `TZ` (default `Africa/Nairobi`)
- `PROVIDER` (default empty)
- `HOT_PRE_ALLOCATED_VUS` (default `80`)
- `HOT_MAX_VUS` (default `300`)

## 2) Mixed cache load (varied request parameters)

Script: `weather-mixed-cache.js`

```bash
k6 run \
  -e BASE_URL=http://127.0.0.1:8001 \
  -e API_KEY_LIST="wk_live_xxx,wk_live_yyy" \
  -e MIXED_RATE=25 \
  -e MIXED_DURATION=5m \
  scripts/load/weather-mixed-cache.js
```

Optional vars:
- `API_PATH` (default `/api/v1/weather/current/`)
- `PATH` (legacy fallback; avoid using because it can conflict with shell `PATH`)
- `TZ` (default `Africa/Nairobi`)
- `PROVIDERS` (default `open_meteo,nasa_power`)
- `MIN_LAT` / `MAX_LAT` (defaults `-1.6` / `-1.0`)
- `MIN_LON` / `MAX_LON` (defaults `36.6` / `37.0`)
- `MIXED_PRE_ALLOCATED_VUS` (default `60`)
- `MIXED_MAX_VUS` (default `250`)

## 3) Integration token mint load (HMAC-signed)

Script: `integration-token-hmac.js`

```bash
k6 run \
  -e BASE_URL=http://127.0.0.1:8001 \
  -e API_KEY=wk_live_xxx \
  -e INTEGRATION_CLIENT_ID=11111111-1111-1111-1111-111111111111 \
  -e INTEGRATION_CLIENT_SECRET_B64=base64_secret_here \
  -e TOKEN_RATE=20 \
  -e TOKEN_DURATION=5m \
  scripts/load/integration-token-hmac.js
```

Optional vars:
- `TOKEN_PATH` (default `/api/v1/integrations/token/`)
- `ALLOW_429` (default `false`; set `true` if you intentionally keep throttles)
- `TOKEN_PRE_ALLOCATED_VUS` (default `50`)
- `TOKEN_MAX_VUS` (default `200`)

## Reading results quickly

- **Hot cache** should show lower p95/p99 latency than mixed.
- **Mixed cache** exposes DB/provider/caching pressure.
- **Integration token** highlights auth/HMAC/token mint hot path behavior.

Suggested PromQL checks:

```promql
histogram_quantile(0.95, sum(rate(django_http_requests_latency_seconds_by_view_method_bucket[5m])) by (le, view, method))
```

```promql
sum(rate(django_http_responses_total_by_status_view_method_total{status=~"5.."}[5m])) by (view, method)
```
