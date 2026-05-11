# Proposed App Structure

## Folder Hierarchy

```text
radio/
├── __init__.py
├── apps.py
├── admin.py
├── models.py           # Station, Provider models
├── managers.py         # Custom query managers
├── urls.py             # URL routing
├── views.py            # API views
├── serializers.py      # DRF serializers
├── services.py         # Business logic layer
├── permissions.py      # Custom permissions
├── throttling.py       # Custom throttle classes
├── tasks.py            # Celery tasks (optional)
├── consumers.py        # WebSocket consumers (optional)
├── tests/
│   ├── __init__.py
│   ├── test_models.py
│   ├── test_views.py
│   └── test_services.py
├── management/
│   └── commands/
│       └── load_stations.py  # Initial data loader
└── migrations/
    └── 0001_initial.py
```

This mirrors the structure of existing apps (`activities/`, `weather/`, `ndvi/`).

## Service Layer Design

The `services.py` module encapsulates business logic, keeping views thin.

### Example: Station Service

```python
# radio/services.py

class StationService:
    """Business logic for radio stations."""

    @staticmethod
    def get_all_stations(filters: dict | None = None) -> QuerySet:
        """Return all active stations, optionally filtered."""
        qs = Station.objects.filter(is_active=True)
        if filters:
            if provider := filters.get("provider"):
                qs = qs.filter(provider__slug=provider)
            if genre := filters.get("genre"):
                qs = qs.filter(genre__icontains=genre)
        return qs.select_related("provider")

    @staticmethod
    def get_station_by_id(station_id: str) -> Station | None:
        """Return station by ID or None."""
        return Station.objects.filter(
            id=station_id,
            is_active=True
        ).select_related("provider").first()

    @staticmethod
    def get_stream_url(station_id: str) -> dict:
        """Return stream URL and metadata for playback."""
        station = StationService.get_station_by_id(station_id)
        if not station:
            raise StationNotFoundError(station_id)
        return {
            "stream_url": station.stream_url,
            "format": station.format,
            "bitrate": station.bitrate,
            "name": station.name,
        }
```

### Separation Pattern

| Layer | Responsibility |
|-------|----------------|
| `views.py` | HTTP handling, serialization, status codes |
| `services.py` | Business logic, data transformation |
| `models.py` | Data storage, queries |
| `serializers.py` | Data representation for API |

## Serializer Responsibilities

### StationSerializer

```python
# radio/serializers.py

class StationSerializer(serializers.ModelSerializer):
    """Serialize station metadata for API responses."""

    provider_name = serializers.CharField(source="provider.name", read_only=True)

    class Meta:
        model = Station
        fields = [
            "id",
            "name",
            "provider",
            "provider_name",
            "genre",
            "country",
            "language",
            "logo_url",
            "is_active",
        ]
```

### StationDetailSerializer

Includes stream URL (for detail endpoint only):

```python
class StationDetailSerializer(StationSerializer):
    """Extended serializer with stream URL."""

    stream_url = serializers.URLField(read_only=True)
    format = serializers.CharField(read_only=True)
    bitrate = serializers.IntegerField(read_only=True)

    class Meta(StationSerializer.Meta):
        fields = StationSerializer.Meta.fields + [
            "stream_url",
            "format",
            "bitrate",
            "website_url",
        ]
```

## Separation of Concerns

### What Belongs Where

| Concern | Location |
|---------|----------|
| Station CRUD | `models.py`, `managers.py` |
| Business rules | `services.py` |
| HTTP handling | `views.py` |
| Data transformation | `serializers.py` |
| Access control | `permissions.py` |
| Rate limiting | `throttling.py` |
| Health checks | `tasks.py` (optional Celery) |

### Anti-Patterns to Avoid

- **Fat views**: Business logic in `views.py`
- **Model serialization**: Using model `__str__` for API responses
- **Direct DB access in views**: Always go through services

## Future Migration Path: Constants → Database

### Phase 1: Hardcoded (v1)

```python
# radio/services.py - Hardcoded stations
STATIONS = [
    {
        "id": "bbc_1xtra",
        "name": "BBC 1Xtra",
        "stream_url": "http://stream.live.vc.bbcmedia.co.uk/bbc_1xtra",
        ...
    },
]

def get_stations():
    return STATIONS
```

### Phase 2: Database with Seed Data (v1.1)

```python
# radio/models.py
class Station(models.Model):
    id = models.CharField(max_length=50, primary_key=True)
    name = models.CharField(max_length=200)
    stream_url = models.URLField()
    ...

# radio/management/commands/load_stations.py
def handle(self, *args, **options):
    for station in SEED_DATA:
        Station.objects.get_or_create(
            id=station["id"],
            defaults=station
        )
```

### Phase 3: Admin-Managed (v2)

- Add Django admin for station management
- Allow non-code updates to station data
- Support multiple providers via admin

### Migration Path Summary

| Phase | Approach | Use Case |
|-------|----------|----------|
| v1 | Hardcoded constants | Fast MVP |
| v1.1 | Database + seed | Data-driven, versionable |
| v2 | Admin-managed | Non-developer updates |