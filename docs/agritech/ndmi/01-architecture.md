# Farm Intelligence Platform — Spectral Analytics Architecture

**Document:** 01-architecture.md  
**Stage:** Production Readiness Review  
**Status:** Revised  
**Author:** Principal Platform Engineering  

---

## Executive Summary

This document revises the NDMI architecture from an index-specific feature addition into a generalized spectral analytics platform. NDMI is the first consumer of the improved infrastructure, not the purpose of it.

The design preserves backward compatibility for existing NDVI and NDWI endpoints, keeps the Django monolith for the foreseeable future, and introduces abstraction boundaries that allow the platform to grow to 10+ spectral indices without architectural rewrites.

---

## Table of Contents

1. [Architectural Direction](#1-architectural-direction)
2. [Formula Registry & Band Registry](#2-formula-registry--band-registry)
3. [Generic Spectral Compute Engine](#3-generic-spectral-compute-engine)
4. [Production Compute Architecture](#4-production-compute-architecture)
5. [Storage Architecture](#5-storage-architecture)
6. [Provenance & Scientific Auditability](#6-provenance--scientific-auditability)
7. [Multi-Level Caching](#7-multi-level-caching)
8. [Provider Abstraction & Failover](#8-provider-abstraction--failover)
9. [Operational Readiness](#9-operational-readiness)
10. [Tenant Isolation & SaaS Readiness](#10-tenant-isolation--saas-readiness)
11. [Event-Driven Evolution](#11-event-driven-evolution)
12. [Science vs Platform Separation](#12-science-vs-platform-separation)
13. [Deployment Topology](#13-deployment-topology)
14. [Migration Strategy](#14-migration-strategy)
15. [Implementation Phases](#15-implementation-phases)
16. [Risk Register](#16-risk-register)
17. [Production Readiness Checklist](#17-production-readiness-checklist)

---

## 1. Architectural Direction

### Principles

| Principle | Rationale |
|-----------|-----------|
| **Backward compatibility is non-negotiable** | Existing NDVI/NDWI clients must work without changes. Every abstraction we introduce must transparently support the current API contract. |
| **Evolution, not revolution** | The platform stays in the Django monolith. Extraction to independent services happens only when profiling proves a bottleneck. |
| **Science is separate from engineering** | Index formulas, quality rules, and thresholds live in a `science/` tree, not embedded in engine code. Engineers change infrastructure; scientists change formulas. |
| **Idempotent compute** | Every compute job produces the same output for the same input + version. Retries are safe. |
| **Configuration over code for new indices** | Adding a new index should require a YAML entry, not a Python branch. |

### Monolith Boundaries

```
┌─────────────────────────────────────────────────────────┐
│                    Django Monolith                        │
│                                                          │
│  ┌──────────────────────┐  ┌──────────────────────────┐  │
│  │    Platform Layer     │  │     Science Layer         │  │
│  │                       │  │                          │  │
│  │  api/                 │  │  science/                │  │
│  │  workers/             │  │    formulas/             │  │
│  │  storage/             │  │    quality/              │  │
│  │  queues/              │  │    fusion/               │  │
│  │  metrics/             │  │    thresholds/           │  │
│  │  providers/           │  │                          │  │
│  └──────────────────────┘  └──────────────────────────┘  │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │           Shared Models                             │  │
│  │  SpectralObservation, SpectralDerivedObservation,  │  │
│  │  IndexJob, IndexRasterArtifact                      │  │
│  └────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

**Extraction candidates (future, not now):**

| Service | Trigger condition |
|---------|------------------|
| Raster rendering worker | CPU-bound PNG generation blocks Celery |
| Provider proxy | Provider API rate limits become a bottleneck |
| Fusion service | Multi-source fusion logic outgrows single-thread model |
| Storage gateway | S3/MinIO access patterns require throttling |

---

## 2. Formula Registry & Band Registry

### Problem

The current codebase uses `if index_type == "NDWI"` branches in every engine, `stac_client.py`, and the view layer. Every new index adds more branches.

### Solution: Formula Registry

A declarative registry at `science/formulas/registry.py` defines every spectral index:

```python
# science/formulas/registry.py

IndexDefinition = TypedDict("IndexDefinition", {
    "name": str,                    # "NDMI"
    "formula": Callable,            # lambda nir, swir: (nir - swir) / (nir + swir)
    "bands": list[str],             # ["nir", "swir1"]
    "range": tuple[float, float],   # (-1.0, 1.0)
    "default_colormap": str,        # "YlOrRd"
    "default_min": float,           # -0.2
    "default_max": float,           # 0.8
    "sensor_band_map": dict,        # {"sentinel2": {"nir": "B08_10m", "swir1": "B11_20m"}, ...}
    "scl_mask": list[int],          # [0, 1, 2, 3, 8, 9, 10, 11]
    "description": str,
})

FORMULA_REGISTRY: dict[str, IndexDefinition] = {
    "NDVI": { "name": "NDVI", "formula": lambda nir, red: (nir - red) / (nir + red),
              "bands": ["nir", "red"], ... },
    "NDWI": { "name": "NDWI", "formula": lambda nir, green: (green - nir) / (green + nir),
              "bands": ["nir", "green"], ... },
    "NDMI": { "name": "NDMI", "formula": lambda nir, swir1: (nir - swir1) / (nir + swir1),
              "bands": ["nir", "swir1"],
              "sensor_band_map": {
                  "sentinel2": {"nir": "B08_10m", "swir1": "B11_20m"},
                  "landsat89": {"nir": "B5", "swir1": "B6"},
                  "modis": {"nir": "sur_refl_b02", "swir1": "sur_refl_b06"},
              }, ... },
    "EVI":  { ... },
    "SAVI": { ... },
    "NBR":  { ... },
}
```

### Band Registry

A companion registry maps abstract band names to concrete sensor-specific asset keys:

```python
# science/formulas/band_registry.py

BAND_REGISTRY: dict[str, dict[str, str]] = {
    "sentinel2_l2a": {
        "red": "B04_10m",
        "green": "B03_10m",
        "nir": "B08_10m",
        "swir1": "B11_20m",
        "swir2": "B12_20m",
        "scl": "SCL",
    },
    "landsat89_l2": {
        "red": "B4",
        "green": "B3",
        "nir": "B5",
        "swir1": "B6",
        "swir2": "B7",
    },
    "modis_09ga": {
        "nir": "sur_refl_b02",
        "swir1": "sur_refl_b06",
        "qa": "state_1km",
    },
}
```

### How engines consume these

Instead of `if index_type == "NDWI": use green_href`, an engine does:

```python
formula_def = FORMULA_REGISTRY[index_type]
band_map = BAND_REGISTRY[self.sensor_key]
required_bands = formula_def["bands"]  # ["nir", "swir1"]

for band_name in required_bands:
    asset_key = band_map[band_name]
    candidates = build_asset_candidates(asset_key)
    href = resolve_asset_href_candidates(item, candidates)
    # load href, compute...
```

**Result:** Adding a new index is a YAML/Python dict entry. No engine code changes. No `if` branches.

---

## 3. Generic Spectral Compute Engine

### Current state

Five engines (`StacEngine`, `SentinelHubEngine`, `GeeEngine`, `LandsatEngine`, `ModisEngine`) each implement `get_timeseries()` and `get_latest()` with duplicated search/windowing/retry logic.

### Target state

A single `SpectralComputeEngine` class parameterized by:

- `provider`: which data source to query
- `band_map`: which sensor/collection to use
- `formula`: the index formula to compute
- `mask_rules`: SCL or QA masking configuration

```python
class SpectralComputeEngine:
    """One engine to compute any spectral index from any provider."""

    def __init__(self, *, provider: DataProvider, formula: IndexDefinition):
        self.provider = provider
        self.formula = formula
        self.band_map = BAND_REGISTRY[provider.sensor_key]

    def compute(self, *, bbox, start, end, step_days, max_cloud) -> list[SpectralPoint]:
        items = self.provider.search(bbox, start, end, max_cloud)
        # ... shared search/windowing logic ...
        for item in items:
            bands = self._load_bands(item, bbox)
            index_array = self.formula["formula"](**bands)
            # ... shared stats logic ...
```

### Provider interface

```python
class DataProvider(Protocol):
    """Abstract over STAC APIs, SentinelHub, GEE, etc."""
    sensor_key: str
    def search(self, bbox, start, end, max_cloud) -> list[StacItem]: ...
    def load_band(self, item, band_asset_key, bbox) -> np.ndarray: ...
    def get_latest(self, bbox, lookback_days, max_cloud) -> StacItem | None: ...
```

### Backward compatibility

The existing per-index engines (`StacEngine`, `SentinelHubEngine`) are not removed. They are refactored to delegate to `SpectralComputeEngine` internally. The `ENGINE_FACTORIES` registry still works; it returns configured `SpectralComputeEngine` instances instead.

```python
def _build_ndmi_stac_engine() -> SpectralComputeEngine:
    return SpectralComputeEngine(
        provider=StacDataProvider(collection="sentinel-2-l2a"),
        formula=FORMULA_REGISTRY["NDMI"],
    )
```

#### Engine migration plan

| Step | Action | Risk |
|------|--------|------|
| 1 | Add `SpectralComputeEngine` alongside existing engines | None — no callers yet |
| 2 | Rewrite one engine (e.g., `LandsatEngine`) to delegate internally | Low — Landsat is least-used |
| 3 | Run `SpectralComputeEngine` + original engine in shadow mode | Low — only log diffs |
| 4 | Promote `SpectralComputeEngine` as the default; keep legacy path for degraded fallback | Medium |
| 5 | Remove per-engine classes after 1 release cycle | Low — remove dead code |

---

## 4. Production Compute Architecture

### Data flow

```
Client Request                           Satellite Overpass
      │                                         │
      ▼                                         ▼
┌──────────────┐                       ┌──────────────┐
│   API Layer   │                       │  Provider    │
│  (Django)     │                       │  Poller      │
│              │                       │  (Celery     │
│  /timeseries  │                       │   Beat)      │
│  /latest      │                       └──────┬───────┘
│  /refresh     │                              │
└──────┬───────┘                              │
       │                                       │
       │  Enqueue job (if stale)               │  Enqueue periodic refresh
       ▼                                       ▼
┌──────────────────────────────────────────────────────┐
│                   Celery Broker (Redis)                │
│   Queues: ingestion, quality, fusion, raster, default │
└──────┬───────────────────────┬───────────────────────┬─┘
       │                       │                       │
       ▼                       ▼                       ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│  Ingestion   │    │   Quality    │    │   Raster     │
│  Workers     │    │   Workers    │    │   Workers    │
│              │    │              │    │              │
│ Download     │    │ V2 scoring   │    │ PNG render   │
│ bands        │    │ Smoothing    │    │ COG to tile  │
│ Compute      │    │ Outlier      │    │ Thumbnail    │
│ index        │    │ detection    │    │ generation   │
│ Upsert obs   │    │ Fusion       │    │              │
└──────┬───────┘    └──────┬───────┘    └──────┬───────┘
       │                   │                   │
       ▼                   ▼                   ▼
┌──────────────────────────────────────────────────────┐
│                    Storage Layer                       │
│  PostgreSQL (observations, derived, jobs)              │
│  MinIO / S3 (COGs, rasters, thumbnails)                │
│  Redis (cache, locks, rate limits)                     │
└────────────────────────────────────────────────────────┘
```

### Celery topology

| Queue | Worker count | Priority | Responsibilities |
|-------|-------------|----------|------------------|
| `ingestion` | 4 | High | Download bands, compute index, upsert `SpectralObservation` |
| `quality` | 2 | Medium | V2 confidence scoring, temporal smoothing, outlier rejection |
| `fusion` | 1 | Low | Multi-source fusion, conflict resolution |
| `raster` | 2 | Low | COG → PNG rendering, thumbnail generation |
| `default` | 1 | Lowest | Housekeeping, cache warming, metric computation |

### Idempotency strategy

Each job carries a `provenance_hash` computed from:
```
SHA256(index_type + farm_id + engine + bucket_date + source_scene_id + formula_version)
```

Before processing, workers run:

```python
if SpectralObservation.objects.filter(provenance_hash=job.provenance_hash,
                                       state="FINAL").exists():
    logger.info("skipping duplicate job_id=%s hash=%s", job.id, job.provenance_hash)
    return
```

### Retry strategy

| Failure type | Retries | Backoff | Max interval | Dead letter |
|-------------|---------|---------|-------------|-------------|
| Provider timeout | 3 | Exponential (2s, 4s, 8s) | 60s | Discard after 2h |
| Provider 429 rate limit | 5 | Exponential + jitter | 300s | Re-queue after 4h |
| Transient DB error | 3 | Linear (1s, 2s, 3s) | 10s | Raise to alert |
| Band asset missing | 1 (check other providers) | None | 5s | Mark null, log |
| Permanent error | 0 | — | — | Raise to alert |

### Dead letter handling

Failed jobs after all retries are moved to a `dead_letter` Redis set keyed by `dead_letter:{queue_name}`. A Celery Beat task `replay_dead_letters` runs every 6 hours:

- Re-queues jobs that may succeed due to external recovery (provider back online)
- Jobs older than 72 hours are promoted to `SpectralObservation` with `state="DEAD_LETTER"` and an alert is fired

---

## 5. Storage Architecture

### Observation storage (PostgreSQL)

The existing `SpectralObservation` model is kept. Index type choices are expanded. No table changes beyond adding `"NDMI"` to the choices list.

### Raster storage (MinIO / S3)

**Canonical raster format:** Cloud Optimized GeoTIFF (COG).

**Storage layout:**

```
s3://spectral-data/
  {tenant_id}/
    {index_type}/
      raw/                              # Raw provider COGs (unmodified)
        {provider}/{year}/{scene_id}.tif
      computed/                         # Computed spectral index COGs
        {year}/{month}/{date}/
          {farm_id}_{bucket_date}_{engine}_{hash}.tif
      rasters/                          # PNG rasters (API-consumable)
        {year}/{month}/{date}/
          {farm_id}_{bucket_date}_{size}.png
          {farm_id}_{bucket_date}_{size}_thumb.png
      tiles/                            # Optional: XYZ tile cache
        {z}/{x}/{y}.png
```

**Pipeline:**

```
Raw Provider COG
  → Download & crop to bbox
  → Compute spectral index (array math)
  → Write computed COG (COG format, overviews)
  → PNG render (colormap applied to COG)
  → Upload to S3/MinIO
  → Record IndexRasterArtifact in DB
```

### COG generation

Computed COGs use:

```python
profile = {
    "driver": "GTiff",
    "dtype": "float32",
    "compress": "DEFLATE",
    "predictor": 3,  # floating-point predictor
    "tiled": True,
    "blockxsize": 256,
    "blockysize": 256,
    "BIGTIFF": "IF_SAFER",
    # Overviews at 2x, 4x, 8x for fast thumbnail generation
}
```

### Raster Request Flow

```
GET /api/v1/farms/{id}/ndmi/raster.png?date=2026-06-22
  │
  ├── Cache hit (Redis key)? → Return cached PNG bytes
  │
  └── Cache miss:
      ├── Check IndexRasterArtifact in DB
      │   ├── Found + fresh (< 24h old) → Serve from S3/MinIO → cache in Redis
      │   └── Not found or stale:
      │       ├── Check L1 in-process cache → miss
      │       ├── Check compute cache (S3/MinIO computed COG) → miss
      │       ├── Enqueue raster render job → return 202 Accepted
      │       └── Return "stale" cached version if available (serve degraded)
      └── Cache result in Redis (L2) with TTL = 15min
```

---

## 6. Provenance & Scientific Auditability

### Observation metadata

Every `SpectralObservation` row captures:

| Field | Example | Purpose |
|-------|---------|---------|
| `index_type` | `"NDMI"` | Which index |
| `engine` | `"ndmi_sentinelhub"` | Which engine produced it |
| `provider` | `"sentinelhub"` | Data source |
| `sensor` | `"sentinel-2-l2a"` | Satellite/sensor |
| `acquisition_time` | `2026-06-22T09:32:00Z` | When satellite acquired the scene |
| `computed_at` | `2026-06-22T10:15:00Z` | When the platform computed it |
| `source_scene_id` | `S2A_MSIL2A_20260622T...` | Original scene identifier |
| `formula_version` | `"1.0.0"` | Which formula/registry version |
| `engine_version` | `"2.3.1"` | Which engine code version |
| `provenance_hash` | `sha256:abc123...` | Deterministic hash for dedup |
| `provenance` | `{"processing_steps": [...], "software_versions": {...}}` | Full JSON provenance trail |
| `quality_flags` | `{"cloud_fraction": 0.05, "valid_pixel_fraction": 0.92, ...}` | Per-observation quality |

### Provenance JSON schema

```json
{
  "processing_steps": [
    {"step": "stac_search", "timestamp": "...", "duration_ms": 1200},
    {"step": "band_download", "timestamp": "...", "duration_ms": 3400, "bands": ["nir", "swir1"]},
    {"step": "index_compute", "timestamp": "...", "duration_ms": 800, "formula": "NDMI_v1"},
    {"step": "scl_mask", "timestamp": "...", "duration_ms": 200, "classes_masked": [0,1,2,3,8,9,10,11]}
  ],
  "software_versions": {
    "engine": "2.3.1",
    "formula_registry": "1.0.0",
    "stac_client": "1.5.0"
  }
}
```

### Audit support

- Every mutation creates a history row (via Django `django-simple-history` or similar)
- Provenance hash allows end-to-end reproduction: same inputs + same formula version = same output
- A `reproduce` admin action re-runs the compute for a given observation and diffs the result

---

## 7. Multi-Level Caching

### Cache layers

| Level | Location | Storage | TTL | Contents |
|-------|----------|---------|-----|----------|
| **L1** | In-process (Django instance) | `lru_cache` on engine factories | Process lifetime | Engine instances, band registries |
| **L2** | Redis | `{index_type}:cache:{farm_id}:{engine}:{date}:{size}` | 15 min | JSON observation lists, PNG byte blobs |
| **L3** | Object storage (S3/MinIO) | `{tenant}/{index_type}/computed/{date}/{farm_id}_{engine}_{hash}.tif` | Until replaced | Computed COGs, rendered rasters |
| **L4** | Provider retrieval | External API | N/A | Raw satellite data |

### Cache invalidation

| Event | Action |
|-------|--------|
| New observation ingested | Delete L2 key for that farm/engine/date. L3 COG is immutable (versioned by hash). |
| Manual refresh (`POST /refresh`) | Delete L2 keys for that farm. L3 stale version serves as fallback. |
| Freshness SLO exceeded (>24h) | L3 stale data is still served but with `Warning: stale` header. Refresh job enqueued. |
| Formula version bump | Recompute all cached L3 COGs with new version. Old COGs archived (not deleted). |

### Stale refresh behaviour

```
Client requests data for farm_id=42, date=2026-06-01
  │
  ├── L2 hit (Redis, < 15min old) → return immediately
  │
  └── L2 miss:
      ├── L3 hit (S3, computed COG exists)
      │   ├── COG age < 24h → render → serve → cache in L2
      │   └── COG age ≥ 24h → serve stale + enqueue refresh → cache in L2 with stale flag
      │
      └── L3 miss:
          ├── Request < 1h old → enqueue ingestion job → return 202
          └── Request ≥ 1h old → enqueue ingestion job → return 503 with retry-after header
```

---

## 8. Provider Abstraction & Failover

### Provider interface

```python
class DataProvider:
    """Abstract interface to satellite data providers."""

    name: str                          # "planetary_computer", "sentinelhub", "gee"
    sensor_key: str                    # "sentinel2_l2a", "landsat89_l2", "modis_09ga"
    priority: int                      # Lower = preferred (1: SentinelHub, 2: Planetary Computer, 3: GEE)

    @abstractmethod
    def search(...) -> list[StacItem]: ...
    @abstractmethod
    def load_band(...) -> np.ndarray: ...

    @abstractmethod
    def health(self) -> ProviderHealth: ...
```

### Provider configuration

```yaml
# config/providers.yaml (loaded via Django settings)

providers:
  sentinelhub:
    class: "ndvi.providers.sentinelhub.SentinelHubProvider"
    priority: 1
    rate_limit: 10  # requests per second
    circuit_breaker:
      failure_threshold: 5
      recovery_timeout: 60  # seconds
      half_open_max_requests: 2
    credentials:
      client_id: "${SENTINELHUB_CLIENT_ID}"
      client_secret: "${SENTINELHUB_CLIENT_SECRET}"

  planetary_computer:
    class: "ndvi.providers.stac.StacProvider"
    priority: 2
    rate_limit: 20
    circuit_breaker:
      failure_threshold: 10
      recovery_timeout: 30
      half_open_max_requests: 3
    config:
      base_url: "https://planetarycomputer.microsoft.com/api/stac/v1/"
      collection: "sentinel-2-l2a"

  gee:
    class: "ndvi.providers.stac.StacProvider"
    priority: 3
    rate_limit: 15
    circuit_breaker:
      failure_threshold: 8
      recovery_timeout: 45
      half_open_max_requests: 2
    config:
      base_url: "https://stac.dataspace.copernicus.eu/v1/"
      collection: "sentinel-2-l2a"
```

### Circuit breaker states

```
CLOSED (normal operation)
  → failure_threshold exceeded
  → OPEN (requests fail fast)
  → recovery_timeout elapsed
  → HALF_OPEN (probe request allowed)
    → success → CLOSED
    → failure → OPEN (reset timer)
```

### Fallback chains

Engines resolve providers in priority order:

```
For a Sentinel-2 NDMI request:
  1. sentinelhub (priority 1) — if circuit CLOSED
  2. planetary_computer (priority 2) — if sentinelhub circuit OPEN or rate limited
  3. gee (priority 3) — if planetary_computer also unavailable
  4. landsat 8/9 (cross-sensor fallback) — if all Sentinel-2 sources down
  5. modis (coarse-resolution fallback) — if all others unavailable
```

### Degraded operation modes

| Mode | Trigger | Behaviour |
|------|---------|-----------|
| **Graceful** | One provider circuit open | Requests fall through to next priority. Metrics mark `provider_unavailable`. |
| **Degraded** | All primary providers unavailable | Read from L3 cache only. API returns `Warning: stale data` header. No write operations. |
| **Minimum** | L3 cache miss + all providers down | API returns 503 with `Retry-After: 300`. Monitoring page shows provider status dashboard. |

---

## 9. Operational Readiness

### Prometheus metrics

```python
# Global counters
spectral_requests_total{index_type, engine, status}  # Per-index request counts
spectral_observations_ingested_total{index_type, engine, provider}
spectral_observations_null_total{index_type, null_reason}
spectral_compute_duration_seconds{index_type, engine, step}  # Histogram
spectral_cache_hit_ratio{index_type, cache_level}  # L2, L3 hit rates
spectral_provider_requests_total{provider, outcome}
spectral_provider_latency_seconds{provider}
spectral_provider_circuit_state{provider}  # 0=closed, 1=open, 2=half-open
spectral_job_queue_depth{queue}  # Ingestion, quality, fusion, raster
spectral_job_duration_seconds{queue, status}
spectral_job_retries_total{queue}
spectral_job_dead_letter_total{queue}

# Tenant-aware (when multi-tenant)
spectral_requests_total{tenant, index_type, status}
spectral_storage_bytes{tenant, storage_class}  # Observations, computed COGs, rasters
```

### Grafana dashboards

| Dashboard | Purpose | Key panels |
|-----------|---------|-----------|
| **Spectral Overview** | Global platform health | Request rate by index type, observation freshness, cache hit ratios, provider health |
| **NDMI Detail** | Index-specific | NDMI observation count, null rate, V2 confidence distribution, provider breakdown |
| **Provider Health** | Provider availability | Circuit breaker states, latency histograms, rate limit hits, error rates by provider |
| **Worker Fleet** | Celery worker status | Queue depths, job durations, retry rates, dead letter counts, worker saturation |
| **Tenant View** | Per-tenant (future) | Same as spectral overview but filtered by tenant_id |

### SLO definitions

| SLO | Target | Measurement | Alert threshold |
|-----|--------|-------------|-----------------|
| API availability (all indices) | ≥ 99.5% | `spectral_requests_total{status=~"5.."}` ratio | < 99.0% over 5min |
| Observation freshness (any index) | ≤ 24h from acquisition to API | `spectral_observations_ingested_total` max age | > 36h for any index |
| Computed data quality | ≥ 75% of observations pass V2 confidence ≥ 0.75 | `spectral_observations_null_total` / total | < 70% pass rate |
| NDMI-specific: freshness | ≤ 24h from Sentinel-2 overpass to API | `ndmi_observation_max_age_seconds` | > 48h |
| NDMI-specific: data quality | ≥ 70% of NDMI observations pass V2 confidence ≥ 0.70 | NDMI-specific null rate | < 60% pass rate |
| Cache hit ratio (L2) | ≥ 80% | `spectral_cache_hit_ratio{level="L2"}` | < 60% |
| Provider availability (all) | ≥ 99.0% | `spectral_provider_requests_total{outcome="success"}` ratio | < 95% for any provider |
| Raster PNG generation P95 | ≤ 10 seconds | `spectral_compute_duration_seconds{step="raster_render"}` | > 30s |
| Job dead letter rate | ≤ 0.1% of total jobs | `spectral_job_dead_letter_total` / `spectral_jobs_total` | > 1% |

### Tracing considerations

- Add `django-silk` for request-level profiling in non-production environments
- Structured logging with `structlog` — every log line includes `request_id`, `tenant_id`, `index_type`, `job_id`
- Trace provider requests with OpenTelemetry spans (future — manual instrumentation via contextvars for now)

### Structured logging format

```json
{
  "timestamp": "2026-06-22T10:15:00Z",
  "level": "INFO",
  "logger": "ndvi.workers.ingestion",
  "request_id": "req_abc123",
  "trace_id": "trace_xyz456",
  "tenant_id": "farm_42",
  "index_type": "NDMI",
  "job_id": "job_789",
  "engine": "ndmi_sentinelhub",
  "provider": "sentinelhub",
  "event": "observation.ingested",
  "duration_ms": 4200,
  "bands_loaded": ["nir", "swir1"],
  "source_scene_id": "S2A_20260622T093200",
  "bucket_date": "2026-06-22",
  "observation_id": 12345,
  "mean": 0.45,
  "sample_count": 850,
  "cloud_fraction": 0.05
}
```

---

## 10. Tenant Isolation & SaaS Readiness

### Tenant identifier propagation

| Layer | Propagation mechanism | Example |
|-------|----------------------|---------|
| **API** | JWT claim → request.user → farm owner | `request.user.farm_set.all()` |
| **Views** | `_get_farm()` extracts tenant from URL | `/api/v1/farms/{farm_id}/...` |
| **Cache keys** | Prefixed with tenant ID | `ndmi:cache:42:ndwi_stac:2026-06-01` |
| **Storage paths** | `s3://spectral-data/{farm_id}/NDMI/...` | `{farm_id}` in S3 path |
| **Queues** | Routing key includes tenant ID (Celery headers) | `{"tenant_id": 42}` in task headers |
| **Metrics** | `tenant` label on Prometheus metrics | `spectral_requests_total{tenant="42"}` |
| **Observations** | `farm` FK on `SpectralObservation` | `farm_id = 42` |
| **Database** | Row-level via `farm` FK | All queries scoped to user's farms |

### Migration path

| Stage | Tenant model | Isolation |
|-------|-------------|-----------|
| **Now** | Single tenant (one org) | Row-level via farm FK only |
| **Stage 1** | Multiple orgs, shared DB | `tenant_id` column, all queries filtered |
| **Stage 2** | Multiple orgs, schema-per-tenant | PostgreSQL schema per tenant |
| **Stage 3** | Multiple orgs, DB-per-tenant | Dedicated database per tenant |

Current implementation supports Stage 0 (farm FK). Moving to Stage 1 requires:

1. Add `Tenant` model and `tenant` FK on `Farm`
2. Add DB-level row filter via Django middleware
3. Update S3 paths to include tenant ID
4. Update cache keys to include tenant ID

This is deferred — implement only when the first multi-org customer is contracted.

---

## 11. Event-Driven Evolution

### Current state (Celery Beat)

```
Celery Beat (cron schedule)
  → enqueue_daily_ndmi_refresh (every 6h)
  → enqueue_ndmi_gap_fill (every 24h)
  → enqueue_raster_cleanup (every 1h)
```

### Target state (event-driven)

```
Satellite overpass
  → Provider webhook / STAC RSS feed
  → Event bus (Redis pub/sub or RabbitMQ)
  → Ingestion worker (opportunistic)
  → Quality worker (chained)
  → Fusion worker (chained)
  → Raster worker (on-demand)
```

### Migration path (no rewrite required)

| Phase | Change | Architecture |
|-------|--------|-------------|
| **1** (now) | Celery Beat drives everything | Scheduled polling |
| **2** | Add a `satellite_overpass` event channel that enqueues the same job type | Beat + event hybrid |
| **3** | Remove Beat schedules for indices with reliable event sources | Mostly event-driven |
| **4** | Replace Redis pub/sub with proper event bus if scale demands | Full event-driven |

Because ingestion tasks are already idempotent (provenance hash dedup), they can safely process the same work regardless of whether it arrived via Beat or an event. Both sources can coexist during migration.

### Event schema (interim — Redis pub/sub)

```python
{
    "event_type": "satellite.overpass",
    "provider": "sentinelhub",
    "sensor": "sentinel-2-l2a",
    "scene_id": "S2A_MSIL2A_20260622T093200",
    "bbox": {"west": ..., "south": ..., "east": ..., "north": ...},
    "acquisition_time": "2026-06-22T09:32:00Z",
    "cloud_cover": 5.2,
    "published_at": "2026-06-22T10:00:00Z",
}
```

---

## 12. Science vs Platform Separation

### Separation rationale

```
science/
  formulas/
    registry.py        # Formula definitions, band maps
    ndmi.py            # NDMI-specific constants (if needed beyond registry)
  quality/
    base.py            # Abstract quality scorer
    ndmi.py            # NDMI confidence rules
  fusion/
    base.py            # Abstract fusion engine
    ndmi.py            # NDMI fusion decision tree
  thresholds/
    ndmi.yaml          # Per-crop NDMI thresholds (maize: {healthy: 0.4, stress: 0.2})
    ndmi_crop_defaults.json

platform/
  api/
    views.py           # Generic spectral view (parameterized by index_type)
    serializers.py     # Observation serializers
    urls.py            # URL routing (auto-registered from registry)
  workers/
    ingestion.py       # Generic ingestion worker
    quality.py         # Generic quality worker
    fusion.py          # Generic fusion worker
    raster.py          # Generic raster render worker
  storage/
    cog.py             # COG read/write utilities
    s3.py              # MinIO/S3 abstraction
    cache.py           # Multi-level cache manager
  providers/
    base.py            # Abstract DataProvider
    stac.py            # STAC API provider
    sentinelhub.py     # Sentinel Hub provider
  queues/
    tasks.py           # Generic Celery task definitions
    routing.py         # Queue routing logic
  metrics/
    prometheus.py      # Metric definitions
```

**Why this separation matters:**

| Reason | Explanation |
|--------|-------------|
| **Science team autonomy** | Scientists edit formulas, quality rules, and thresholds without touching platform code. No Django deploy needed for threshold changes. |
| **Platform team autonomy** | Engineers refactor the platform (storage, caching, workers) without changing science behaviour. |
| **Test isolation** | Science tests run on synthetic data in milliseconds. Platform tests require infrastructure (Redis, MinIO, DB). |
| **Versioning** | Science and platform version independently. A formula registry change (`v1.1.0`) is decoupled from platform release (`v2.3.0`). |
| **Audit** | Each observation records `formula_version` and `engine_version`. Repro steps are explicit. |

---

## 13. Deployment Topology

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            Load Balancer                                 │
│                              (nginx / haproxy)                           │
└────────────────────────────────────────────┬────────────────────────────┘
                                             │
                    ┌────────────────────────┼────────────────────────┐
                    ▼                        ▼                        ▼
          ┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
          │   Django Web     │    │   Django Web     │    │   Django Web     │
          │   (gunicorn)     │    │   (gunicorn)     │    │   (gunicorn)     │
          │                  │    │                  │    │                  │
          │  API endpoints   │    │  API endpoints   │    │  API endpoints   │
          └────────┬─────────┘    └────────┬─────────┘    └────────┬─────────┘
                   │                       │                       │
                   └───────────────────────┼───────────────────────┘
                                           │
                    ┌──────────────────────┼──────────────────────┐
                    ▼                      ▼                      ▼
          ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────┐
          │     Redis        │  │    PostgreSQL    │  │   MinIO / S3         │
          │                  │  │                  │  │                      │
          │ • Cache (L2)     │  │ • SpectralObs    │  │ • Computed COGs      │
          │ • Celery broker   │  │ • DerivedObs     │  │ • PNG rasters        │
          │ • Rate limiter   │  │ • Jobs           │  │ • Raw provider COGs  │
          │ • Locks          │  │ • Farm/User data │  │ • Tiles              │
          └──────────────────┘  └──────────────────┘  └──────────────────────┘

          ┌──────────────────────────────────────────────────────────────────┐
          │                    Celery Worker Pool                            │
          │                                                                  │
          │  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐ │
          │  │ Ingestion  │  │  Quality   │  │  Fusion    │  │  Raster    │ │
          │  │ ×4 workers  │  │  ×2 workers │  │  ×1 worker │  │  ×2 workers │ │
          │  └────────────┘  └────────────┘  └────────────┘  └────────────┘ │
          └──────────────────────────────────────────────────────────────────┘

          ┌──────────────────────────────────────────────────────────────────┐
          │                    Monitoring Stack                              │
          │                                                                  │
          │  Prometheus ←── Django metrics exporter                          │
          │       │                                                          │
          │       └──→ Grafana (dashboards + alerts)                        │
          │       └──→ Alertmanager (PagerDuty / Slack)                      │
          └──────────────────────────────────────────────────────────────────┘
```

### Node sizing (single-tenant, initial deployment)

| Node type | CPU | RAM | Storage | Count |
|-----------|-----|-----|---------|-------|
| Django web | 4 vCPU | 8 GB | 50 GB | 2 (HA pair) |
| Celery worker (ingestion) | 4 vCPU | 16 GB | 100 GB | 2 |
| Celery worker (quality/fusion) | 2 vCPU | 8 GB | 50 GB | 1 |
| Celery worker (raster) | 4 vCPU | 16 GB | 200 GB (SSD temp) | 1 |
| PostgreSQL | 8 vCPU | 32 GB | 500 GB SSD | 1 (primary + replica) |
| Redis | 4 vCPU | 16 GB | 50 GB | 1 (cluster in future) |
| MinIO | 8 vCPU | 32 GB | 2 TB (scale-out) | 4 (distributed) |
| Prometheus + Grafana | 2 vCPU | 8 GB | 200 GB | 1 |

---

## 14. Migration Strategy

### Current state

```
NDVI → StacEngine → load_ndvi_array() → NdviObservation
NDWI → StacEngine(index_type="NDWI") → load_ndwi_array() → SpectralObservation(index_type="NDWI")
```

### Target state

```
Any index → SpectralComputeEngine → science/formulas/registry.py → SpectralObservation(index_type="*")
```

### Migration phases

| Phase | What changes | Risk | Rollback |
|-------|-------------|------|----------|
| **P0** | Add `FORMULA_REGISTRY`, `BAND_REGISTRY` as read-only data | None | Remove files |
| **P1** | Refactor `StacEngine._compute_stats()` to read from registries instead of `if index_type` | Low | Revert refactor, keep registries |
| **P2** | Replace engine Factory functions with `SpectralComputeEngine` wrappers | Medium | Keep old factory as fallback |
| **P3** | Shadow-run both paths for 1 week, compare outputs | Low | Only logging |
| **P4** | Remove dead `if index_type` branches, old engine classes | Low | Already shadow-verified |
| **P5** | Add NDMI as first consumer of the new architecture | Low | Remove NDMI entries |

### Rollback procedure

1. Revert `ENGINE_FACTORIES` to point at legacy engine classes
2. Old `if index_type` branches still exist in shadow mode during P2-P3
3. Run rollback script (`git revert`) + redeploy web workers
4. New NDMI `SpectralObservation` rows are deleted; NDVI/NDWI untouched

---

## 15. Implementation Phases

### Phase 0 — Platform foundations (2 weeks)

| # | Task | Dependencies |
|---|------|-------------|
| 0.1 | Create `science/formulas/registry.py` with NDVI, NDWI definitions | None |
| 0.2 | Create `science/formulas/band_registry.py` with sensor→band mappings | None |
| 0.3 | Add `DEFAULT_ASSET_SWIR1` constants and `asset_swir` params to existing engines | 0.2 |
| 0.4 | Add `load_ndmi_array()` function in `stac_client.py` | 0.2 |
| 0.5 | Add `NDMI_EVALSCRIPT` in `sentinelhub.py` | 0.2 |
| 0.6 | Add `ndmi_*` factory functions to `services.py` and `ENGINE_FACTORIES` | 0.4, 0.5 |
| 0.7 | Add `"NDMI"` to `SpectralObservation.index_type` choices | None |
| 0.8 | Register `ndmi/` URL prefix | 0.7 |

### Phase 1 — Generic compute engine (3 weeks)

| # | Task | Dependencies |
|---|------|-------------|
| 1.1 | Build `SpectralComputeEngine` base class | 0.1 |
| 1.2 | Build `StacDataProvider` (first `DataProvider` implementation) | 0.2 |
| 1.3 | Refactor one engine (e.g., LandsatEngine) to delegate to `SpectralComputeEngine` | 1.1, 1.2 |
| 1.4 | Shadow-run both paths in staging, log diffs | 1.3 |
| 1.5 | Promote `SpectralComputeEngine` to default for all engines | 1.4 |
| 1.6 | Remove old `if index_type` branches | 1.5 |

### Phase 2 — NDMI feature (2 weeks)

| # | Task | Dependencies |
|---|------|-------------|
| 2.1 | Add NDMI Celery Beat tasks | 0.6 |
| 2.2 | Create `science/quality/ndmi.py` | 0.1 |
| 2.3 | Create `science/fusion/ndmi.py` | 0.1 |
| 2.4 | Create NDMI raster colormap module | 0.1 |
| 2.5 | Add NDMI Prometheus metrics | 0.6 |
| 2.6 | Add NDMI Grafana dashboard panels | 2.5 |
| 2.7 | Document NDMI interpretation guide | 2.2 |
| 2.8 | Tune quality thresholds (2 weeks production data) | 2.2 |

### Phase 3 — Production hardening (2 weeks)

| # | Task | Dependencies |
|---|------|-------------|
| 3.1 | Multi-level caching (L1, L2, L3, L4) | 1.5 |
| 3.2 | Circuit breaker implementation on providers | 1.2 |
| 3.3 | Dead letter queue + replay | 1.1 |
| 3.4 | Structured logging rollout | 1.5 |
| 3.5 | SLO monitoring dashboards | 2.5 |
| 3.6 | Provenance tracking (history model, audit support) | 1.5 |

### Phase 4 — Future scale (deferred)

| # | Task | Trigger |
|---|------|---------|
| 4.1 | Tensor-based batch compute (vectorized over items) | 20+ farms in active ingestion |
| 4.2 | Tenant isolation Stage 1 (multi-org) | First multi-org customer |
| 4.3 | Event-driven ingestion (provider webhooks) | 10+ daily satellite passes per farm |
| 4.4 | Service extraction (raster, fusion separate processes) | Profiling confirms bottleneck |

---

## 16. Risk Register

| ID | Risk | Likelihood | Impact | Mitigation | Owner |
|----|------|-----------|--------|------------|-------|
| R01 | Formula registry abstraction adds latency vs hardcoded `if` branches | Medium | Low | Profile before/after. Registry lookup is O(1) dict lookup — negligible. |
| R02 | `SpectralComputeEngine` introduces regressions for existing NDVI/NDWI | Medium | High | Shadow-run both paths for 1 week. Compare pixel-level diffs. |
| R03 | SWIR band unavailable for requested date/bbox (cloud, processing gap) | High | Medium | Fallback to nearest available date. Gap-fill tasks. |
| R04 | NDMI quality thresholds produce >25% null rate | Medium | Medium | Conservative initial thresholds. 2-week tuning sprint. |
| R05 | Sentinel-2 B11 (20m) + B08 (10m) resampling introduces edge artifacts | Low | Medium | Validate against known-reference NDMI images. Resample NIR to 20m instead of SWIR to 10m. |
| R06 | Multi-level cache invalidation bugs serve stale data | Low | High | TTL-based expiry as safety net. `Warning` header on stale data. Manual flush admin action. |
| R07 | Celery worker queues back up during peak satellite overpass times | Medium | Medium | Separate queues per function. Autoscaling (via Celery worker count). Dead letter prevents infinite loops. |
| R08 | Provider circuit breaker false positive (transient glitch escalates) | Medium | Low | Half-open probe after recovery timeout. Exponential backoff with jitter. |
| R09 | Provenance hash collisions | Low | Low | SHA256 on 6+ fields. Log and alert on collision. |
| R10 | Core team unfamiliar with new architecture | Medium | Medium | Pair-program Phase 1. Document architecture. Internal RFC review. |

---

## 17. Production Readiness Checklist

### Architecture & Design

- [ ] Formula registry defined for NDVI, NDWI, NDMI
- [ ] Band registry defined for all supported sensors
- [ ] `SpectralComputeEngine` built and tested
- [ ] All existing NDVI/NDWI tests pass unmodified
- [ ] Shadow-run comparison shows < 0.1% pixel-level difference
- [ ] No `if index_type` branches remain in engine code

### Data & Storage

- [ ] PostgreSQL schema supports `index_type="NDMI"`
- [ ] S3/MinIO storage layout defined and implemented
- [ ] COG generation pipeline tested end-to-end
- [ ] Provenance fields populated on every observation
- [ ] Dedup by `provenance_hash` verified

### Caching

- [ ] L1 cache (lru_cache) on engine factories
- [ ] L2 cache (Redis) with TTL and stale serving
- [ ] L3 cache (S3/MinIO) with versioned COGs
- [ ] Cache invalidation tested for:
  - New observation ingestion
  - Manual refresh
  - Formula version bump
  - TTL expiry

### Operations

- [ ] All Prometheus metrics emitting
- [ ] Grafana dashboards built (Spectral Overview, NDMI Detail, Provider Health, Worker Fleet)
- [ ] SLO monitoring configured with alert thresholds
- [ ] PagerDuty/Slack alert routing defined
- [ ] Structured logging in JSON format
- [ ] Dead letter queue + replay automation tested

### Resilience

- [ ] Circuit breaker configured for each provider
- [ ] Fallback chains tested (provider → provider → cross-sensor → coarse)
- [ ] Degraded mode tested (L3 cache only, no providers)
- [ ] Minimum mode tested (503 with retry-after)
- [ ] All tasks idempotent (retry-safe)

### Security & Multi-tenancy

- [ ] Tenant identifier propagates through all layers
- [ ] Cache keys include tenant prefix
- [ ] S3 paths include tenant prefix
- [ ] Row-level access enforced by `farm` FK
- [ ] No cross-tenant data leakage possible

### Documentation

- [ ] Architecture document current
- [ ] API documentation generated (drf-spectacular)
- [ ] Interpretation guide published
- [ ] Runbook for degraded/minimum modes
- [ ] On-call playbook for provider outages

### Deployment

- [ ] Migration script tested on staging DB
- [ ] Rollback procedure documented and tested
- [ ] Canary deployment plan defined
- [ ] Performance baseline captured (latency P50/P95/P99, ingestion rate)
- [ ] Capacity plan for projected first 3 months
