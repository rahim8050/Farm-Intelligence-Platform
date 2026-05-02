# Monitoring Stack (Prometheus + Grafana)

## What Exists Today

- `/metrics` is exposed by Django via `django_prometheus.urls`.
- Django Prometheus middleware is enabled, providing request, response, and DB metrics.
- Custom metrics:
  - NDVI: `ndvi_jobs_total`, `ndvi_upstream_requests_total`, `ndvi_upstream_latency_seconds`, `ndvi_cache_hit_total`, `ndvi_farms_stale_total`
  - NDVI stream consumer: `redis_stream_pending_entries`, `redis_stream_pending_age_max`, `ndvi_stream_consumer_heartbeat`, `ndvi_stream_consumer_failures_total`
  - Weather: `weather_provider_requests_total`, `weather_provider_errors_total`, `weather_provider_latency_seconds`, `weather_cache_hits_total`, `weather_cache_misses_total`
  - Celery: `celery_tasks_total`, `celery_tasks_in_progress`, `celery_task_runtime_seconds`

NDVI stream consumer metrics are exposed from the consumer process itself on
`NDVI_STREAM_METRICS_PORT` (default `8002`).

Celery worker metrics are exposed from the worker process itself on
`NDVI_CELERY_METRICS_PORT` (default `8003`).

Celery metrics are aggregated via the shared cache; ensure the Celery workers
and Django API share the same Redis cache in production.
- Monitoring scaffolding exists in `docker-compose.monitoring.yml` and `monitoring/prometheus/prometheus.yml`.

## Cardinality Guardrails (Summary)

All custom metrics use low-cardinality labels only:
- NDVI labels: `status`, `type`, `engine`, `outcome`, `layer`
- Weather labels: `provider`, `endpoint`, `error_type`
- Celery labels: `task`, `event`

Do not add labels with `job_id`, `farm_id`, `user_id`, raw URL paths, request IDs, timestamps, or exception messages.

## Quick Start (Local)

Bring up the monitoring stack:

```bash
docker compose -f docker-compose.monitoring.yml up -d
```

Set exporter connection variables (examples below). These are read by docker compose:

```bash
REDIS_ADDR=redis://host.docker.internal:26379
MYSQL_EXPORTER_DSN=user:password@(host.docker.internal:3306)/
POSTGRES_EXPORTER_DSN=postgresql://user:password@host.docker.internal:5432/postgres?sslmode=disable
```

For Redis Sentinel validation, point `REDIS_ADDR` at a published Sentinel port
instead of the standalone Redis port. With the current `redis_exporter` image,
Sentinel health is exposed via `redis_sentinel_*` series such as:

- `redis_sentinel_master_status`
- `redis_sentinel_master_ok_sentinels`
- `redis_sentinel_master_ok_slaves`
- `redis_sentinel_masters`

If you use MySQL, enable the MySQL exporter profile:

```bash
docker compose -f docker-compose.monitoring.yml --profile mysql up -d
```

If you use Postgres, enable the Postgres exporter profile:

```bash
docker compose -f docker-compose.monitoring.yml --profile postgres up -d
```

## Verify

Confirm `/metrics` is reachable locally:

```bash
curl -sS http://localhost:8000/metrics | head
```

Check Prometheus targets:

```bash
curl -sS http://localhost:9090/api/v1/targets | head
```

Check blackbox probe results:

```bash
curl -sS http://localhost:9090/api/v1/query?query=probe_success
```

## Blackbox Targets

Update the blackbox targets in `monitoring/prometheus/prometheus.yml`:
- DRF health: `http://host.docker.internal:8000/`
- Nextcloud base URL: set to your real Nextcloud base URL

## Security Notes

- `/metrics` should be internal only.
- Exporter ports are not published to the host in `docker-compose.monitoring.yml`.
- Prometheus, Grafana, and Loki are bound to `127.0.0.1` by default.
- For production, enforce access via firewall or reverse proxy ACLs.

## Alert Rules

Alert rules live in `monitoring/prometheus/alerts.yml` and cover:
- Target down and blackbox probe failures
- Django 5xx rate spikes and p95 latency
- Disk space low/critical (Nextcloud + DB mounts)
- DB exporter down and connection saturation
- Redis down and memory pressure
- NDVI failures and missing successful jobs
- Weather provider error rate

## Dashboard Spec v1

**Overview / NOC**
- Targets up: `up`
- Blackbox probe success: `probe_success`
- Django 5xx rate: `sum(rate(django_http_responses_total_by_status_total{job="django",status=~"5.."}[5m])) / sum(rate(django_http_responses_total_by_status_total{job="django"}[5m]))`
- Django p95 latency: `histogram_quantile(0.95, sum by (le) (rate(django_http_requests_latency_seconds_by_view_method_bucket{job="django"}[5m])))`
- Redis up: `up{job="redis_exporter"}`

**API (DRF)**
- RPS by method: `sum by (method) (rate(django_http_requests_total_by_view_transport_method_total{job="django"}[5m]))`
- p95 latency by view: `histogram_quantile(0.95, sum by (le, view) (rate(django_http_requests_latency_seconds_by_view_method_bucket{job="django"}[5m])))`
- 4xx/5xx by status: `sum by (status) (rate(django_http_responses_total_by_status_total{job="django",status=~"4..|5.."}[5m]))`
- NDVI jobs by status: `sum by (status) (increase(ndvi_jobs_total[15m]))`
- Weather provider errors: `sum by (provider) (rate(weather_provider_errors_total[10m]))`

**Nextcloud + Proxy**
- Nextcloud probe success: `probe_success{service="nextcloud"}`
- Nextcloud probe latency: `probe_duration_seconds{service="nextcloud"}`
- DRF home probe success: `probe_success{service="django"}`
- DRF home probe latency: `probe_duration_seconds{service="django"}`

**Infra (Host / DB / Redis)**
- CPU: `rate(node_cpu_seconds_total{mode!="idle"}[5m])`
- Memory: `1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)`
- Disk free %: `node_filesystem_avail_bytes{fstype!~"tmpfs|overlay"} / node_filesystem_size_bytes{fstype!~"tmpfs|overlay"}`
- MySQL connections: `mysql_global_status_threads_connected / mysql_global_variables_max_connections`
- Postgres connections: `sum by (instance) (pg_stat_activity_count) / max by (instance) (pg_settings_max_connections)`
- Redis memory: `redis_memory_used_bytes / redis_memory_max_bytes`
