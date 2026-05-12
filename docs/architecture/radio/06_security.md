# Security Architecture

> **Status**: ✅ IMPLEMENTED (Public access, AllowAny permissions)

## HTTPS Requirements

All radio endpoints must be served over HTTPS in production.

### Configuration

```python
# config/settings.py

SECURE_SSL_REDIRECT = True  # Redirect HTTP to HTTPS
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
```

### Stream URLs

Radio providers may serve over HTTP. This is acceptable because:

1. Stream URLs are retrieved via HTTPS API
2. Audio streaming protocols (HTTP streaming) don't have the same security requirements as API calls
3. Users expect stream compatibility with various providers

However, prefer providers offering HTTPS streams when available.

## DRF Throttling Strategy

### Throttle Classes

Following project conventions in `config/settings.py`:

```python
# radio/throttling.py

from rest_framework.throttling import SimpleRateThrottle


class RadioListThrottle(SimpleRateThrottle):
    """Throttle for station list endpoint."""
    scope = "radio_list"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request)
        }


class RadioDetailThrottle(SimpleRateThrottle):
    """Throttle for station detail/stream endpoints."""
    scope = "radio_detail"
```

### Rate Configuration

```python
# config/settings.py

REST_FRAMEWORK = {
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "radio_list": "60/minute",
        "radio_detail": "120/minute",
    },
}
```

### Rationale

| Endpoint | Rate | Rationale |
|----------|------|-----------|
| List | 60/min | Higher rate for page refreshes |
| Detail/Stream | 120/min | Higher rate for playback controls |

## Token/JWT Options

### v1: No Authentication (Public)

Rationale: Radio streams are typically public. No auth required.

```python
# radio/views.py
class StationListView(APIView):
    permission_classes = []  # Public access
```

### v2: Optional Authentication

If favorites/history are added:

```python
# radio/views.py
from rest_framework.permissions import IsAuthenticated

class FavoriteStationView(APIView):
    permission_classes = [IsAuthenticated]
```

### Token Strategy

| Feature | Auth Required | Justification |
|---------|---------------|---------------|
| List stations | No | Public information |
| Station detail | No | Public information |
| Stream URL | No | Public stream |
| Favorites | Yes | User-specific data |
| Listening history | Yes | User-specific data |

## Public vs Authenticated Stream Access

### Public Access (v1)

All endpoints are public. Stream URLs are not secret.

**Rationale**:
- Radio stations broadcast publicly
- No authentication needed to listen
- Simplifies Nextcloud integration
- Reduces attack surface

### Future: Authenticated Streams

If access control is needed later:

```python
class SignedStreamUrlView(APIView):
    """Return time-limited signed stream URL."""

    permission_classes = [IsAuthenticated]

    def get(self, request, station_id):
        # Generate time-limited URL
        expires = timezone.now() + timedelta(hours=1)
        token = jwt.encode({
            "station_id": station_id,
            "exp": expires,
            "user_id": request.user.id,
        }, settings.SECRET_KEY, algorithm="HS256")

        stream_url = f"{station.stream_url}?token={token}"
        return Response({"stream_url": stream_url})
```

## Environment-Based Configuration

### Environment Variables

```bash
# .env
RADIO_DEFAULT_PROVIDER=bbc
RADIO_ENABLE_HEALTH_CHECKS=true
RADIO_HEALTH_CHECK_INTERVAL=300  # seconds
```

### Settings Configuration

```python
# config/settings.py

RADIO_DEFAULT_PROVIDER = env("RADIO_DEFAULT_PROVIDER", default="bbc")
RADIO_ENABLE_HEALTH_CHECKS = env.bool("RADIO_ENABLE_HEALTH_CHECKS", default=True)
RADIO_HEALTH_CHECK_INTERVAL = env.int("RADIO_HEALTH_CHECK_INTERVAL", default=300)
```

### Security Checklist

- [ ] HTTPS enforced in production
- [ ] CORS restricted to Nextcloud origin
- [ ] No sensitive data in station metadata
- [ ] Stream URLs are public (no secrets)
- [ ] Throttling configured appropriately
- [ ] No hardcoded credentials
- [ ] Logging excludes sensitive data