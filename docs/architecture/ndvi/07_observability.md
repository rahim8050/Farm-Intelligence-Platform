# NDVI Observability

## Current Django modules

- `ndvi/metrics.py`
- `ndvi/tasks.py`
- `ndvi/circuit_breaker.py`
- `ndvi/retry_policy.py`
- `ndvi/management/commands/consume_ndvi_stream.py`
- `ndvi/views.py`

## Metrics already present

- `spectral_jobs_total{index,status,type,engine}`
- `spectral_upstream_requests_total{index,engine,outcome}`
- `spectral_upstream_latency_seconds{index,engine}`
- `spectral_cache_hit_total{index,level}`
- `spectral_farms_stale_total{index,engine}`
- `ndvi_circuit_breaker_state{engine}`
- `ndvi_circuit_breaker_transitions_total{engine,from_state,to_state}`

## Blueprint metrics to add for V2

- `ndvi_v2_observations_total{engine,source}`
- `ndvi_v2_null_outputs_total{engine,reason}`
- `ndvi_v2_confidence_bucket{engine,source,bucket}`
- `ndvi_source_usage_total{source,endpoint}`
- `ndvi_source_disagreement_total{pair}`
- `ndvi_v2_suppressed_observations_total{reason}`
- `spectral_backfill_rows_total{index,engine,status}`
- `ndvi_stream_dlq_total{consumer}`

## Logging fields

- `request_id`
- `job_id`
- `farm_id`
- `engine`
- `source`
- `scene_id`
- `bucket_date`
- `v1_observation_id`
- `v2_observation_id`
- `confidence`
- `cloud_fraction`
- `valid_pixel_fraction`
- `quality_flags`
- `decision`
- `decision_reason`
- `retry_count`
- `error_class`

## Traceability per observation

- Every V2 row must point to exactly one V1 row.
- Every farm-state output must be reproducible from a fixed V2 window.
- Every null response must include a machine-readable reason.
- Every stream message must carry a request hash and the job envelope.

## Transaction boundaries

- Emit success counters after the write transaction commits.
- Emit failure counters on caught exceptions before the task exits.
- Do not treat a provider retry as a successful observation.

## Operational thresholds

- Alert on rising null-output rate.
- Alert on source disagreement spikes.
- Alert when low-confidence outputs exceed the accepted rollout target.
- Alert when circuit breakers stay open longer than the expected recovery window.

