# API Architecture

> **Status**: ✅ IMPLEMENTED

## Proposed Endpoint Structure

All radio endpoints follow the `/api/v1/radio/` prefix, consistent with existing project conventions.

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/radio/stations/` | List all available stations |
| GET | `/api/v1/radio/stations/{id}/` | Get single station details |
| GET | `/api/v1/radio/stations/{id}/stream/` | Get stream URL for playback |
| GET | `/api/v1/radio/providers/` | List available providers |
| GET | `/api/v1/radio/providers/{id}/stations/` | List stations by provider |

### Example: Station List

```
GET /api/v1/radio/stations/

Response:
{
    "status": 0,
    "message": "Stations retrieved successfully",
    "data": {
        "count": 1,
        "results": [
            {
                "id": "bbc_1xtra",
                "name": "BBC 1Xtra",
                "provider": "bbc",
                "genre": "Hip Hop",
                "country": "UK",
                "language": "English",
                "logo_url": "https://example.com/logo.png",
                "is_active": true
            }
        ]
    }
}
```

## API Versioning Strategy

The project uses URL-based versioning:

```
/api/v1/radio/...
```

This aligns with existing endpoints:
- `/api/v1/weather/`
- `/api/v1/activities/`
- `/api/v1/ndvi/`

Version changes follow project conventions (v1, v2, etc.).

## Response Envelope Format

All responses use the standard envelope defined in `config.api.responses.success_response`:

```json
{
    "status": 0,
    "message": "string",
    "data": "object|null",
    "errors": null
}
```

### Success Envelope

| Field | Type | Description |
|-------|------|-------------|
| `status` | int | 0 for success (1 for error) |
| `message` | string | Human-readable status message |
| `data` | object | Response payload |

### Error Envelope

```json
{
    "status": 1,
    "message": "Station not found",
    "data": null,
    "errors": null
}
```

## Error Handling Format

| HTTP Status | Scenario | Message |
|-------------|-----------|---------|
| 404 | Station not found | "Station not found" |
| 404 | Provider not found | "Provider not found" |
| 429 | Rate limit exceeded | "Rate limit exceeded. Try again later." |

Error responses follow the envelope format with `status: 1`.

## Authentication Strategy Options

### Option 1: Public Access (Recommended for v1)

Rationale: Radio streams are typically public. No auth required for station metadata or stream URLs.

```python
# In radio/views.py
class StationListView(APIView):
    permission_classes = []  # No authentication
```

### Option 2: Authenticated Access

If listening history or favorites are needed:

```python
class StationListView(APIView):
    permission_classes = [IsAuthenticated]
```

**Decision**: Start with public access (Option 1). Add auth later if favorites/history require it.

## Rate Limiting Considerations

| Endpoint | Throttle Scope | Rate |
|----------|---------------|------|
| `stations/` | `radio_list` | 60/min |
| `stations/{id}/` | `radio_detail` | 120/min |
| `stations/{id}/stream/` | `radio_stream` | 120/min |

Throttle classes follow project conventions in `config/settings.py`.

## CORS Considerations

- Allow Nextcloud origin via `CORS_ALLOWED_ORIGINS`
- Use `django-cors-headers` for configuration
- No wildcard `*` in production

```python
CORS_ALLOWED_ORIGINS = [
    "https://nextcloud.example.com",
]
```

## OpenAPI Documentation

Endpoints will be documented with `drf-spectacular` using:

- `@extend_schema()` decorators
- Inline serializers for response envelopes
- Request/response examples

Example:

```python
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers

StationEnvelope = inline_serializer(
    name="StationEnvelope",
    fields={
        "status": serializers.IntegerField(),
        "message": serializers.CharField(),
        "data": StationSerializer(),
        "errors": serializers.JSONField(allow_null=True),
    },
)

class StationListView(APIView):
    @extend_schema(
        responses={200: StationEnvelope}
    )
    def get(self, request):
        ...
```