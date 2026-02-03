# Monitoring Runbook

## A. Overview / Goals

This runbook verifies end-to-end observability for the Django/DRF API. The goals
are to:

- Validate JWT auth flows, DB query metrics, endpoint request metrics, and
  latency histograms.
- Validate HTTP error/status monitoring (401/404/429/5xx) and rate limiting.
- Ensure correctness in `/metrics`, Prometheus UI (PromQL), and Grafana
  dashboards.

## B. Stack Components

- Django/DRF with `django-prometheus` exposing `/metrics`.
- Prometheus scraping Django metrics.
- Grafana visualizing PromQL.
- Optional Loki for logs (present in `docker-compose.monitoring.yml`).

## C. Django instrumentation details

### C1) DB instrumentation

`config/settings.py` maps supported DB engines to `django_prometheus` backends
(e.g., `django_prometheus.db.backends.mysql` for MySQL). For Postgres/MySQL,
this enables DB query metrics.

Expected metrics:

```promql
django_db_execute_total{alias="default",vendor="mysql"}
django_db_query_duration_seconds_bucket{alias="default",vendor="mysql",le="..."}
django_db_query_duration_seconds_count{alias="default",vendor="mysql"}
django_db_query_duration_seconds_sum{alias="default",vendor="mysql"}
```

### C2) HTTP request + latency instrumentation

Expected metrics:

```promql
django_http_requests_total_by_view_transport_method_total{view="farm-list",method="GET",...}
django_http_requests_latency_seconds_by_view_method_bucket{view="farm-list",method="GET",le="..."}
django_http_requests_latency_seconds_by_view_method_count{view="farm-list",method="GET"}
django_http_requests_latency_seconds_by_view_method_sum{view="farm-list",method="GET"}
```

### C3) HTTP response status instrumentation (error monitoring)

Confirmed metrics (from `/metrics`):

```promql
django_http_responses_total_by_status_total{status="200"}
django_http_responses_total_by_status_total{status="401"}
django_http_responses_total_by_status_total{status="404"}
django_http_responses_total_by_status_total{status="400"}
```

Per-view breakdowns:

```promql
django_http_responses_total_by_status_view_method_total{method="GET",status="401",view="farm-list"}
django_http_responses_total_by_status_view_method_total{method="GET",status="200",view="farm-list"}
django_http_responses_total_by_status_view_method_total{method="POST",status="400",view="login"}
django_http_responses_total_by_status_view_method_total{method="POST",status="429",view="login"}
django_http_responses_total_by_status_view_method_total{method="GET",status="404",view="<unnamed view>"}
```

Note: Prometheus applies the job label at scrape time; it does not appear in raw
`/metrics` output.

## D. Local verification workflow (step-by-step)

### D1) Auth sanity

Login returns user + JWT tokens in the custom response shape
(`status`, `message`, `data`, `errors`). Use valid credentials for a real user.

```bash
BASE_URL="http://localhost:8000"
```

```bash
curl -sS -X POST "$BASE_URL/api/v1/auth/login/" \
  -H "Content-Type: application/json" \
  -d '{"identifier":"<EMAIL_OR_USERNAME>","password":"<PASSWORD>"}'
```

```bash
ACCESS="<PASTE_ACCESS_TOKEN>"
```

```bash
curl -sS "$BASE_URL/api/v1/auth/me/" \
  -H "Authorization: Bearer $ACCESS"
```

Issues observed during validation:
- Shell paste concatenation can corrupt JSON, causing a parse error like
  "Extra data".
- Missing or expired `$ACCESS` returns `401`.

### D2) Verify DB metrics move

Trigger a DB-backed endpoint (example uses the farms list):

```bash
curl -sS "$BASE_URL/api/v1/farms/" \
  -H "Authorization: Bearer $ACCESS" \
  -o /dev/null
```

Probe DB metrics in `/metrics`:

```bash
curl -sS "$BASE_URL/metrics" | rg "django_db_execute_total"
```

