# Operational Considerations

> **Status**: ✅ IMPLEMENTED (Periodic station health checks, Prometheus metrics, envelope errors)
> **Phase 2 delivered**: 2026-06-03 — see `IMPLEMENTATION_SUMMARY.md` § Phase 2.
> **Still out of scope**: structured `extra=` logging, Grafana dashboard JSON, station-list
> caching layer, fallback-station redirect, `radio_api_request_latency` /
> `radio_api_request_errors` counters.

## Logging Strategy

### Log Levels

| Level | Use Case |
|-------|----------|
| DEBUG | Request/response details in development |
| INFO | Station access, playback events |
| WARNING | Degraded stations, slow responses |
| ERROR | Failed requests, exceptions |

### Structured Logging

```python
# radio/services.py
import logging

logger = logging.getLogger(__name__)

def get_station_by_id(station_id: str):
    logger.info(
        "station_retrieved",
        extra={
            "station_id": station_id,
            "user_id": getattr(request.user, "id", "anonymous"),
        }
    )
```

### What NOT to Log

- Stream URLs (may contain identifiers)
- User-specific data beyond IDs
- Provider API keys

## Monitoring

### Key Metrics

| Metric | Type | Description | Alert Threshold |
|--------|------|-------------|-----------------|
| `radio_stations_total` | Gauge | Stations evaluated in the last health-check run | < 1 |
| `radio_station_health_failures_total` | Counter | Per-station failed health checks (label: `station_id`) | rate > 0.1/s for 5m |
| `radio_station_health_successes_total` | Counter | Per-station successful health checks (label: `station_id`) | n/a |
| `radio_station_health_latency_seconds` | Histogram | Probe round-trip time in seconds | n/a |
| `radio_health_checks_last_run_timestamp` | Gauge | Unix timestamp of the last successful task run | now - value > 10m → stale |
| `radio_api_request_latency` | Counter (planned) | API response time | > 500ms — **not implemented yet** |
| `radio_api_request_errors` | Counter (planned) | Failed requests | > 10/min — **not implemented yet** |

### Prometheus Integration

```python
# radio/metrics.py

from prometheus_client import Counter, Histogram

radio_requests_total = Counter(
    "radio_api_requests_total",
    "Total radio API requests",
    ["endpoint", "status"]
)

radio_request_duration = Histogram(
    "radio_api_request_duration_seconds",
    "Radio API request duration"
)
```

### Grafana Dashboard

Recommended panels:
- Stations available (gauge)
- API request rate (graph)
- Response latency (heatmap)
- Health check failures (alert)

## Health Checks

### Endpoint

`GET /api/v1/radio/health/` — public, `AllowAny`. Returns the standard envelope.

```python
# radio/views.py — implemented in Phase 2

class RadioHealthView(APIView):
    """Public radio-service health summary.

    Auth:        AllowAny
    Throttle:    none
    Response:    envelope with `data` = RadioHealthPayload
    """

    @extend_schema(
        responses={200: RadioHealthEnvelope},
        auth=[],
    )
    def get(self, request: Request) -> Response:
        """Return aggregate availability of all active stations."""
        return success_response(
            summarize_health(),
            status_code=status.HTTP_200_OK,
        )
```

`RadioHealthPayload` shape (defined inline in `radio/views.py`):

| Key | Type | Meaning |
|-----|------|---------|
| `status` | `"healthy" \| "degraded" \| "unhealthy"` | `healthy` = all available, `unhealthy` = reachable=0 AND unreachable>0, else `degraded`; 0 stations = `degraded` |
| `stations_total` | int | Active stations in the catalogue |
| `stations_available` | int | Active stations with `is_available=True` |
| `stations_unavailable` | int | Active stations with `is_available=False` |
| `stations_unchecked` | int | Active stations with `is_available=None` (never checked yet) |
| `timestamp` | str (ISO 8601) | Server time of the response |

`is_available` semantics (per `radio/models.py`):

- `None`  → never probed yet. Bootstrap: not gated.
- `True`  → last probe succeeded. Playback permitted.
- `False` → last probe failed. `StationStreamView` returns **HTTP 503** with envelope
  `{"success": 1, "message": "Station is currently unavailable", "data": null,
  "errors": {"station_id": "<uuid>", "reason": "health_check_failed"}}`.

### Celery Health Check Task

`radio.tasks.check_all_stations_health` — registered in `CELERY_BEAT_SCHEDULE` as
`radio-health-check` with schedule `RADIO_HEALTH_CHECK_INTERVAL_SECONDS` (default 300 s).

