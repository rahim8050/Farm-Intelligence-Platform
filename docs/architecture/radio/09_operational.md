# Operational Considerations

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

| Metric | Description | Alert Threshold |
|--------|-------------|-----------------|
| `radio_stations_total` | Total active stations | < 1 |
| `radio_station_health_failures` | Failed health checks | > 5/min |
| `radio_api_request_latency` | API response time | > 500ms |
| `radio_api_request_errors` | Failed requests | > 10/min |

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

```python
# radio/views.py

class HealthCheckView(APIView):
    """Radio service health check."""

    def get(self, request):
        station_count = Station.objects.filter(is_active=True).count()

        return Response({
            "status": "healthy" if station_count > 0 else "degraded",
            "stations_active": station_count,
            "timestamp": timezone.now().isoformat()
        })
```

### Celery Health Check Task

```python
# radio/tasks.py

@app.task
def check_station_health(station_id: str) -> bool:
    """Verify station stream is reachable."""
    station = Station.objects.get(id=station_id)
    try:
        response = requests.head(station.stream_url, timeout=5)
        return response.status_code == 200
    except requests.RequestException:
        return False
```

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
                    "success": 1,
                    "message": "Station currently unavailable",
                    "data": None
                },
                status=503
            )

        return Response({
            "success": 0,
            "message": "Stream URL retrieved",
            "data": {
                "stream_url": station.stream_url,
                "format": station.format
            }
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

```yaml
# prometheus/alerts.yml

- alert: RadioStationDown
  expr: radio_station_health_failures > 5
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "Radio station unavailable"
    description: "{{ $value }} health checks failed"
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
| requests | ^2.31 | HTTP client |