```bash
curl -sS "$BASE_URL/metrics" | rg "django_db_query_duration_seconds_(bucket|count|sum)"
```

### D3) Verify per-view request & latency metrics move

Single request:

```bash
curl -sS "$BASE_URL/api/v1/farms/" \
  -H "Authorization: Bearer $ACCESS" \
  -o /dev/null
```

Probe request counters:

```bash
curl -sS "$BASE_URL/metrics" | rg "django_http_requests_total_by_view_transport_method_total.*view=\"farm-list\".*method=\"GET\""
```

Probe latency histogram:

```bash
curl -sS "$BASE_URL/metrics" | rg "django_http_requests_latency_seconds_by_view_method_(bucket|count|sum).*view=\"farm-list\".*method=\"GET\""
```

Generate load (200 requests):

```bash
for i in $(seq 1 200); do
  curl -sS "$BASE_URL/api/v1/farms/" \
    -H "Authorization: Bearer $ACCESS" \
    -o /dev/null
done
```

### D4) Verify error/status monitoring

Force a 401 by calling a protected endpoint without auth:

```bash
curl -sS -o /dev/null -w "%{http_code}\n" "$BASE_URL/api/v1/farms/"
```

Confirm status counters:

```bash
curl -sS "$BASE_URL/metrics" | rg "status=\"401\""
```

```bash
curl -sS "$BASE_URL/metrics" | rg "django_http_responses_total_by_status_total\{status=\"401\"\}"
```

Confirm per-view counters:

```bash
curl -sS "$BASE_URL/metrics" | rg "django_http_responses_total_by_status_view_method_total.*status=\"401\".*view=\"farm-list\""
```

## E. What we measured (example snapshot from local run)

DB per-request example:
- Farms request: 4 SQL statements.
- DB time added: ~3.65 ms.
- Avg per statement: ~0.91 ms.
- Conclusion: DB is not the bottleneck; most time is elsewhere.

farm-list latency example (after ~216 observations):
- Mean latency ~23 ms (sum/count).
- Buckets:
  - <= 25 ms: 160
  - <= 50 ms: 210
  - <= 100 ms: 213
  - <= 250 ms: 215
  - <= 500 ms: 216
- Conclusion: ~97% < 50 ms; small tail up to 500 ms.

## F. Prometheus UI gotcha + fix patterns

PromQL rate and increase functions can show 0 or blank when traffic is idle or
when instant evaluation has no recent samples.

Fix patterns:
- Use this pattern for recent counts:

```promql
increase(...[5m])
```

- Append this fallback to avoid blank panels:

```promql
or vector(0)
```

Example queries:

```promql
sum(increase(django_http_responses_total_by_status_total{job="django"}[5m]))
```

```promql
sum(increase(django_http_responses_total_by_status_total{job="django"}[5m])) or vector(0)
```

The increase function can be fractional due to scrape timing and extrapolation.
For count panels, apply the round function:

```promql
round(sum(increase(django_http_responses_total_by_status_total{job="django"}[5m]))) or vector(0)
```

## G. PromQL recipes (copy/paste)

Requests (farm-list), raw total:

```promql
django_http_requests_total_by_view_transport_method_total{job="django",view="farm-list",method="GET"}
```

Requests (farm-list), last 5m:

```promql
sum(increase(django_http_requests_total_by_view_transport_method_total{job="django",view="farm-list",method="GET"}[5m])) or vector(0)
```

Requests (farm-list), RPS:

```promql
sum(rate(django_http_requests_total_by_view_transport_method_total{job="django",view="farm-list",method="GET"}[5m])) or vector(0)
```

Latency (farm-list p95), bucket series:

```promql
django_http_requests_latency_seconds_by_view_method_bucket{job="django",view="farm-list",method="GET"}
```

Latency (farm-list p95), histogram quantile:

```promql
histogram_quantile(0.95, sum by (le) (rate(django_http_requests_latency_seconds_by_view_method_bucket{job="django",view="farm-list",method="GET"}[5m]))) or vector(0)
```

DB p95 query latency, bucket series:

