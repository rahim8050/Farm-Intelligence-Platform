# NDVI Models

## Current Django modules

- `ndvi/models.py`
- `ndvi/serializers.py`
- `ndvi/tasks.py`
- `ndvi/farm_state.py`

## Current persisted models

### `NdviObservation`

Represents the V1 raw observation layer.

Fields:

- `farm`: `ForeignKey(Farm, on_delete=CASCADE, related_name="ndvi_observations")`
- `engine`: `CharField(max_length=64, default=default_ndvi_engine_name)`
- `bucket_date`: `DateField`
- `mean`: `FloatField`
- `min`: `FloatField(null=True, blank=True)`
- `max`: `FloatField(null=True, blank=True)`
- `sample_count`: `IntegerField(null=True, blank=True)`
- `cloud_fraction`: `FloatField(null=True, blank=True)`
- `created_at`: `DateTimeField(auto_now_add=True)`
- `updated_at`: `DateTimeField(auto_now=True)`

Constraints:

- Unique constraint on `(farm, engine, bucket_date)`
- Indexes on `(farm, bucket_date)` and `(engine, bucket_date)`

Implementation mapping:

- V1 persistence logic lives in `ndvi/services.py` and `ndvi/tasks.py`
- Serializer surface lives in `ndvi/serializers.py`
- Farm-state reads V1 rows in `ndvi/farm_state.py`

### `NdviJob`

Represents the idempotent async job envelope.

Fields:

- `owner`, `farm`, `engine`
- `job_type`
- `start`, `end`, `step_days`, `max_cloud`, `lookback_days`
- `request_hash`
- `status`
- `attempts`
- `last_error`
- `locked_until`
- `created_at`, `started_at`, `finished_at`

Constraints:

- Unique active job constraint on `(owner, farm, engine, request_hash)` while `status in queued|running`
- Indexes on `(owner, farm, status)` and `request_hash`

Implementation mapping:

- Job creation and locking live in `ndvi/services.py`
- Job execution lives in `ndvi/tasks.py`
- Job status API lives in `ndvi/views.py` and `ndvi/serializers.py`

### `NdviRasterArtifact`

Represents the persisted raster PNG artifact.

Fields:

- `farm`, `owner_id`, `engine`, `date`, `size`, `max_cloud`
- `content_hash`
- `image`
- `created_at`
- `last_error`

Constraints:

- Unique constraint on `(farm, engine, date, size, max_cloud)`
- Indexes on `(owner_id, date)` and `(engine, date)`

## Additive V2 model contract

The V2 layer must be persisted separately from V1. Keep the raw row unchanged.

### `NdviDerivedObservation`

Store as an additive model in `ndvi/models.py` if persistence is required.

Fields:

- `farm`: `ForeignKey(Farm, on_delete=CASCADE, related_name="ndvi_v2_observations")`
- `v1_observation`: `OneToOneField(NdviObservation, on_delete=CASCADE, related_name="v2_observation")`
- `engine`: `CharField(max_length=64)`
- `bucket_date`: `DateField`
- `source`: `CharField(max_length=32)`
- `selected_ndvi`: `FloatField(null=True, blank=True)`
- `smoothed_ndvi`: `FloatField(null=True, blank=True)`
- `confidence`: `FloatField`
- `confidence_components`: `JSONField(default=dict)`
- `quality_flags`: `JSONField(default=dict)`
- `is_null`: `BooleanField(default=False)`
- `null_reason`: `CharField(max_length=64, null=True, blank=True)`
- `created_at`: `DateTimeField(auto_now_add=True)`
- `updated_at`: `DateTimeField(auto_now=True)`

Constraints:

- Unique constraint on `v1_observation`
- Unique constraint on `(farm, engine, bucket_date)`
- Indexes on `(farm, engine, bucket_date)`, `(engine, confidence)`, `(source, bucket_date)`

## Transaction boundaries

- `NdviObservation` upsert: one atomic block per scene or per bucket batch.
- `NdviDerivedObservation` upsert: one atomic block per source observation.
- `NdviJob` state transitions: one atomic block around status change and lock refresh.
- `NdviRasterArtifact` write: one atomic block around content hash check and file save.

## Idempotency keys

- V1 ingest: `(farm_id, engine, bucket_date)` plus source scene identifier where available.
- V2 persist: `v1_observation_id`.
- Job queue: `request_hash`.
- Raster artifact: `(farm, engine, date, size, max_cloud)`.

