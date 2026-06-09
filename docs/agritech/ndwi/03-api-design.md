# NDWI API Design

**Document:** 03-api-design.md
**Stage:** Design (pre-implementation)
**Status:** Draft for review

---

## Endpoints

All endpoints follow the NDVI pattern, substituting `ndvi` → `ndwi` in URL paths. Legacy NDVI endpoints remain unchanged.

| Method | Endpoint | View | Purpose |
|--------|----------|------|---------|
| GET | `/api/v1/farms/<id>/ndwi/timeseries/` | `NdwiTimeseriesView` | NDWI time series |
| GET | `/api/v1/farms/<id>/ndwi/latest/` | `NdwiLatestView` | Latest NDWI value |
| POST | `/api/v1/farms/<id>/ndwi/refresh/` | `NdwiRefreshView` | Manual refresh trigger |
| GET | `/api/v1/farms/<id>/ndwi/raster.png` | `NdwiRasterPngView` | Cached NDWI raster PNG |
| POST | `/api/v1/farms/<id>/ndwi/raster/queue` | `NdwiRasterQueueView` | Queue raster render |

### Internal Implementation

View classes are parameterized by `index_type`:

```python
class NdwiTimeseriesView(APIView):
    index_type = "NDWI"
    engine_prefix = "ndwi_"
    cache_prefix = "ndwi:cache:"

    # All logic identical to NdviTimeseriesView except:
    # - get_engine() called with index_type="NDWI"
    # - Cache keys use ndwi:cache: prefix
    # - Response serializer shows NDWI-appropriate ranges
```

This avoids code duplication while keeping legacy NDVI views untouched.

## Request Parameters

### GET `/api/v1/farms/<id>/ndwi/timeseries/`

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `engine` | string | No | `"ndwi_stac"` | NDWI engine name |
| `start` | date (ISO) | No | 30 days ago | Start of time range |
| `end` | date (ISO) | No | today | End of time range |
| `step_days` | int | No | 7 | Bucket interval |
| `max_cloud` | int | No | 30 | Max cloud % |
| `representation` | string | No | null | `"v2"` to include V2 quality |

### GET `/api/v1/farms/<id>/ndwi/latest/`

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `engine` | string | No | `"ndwi_stac"` | NDWI engine name |
| `lookback_days` | int | No | 14 | How far to look back |
| `max_cloud` | int | No | 30 | Max cloud % |
| `representation` | string | No | null | `"v2"` to include V2 quality |

### POST `/api/v1/farms/<id>/ndwi/refresh/`

No request body. Cooldown: 900s per user per farm.

### GET `/api/v1/farms/<id>/ndwi/raster.png`

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `date` | date (ISO) | Yes | — | Raster date |
| `engine` | string | No | `"ndwi_stac"` | Engine |
| `size` | int | No | 512 | Image size (max 1024) |
| `max_cloud` | int | No | 30 | Max cloud % |

## Response Examples

### Timeseries Response

```json
{
  "success": 0,
  "message": "OK",
  "data": [
    {
      "id": 1234,
      "index_type": "NDWI",
      "farm": 42,
      "engine": "ndwi_stac",
      "bucket_date": "2026-06-01",
      "mean": 0.15,
      "min": -0.05,
      "max": 0.42,
      "sample_count": 850,
      "cloud_fraction": 0.05,
      "valid_pixel_fraction": 0.92,
      "state": "FINAL",
      "acquired_at": "2026-06-01T09:32:00Z"
    }
  ]
}
```

### V2 Quality Response

When `?representation=v2`:

```json
{
  "success": 0,
  "message": "OK",
  "data": [
    {
      "id": 1235,
      "index_type": "NDWI",
      "farm": 42,
      "engine": "ndwi_stac",
      "bucket_date": "2026-06-01",
      "mean": 0.15,
      "source": "ndwi_stac",
      "selected_index_value": 0.15,
      "smoothed_index_value": 0.17,
      "confidence": 0.88,
      "is_null": false,
      "quality_flags": {}
    }
  ]
}
```

## Versioning Strategy

| Version | Approach | Details |
|---------|----------|---------|
| V1 (raw) | Current `SpectralObservation` model | Direct engine output, no quality processing |
| V2 (quality) | `SpectralDerivedObservation` model | Confidence scoring, temporal smoothing, outlier rejection |

Same V1 → V2 architecture as NDVI. V2 is requested via `?representation=v2`. V2 is not returned by default (backward compatibility).

## Backward Compatibility

| Aspect | Strategy |
|--------|----------|
| **NDVI endpoints** | No changes. `/api/v1/farms/<id>/ndvi/*` continues to work identically. |
| **NDVI models** | Existing `NdviObservation` rows get `index_type="NDVI"`. Queries filtered to `index_type="NDVI"` by default on legacy views. |
| **NDVI cache** | Old `ndvi:cache:` keys are never read by NDWI views. NDWI uses `ndwi:cache:` prefix. No cache poisoning. |
| **NDVI Celery tasks** | The `run_ndvi_job` task continues to exist (filtered to `index_type="NDVI"`). New `run_ndwi_job` handles NDWI. |
| **NDVI metrics** | `ndvi_*` metrics unchanged. New `ndwi_*` metrics added independently. |
| **NDVI dashboards** | Not affected. NDWI gets separate dashboard panels. |

## Authentication & Permissions

Same as NDVI endpoints:
- **Auth:** `FarmObservationAuthentication`, `IntegrationJWTAuthentication`
- **Permission:** `IsAuthenticated`
- **Throttle:** Per-user rate limiting via existing throttle classes

## OpenAPI / Schema

Same pattern as NDVI:

```python
from drf_spectacular.utils import extend_schema, inline_serializer

NdwiEnvelope = inline_serializer(
    name="NdwiEnvelope",
    fields={
        "success": serializers.IntegerField(),
        "message": serializers.CharField(),
        "data": NdwiObservationSerializer(),
    },
)

@extend_schema(
    parameters=[...],
    responses={200: NdwiEnvelope},
)
def get(self, request, farm_id):
    ...
```