```python
# radio/tasks.py — implemented in Phase 2

@app.task(bind=True, name="radio.tasks.check_all_stations_health",
          max_retries=0, time_limit=300, soft_time_limit=270)
def check_all_stations_health(self) -> dict[str, int]:
    """Iterate every active station, probe, record, and update metrics.

    Returns {"checked": int, "available": int, "unavailable": int} for logging
    and downstream dashboards.
    """
    summary = probe_all_active_stations()
    radio_stations_total.set(summary["checked"])
    radio_health_checks_last_run_timestamp.set(time.time())
    return summary
```

Probing lives in `radio/services.py` (`probe_station`, `record_probe_result`,
`probe_all_active_stations`). The probe issues a `httpx.Client.head(...)` request and
**falls back to GET** on `405`/`501`, since some providers reject HEAD. Timeouts and
network exceptions both produce an unreachable result; only HTTP 2xx/3xx codes are
considered reachable. The station's aggregate `is_available` is updated atomically
with a single `Station.objects.filter(pk=...).update(...)` call.

## Failure Handling

### Station Unavailable

```python
# radio/views.py

class StationStreamView(APIView):
    def get(self, request, station_id):
        station = get_object_or_404(Station, id=station_id, is_active=True)

        # Check availability
        if not is_station_available(station):
            return Response(
                {
                    "status": 1,
                    "message": "Station currently unavailable",
                    "data": None,
                    "errors": None
                },
                status=503
            )

        return Response({
            "status": 0,
            "message": "Stream URL retrieved",
            "data": {
                "stream_url": station.stream_url,
                "format": station.format
            },
            "errors": None
        })
```

### Retry Strategy

| Scenario | Action |
|----------|--------|
| Stream URL fails | Return 503, suggest retry |
| Provider API fails | Use cached data, log error |
| Database unavailable | Return 503, no partial data |

## Provider Downtime Handling

### Caching Strategy

```python
# radio/services.py

class StationService:
    CACHE_TTL = 300  # 5 minutes

    def get_all_stations(self):
        cache_key = "radio:stations:all"
        cached = cache.get(cache_key)

        if cached:
            return cached

        stations = Station.objects.filter(is_active=True).select_related("provider")
        cache.set(cache_key, stations, self.CACHE_TTL)
        return stations
```

### Fallback Stations

```python
# radio/services.py

FALLBACK_STATIONS = {
    "bbc_1xtra": "bbc_radio1",  # If 1Xtra fails, suggest Radio 1
}

def get_stream_url_with_fallback(station_id: str) -> dict:
    station = get_station_by_id(station_id)

    if not is_station_available(station):
        fallback_id = FALLBACK_STATIONS.get(station_id)
        if fallback_id:
            return get_stream_url_with_fallback(fallback_id)
        raise StationUnavailableError(station_id)

    return {"stream_url": station.stream_url}
```

### Monitoring Alerts

Three alerts live in `monitoring/prometheus/alerts.yml` under group `radio`:

```yaml
# prometheus/alerts.yml — radio group, shipped 2026-06-03

- alert: RadioStationHealthCheckFailing
  expr: rate(radio_station_health_failures_total[5m]) > 0.1
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "Radio station failing health checks"
    description: "Station {{ $labels.station_id }} failing for 5m."

- alert: RadioStationsAllUnavailable
  expr: radio_stations_total > 0 and radio_station_health_successes_total == 0
  for: 2m
  labels:
    severity: critical
  annotations:
    summary: "All radio stations are unreachable"
    description: "No station has succeeded in the last run."

- alert: RadioHealthCheckStale
  expr: time() - radio_health_checks_last_run_timestamp > 600
  for: 2m
  labels:
    severity: warning
  annotations:
    summary: "Radio health-check task is stale"
    description: "Last run > 10m ago. Check Celery worker / beat."
```

## Runbook

### Station Playback Fails

1. Check if provider is down (external)
2. Verify station is active in admin
3. Check health check history
4. Update stream URL if changed
5. Contact provider if persistent

### High API Latency

1. Check database queries (add indexes)
2. Review cache hit rate
3. Check network to provider
4. Scale Django workers if needed

### No Stations Available

1. Verify database connectivity
2. Check station seed data loaded
3. Verify `is_active` flags
4. Run management command to reload

## Dependencies

| Dependency | Version | Purpose |
|------------|---------|---------|
| Django | ^5.0 | Web framework |
| djangorestframework | ^3.14 | API framework |
| django-cors-headers | ^4.0 | CORS |
| prometheus-client | ^0.19 | Metrics |
| requests | ^2.31 | (not used; see httpx) |
| httpx | ^0.27 | HTTP client (probes) |