```promql
django_db_query_duration_seconds_bucket{alias="default",vendor="mysql"}
```

DB p95 query latency, histogram quantile:

```promql
histogram_quantile(0.95, sum by (le) (rate(django_db_query_duration_seconds_bucket{alias="default",vendor="mysql"}[5m]))) or vector(0)
```

Error/status monitoring, 401 farm-list recent count (integerized):

```promql
round(sum(increase(django_http_responses_total_by_status_view_method_total{job="django",status="401",view="farm-list"}[5m]))) or vector(0)
```

Error/status monitoring, 404 overall recent count (integerized):

```promql
round(sum(increase(django_http_responses_total_by_status_total{job="django",status="404"}[5m]))) or vector(0)
```

Error/status monitoring, 5xx by view:

```promql
sum(increase(django_http_responses_total_by_status_view_method_total{job="django",status=~"5.."}[5m])) by (view) or vector(0)
```

Error/status monitoring, 5xx error rate % overall:

```promql
100 * (sum(rate(django_http_responses_total_by_status_total{job="django",status=~"5.."}[5m])) / sum(rate(django_http_responses_total_by_status_total{job="django"}[5m]))) or vector(0)
```

Error/status monitoring, non-2xx error rate % by view:

```promql
100 * (sum(rate(django_http_responses_total_by_status_view_method_total{job="django",status!~"2.."}[5m])) by (view) / sum(rate(django_http_responses_total_by_status_view_method_total{job="django"}[5m])) by (view)) or vector(0)
```

## H. Grafana access (local)

Grafana is typically available at `http://localhost:3000`.

WSL2 note (get IP for browser access):

```bash
hostname -I | awk '{print $1}'
```

Open `http://<WSL-IP>:3000` in a browser after retrieving the IP.

If you need to find admin credentials set via Docker Compose:

```bash
rg -n "GF_SECURITY_ADMIN_USER|GF_SECURITY_ADMIN_PASSWORD|grafana" docker-compose.monitoring.yml
```

## I. Docker Compose monitoring notes

The monitoring stack is defined in `docker-compose.monitoring.yml` and must be
started with the `-f` flag.

```bash
docker compose -f docker-compose.monitoring.yml up -d
```

Grafana is pinned to `grafana/grafana-oss:10.4.3`, and plugin preinstall is disabled via
`GF_PLUGINS_PREINSTALL_DISABLED=true` to avoid provisioning failures.

If you see a container-name conflict like `/weather-apis-prometheus-1 is already in use`, use one of the safe resolutions below.

Start Grafana only:

```bash
docker compose -f docker-compose.monitoring.yml up -d --no-deps grafana
```

Remove the old container if safe, then re-run:

```bash
docker rm -f weather-apis-prometheus-1
```

```bash
docker compose -f docker-compose.monitoring.yml up -d
```

## J. Rate limiting verification (login: 10/min)

Spike test (expect requests 1-10 => 400, 11-12 => 429):

```bash
URL="http://localhost:8000/api/v1/auth/login/"
PAYLOAD='{"email":"ratelimit@test.local","password":"wrong-password"}'

for i in $(seq 1 12); do
  curl -s -o /dev/null -w "%{http_code}\n" \
    -X POST "$URL" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD"
done
```

Confirm `Retry-After` header when throttled:

```bash
curl -sS -i -X POST "$URL" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD" | rg -i "HTTP/|Retry-After"
```

Confirm 429 appears in metrics (view="login", method="POST", status="429"):

```bash
curl -sS "http://localhost:8000/metrics" | rg "django_http_responses_total_by_status_view_method_total.*view=\"login\".*method=\"POST\".*status=\"429\""
```

```promql
round(sum(increase(django_http_responses_total_by_status_view_method_total{job="django",view="login",method="POST",status="429"}[5m]))) or vector(0)
```

## K. Production notes

- Do not expose Grafana publicly with default credentials.
- Prefer a reverse proxy with auth or VPN access.
- Keep `/metrics` internal.
