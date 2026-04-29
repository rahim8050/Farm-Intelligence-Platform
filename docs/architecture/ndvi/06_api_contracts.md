# NDVI API Contracts

## Current Django modules

- `ndvi/views.py`
- `ndvi/serializers.py`
- `config/api/responses.py`
- `ndvi/tasks.py`

## Service class mapping

- `NdviTimeseriesService`
- `NdviLatestService`
- `NdviFarmStateService`
- `NdviRefreshService`
- `NdviRasterService`

## Endpoint map

- `GET /api/v1/farms/<farm_id>/ndvi/timeseries/`
- `GET /api/v1/farms/<farm_id>/ndvi/latest/`
- `GET /api/v1/farm-state/<farm_id>/`
- `POST /api/v1/farms/<farm_id>/ndvi/refresh/`
- `GET /api/v1/farms/<farm_id>/ndvi/raster.png`
- `POST /api/v1/farms/<farm_id>/ndvi/raster/queue`

## Response envelope

All successful responses use the existing envelope:

```json
{
  "success": 0,
  "message": "string",
  "data": {}
}
```

## V1 response example

```json
{
  "success": 0,
  "message": "NDVI timeseries",
  "data": {
    "farm_id": 22,
    "representation": "v1",
    "observations": [
      {
        "bucket_date": "2026-04-01",
        "mean": 0.541,
        "min": 0.419,
        "max": 0.629,
        "sample_count": 16384,
        "cloud_fraction": 0.243
      }
    ]
  }
}
```

## V2 response example

```json
{
  "success": 0,
  "message": "NDVI timeseries",
  "data": {
    "farm_id": 22,
    "representation": "v2",
    "observations": [
      {
        "bucket_date": "2026-04-01",
        "source": "sentinel2",
        "selected_ndvi": 0.541,
        "smoothed_ndvi": 0.538,
        "confidence": 0.91,
        "quality_flags": {
          "cloud_heavy": false,
          "fallback_used": false
        }
      }
    ]
  }
}
```

## Null response example

```json
{
  "success": 0,
  "message": "NDVI latest",
  "data": {
    "farm_id": 22,
    "representation": "v2",
    "observation": null,
    "stale": true,
    "reason": "low_confidence"
  }
}
```

## Serializer mapping

- `TimeseriesRequestSerializer` validates `start`, `end`, `step_days`, and `max_cloud`
- `LatestRequestSerializer` validates `lookback_days` and `max_cloud`
- `RasterPngRequestSerializer` validates `date`, `size`, and `max_cloud`
- `NdviObservationSerializer` serializes V1 rows
- `FarmStateSerializer` serializes the derived farm state payload
- `NdviJobSerializer` exposes the async job envelope

## Transaction boundaries

- Read endpoints do not open business transactions.
- Refresh endpoints only create or enqueue jobs.
- Raster queue endpoints only enqueue work and return the job envelope.

## Backward compatibility

- Keep V1 as the default representation until the validation window closes.
- Expose V2 through `?representation=v2` or an equivalent explicit flag.
- Do not change the envelope shape.

