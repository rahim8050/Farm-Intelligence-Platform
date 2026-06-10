# NDWI Implementation Readiness

**Document:** 11-implementation-readiness.md
**Date:** 2026-06-09
**Status:** Final — ready for Phase 1 TDD

---

## 1. Final Package/Module Structure

All NDWI code lives inside the existing `ndvi/` Django app. No separate Django app, no separate namespace.

```
ndvi/
├── models.py                      # + index_type field on NdviObservation, NdviDerivedObservation, NdviJob, NdviRasterArtifact
├── stac_client.py                 # + load_ndwi_array() (new function, keeps load_ndvi_array() unchanged)
├── engines/
│   ├── base.py                    # unchanged (NdviPoint reused for NDWI)
│   ├── stac.py                    # + asset_green param, NDWI_FORMULA support
│   ├── sentinelhub.py             # + NDWI evalscript
│   ├── gee.py                     # + asset_green param
│   ├── landsat.py                 # + asset_green param
│   └── modis.py                   # unchanged (raises UnsupportedIndexError for NDWI)
├── services.py                    # + get_engine(index_type=) param, ndwi_* factories in ENGINE_FACTORIES
├── v2_quality.py                  # unchanged (NDVI quality)
├── quality_ndwi.py                # NEW — NDWI-specific quality (or parameterized QualityConfig)
├── fusion.py                      # unchanged (NDVI fusion)
├── fusion_ndwi.py                 # NEW — NDWI-specific fusion thresholds
├── views.py                       # + NdwiTimeseriesView, NdwiLatestView, NdwiRefreshView, NdwiRasterPngView, NdwiRasterQueueView
├── serializers.py                 # + NdwiObservationSerializer (reuses fields, differents default engine)
├── urls.py                        # + ndwi/ URL patterns
├── tasks.py                       # + run_ndwi_job, enqueue_daily_ndwi_refresh, enqueue_weekly_ndwi_gap_fill
├── metrics.py                     # + spectral_index_* metrics with index label (replaces ndvi_* gradually)
├── raster/
│   ├── png.py                     # + ndwi_to_png_bytes() with Blues colormap
│   ├── service.py                 # + render_ndwi_png()
│   └── registry.py                # + ndwi_* raster engine entries
└── tests/
    ├── test_engines_ndwi.py       # NEW — NDWI formula, SCL mask, factory tests
    ├── test_quality_ndwi.py       # NEW — NDWI confidence, outlier, null conditions
    ├── test_fusion_ndwi.py        # NEW — NDWI decision tree, conflict, fallback
    ├── test_views_ndwi.py         # NEW — NDWI endpoint tests
    ├── test_tasks_ndwi.py         # NEW — NDWI task tests
    ├── test_raster_ndwi.py        # NEW — NDWI raster tests
    ├── test_migrations.py         # + migration 0003 tests
    └── test_no_regression.py      # NEW — NDVI unchanged assertions
```

**Key principle:** No file in `ndvi/` that currently works for NDVI is renamed or restructured. All additions are either new files or additive changes to existing files.

---

## 2. Exact Model Changes

### NdviObservation (no rename)

```python
# New field — single additive change
index_type = models.CharField(
    max_length=16,
    choices=[("NDVI", "NDVI"), ("NDWI", "NDWI")],
    default="NDVI",
    db_index=True,
)
```

**Unique constraints** (drop old, add new with `index_type`):

```python
# Before:
UniqueConstraint(fields=["farm", "engine", "bucket_date", "version"],
                 name="uniq_ndvi_observation_farm_engine_bucket_version")

# After:
UniqueConstraint(fields=["index_type", "farm", "engine", "bucket_date", "version"],
                 name="uniq_ndvi_observation_per_index")
```

Same pattern for the other two unique constraints (latest, scene dedup).

**Indexes** (no change — existing indexes work for NDVI queries, partial indexes added for NDWI):

```python
# New partial indexes for NDWI query performance
models.Index(fields=["index_type", "farm", "bucket_date"],
             condition=Q(index_type="NDWI")),
models.Index(fields=["index_type", "engine", "bucket_date"],
             condition=Q(index_type="NDWI")),
```

Existing full-table indexes are not dropped. NDVI queries are unaffected.

### NdviDerivedObservation (no rename)

```python
# Same additive change
index_type = models.CharField(
    max_length=16,
    choices=[("NDVI", "NDVI"), ("NDWI", "NDWI")],
    default="NDVI",
)
```

**No field renames.** `selected_ndvi` stays `selected_ndvi`. `smoothed_ndvi` stays `smoothed_ndvi`. NDWI values are stored in the same float field — semantics differ but that's the consumer's responsibility.

### NdviJob (no rename)

```python
index_type = models.CharField(
    max_length=16,
    choices=[("NDVI", "NDVI"), ("NDWI", "NDWI")],
    default="NDVI",
)

# Job type choices grow:
job_type = models.CharField(
    max_length=32,
    choices=[
        # Existing
        ("refresh_latest", ...),
        ("gap_fill", ...),
        ("backfill", ...),
        ("raster_png", ...),
        # New NDWI
        ("ndwi_refresh_latest", ...),
        ("ndwi_gap_fill", ...),
        ("ndwi_backfill", ...),
        ("ndwi_raster_png", ...),
    ],
)
```

### NdviRasterArtifact (no rename)

```python
index_type = models.CharField(
    max_length=16,
    choices=[("NDVI", "NDVI"), ("NDWI", "NDWI")],
    default="NDVI",
)
```

### QuerySet changes

`ValidObservationQuerySet` gets optional `index_type` filter parameter. NDVI views continue to call `.valid()` without `index_type` (default behavior unchanged).

---

## 3. Exact Migration Strategy

### Migration 0003 — single migration, no renames

```python
from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [("ndvi", "0002_audio_alert_delivery_tracking")]

    operations = [
        # 1. Add index_type to all 4 models (additive, default="NDVI")
        migrations.AddField(
            model_name="ndviobservation",
            name="index_type",
            field=models.CharField(
                choices=[("NDVI", "NDVI"), ("NDWI", "NDWI")],
                default="NDVI",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="ndviderivedobservation",
            name="index_type",
            field=models.CharField(
                choices=[("NDVI", "NDVI"), ("NDWI", "NDWI")],
                default="NDVI",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="ndvijob",
            name="index_type",
            field=models.CharField(
                choices=[("NDVI", "NDVI"), ("NDWI", "NDWI")],
                default="NDVI",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="ndvirasterartifact",
            name="index_type",
            field=models.CharField(
                choices=[("NDVI", "NDVI"), ("NDWI", "NDWI")],
                default="NDVI",
                max_length=16,
            ),
        ),

        # 2. Drop old constraints
        migrations.RemoveConstraint(
            model_name="ndviobservation",
            name="uniq_ndvi_observation_farm_engine_bucket_version",
        ),
        migrations.RemoveConstraint(
            model_name="ndviobservation",
            name="uniq_ndvi_latest_observation",
        ),
        migrations.RemoveConstraint(
            model_name="ndviobservation",
            name="uniq_ndvi_scene_per_farm_engine",
        ),

        # 3. Add new constraints with index_type
        migrations.AddConstraint(
            model_name="ndviobservation",
            constraint=models.UniqueConstraint(
                fields=["index_type", "farm", "engine", "bucket_date", "version"],
                name="uniq_ndvi_observation_per_index",
            ),
        ),
        migrations.AddConstraint(
            model_name="ndviobservation",
            constraint=models.UniqueConstraint(
                fields=["index_type", "farm", "engine", "bucket_date"],
                condition=models.Q(("is_latest", True)),
                name="uniq_ndvi_latest_per_index",
            ),
        ),
        migrations.AddConstraint(
            model_name="ndviobservation",
            constraint=models.UniqueConstraint(
                fields=["index_type", "farm", "engine", "source_scene_id", "provenance_hash"],
                condition=models.Q(("source_scene_id__isnull", False)),
                name="uniq_ndvi_scene_per_index",
            ),
        ),

        # 4. Add NDWI partial indexes
        migrations.AddIndex(
            model_name="ndviobservation",
            index=models.Index(
                fields=["index_type", "farm", "bucket_date"],
                condition=models.Q(("index_type", "NDWI")),
                name="ndvi_obs_ndwi_farm_date_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="ndviobservation",
            index=models.Index(
                fields=["index_type", "engine", "bucket_date"],
                condition=models.Q(("index_type", "NDWI")),
                name="ndvi_obs_ndwi_engine_date_idx",
            ),
        ),
    ]
```

**Rollback:** `migration migrate ndvi 0002` — drops `index_type` column from all tables, drops NDWI partial indexes, restores old constraints (via `migration unapply`).

**Execution time:** Expected < 2s on production. Adding nullable/default columns in PostgreSQL is metadata-only.

---

## 4. Metric Strategy Decision

**Decision: Unified `spectral_index_*` with `index` label.**

All existing `ndvi_*` metrics gain an `index` label. New code uses `spectral_index_*` naming. Migration path:

| Phase | NDVI metrics | NDWI metrics |
|-------|-------------|-------------|
| During NDWI implementation | `ndvi_*{index="NDVI"}` (unchanged) | `spectral_index_*{index="NDWI"}` (new) |
| After NDWI ships (future) | `ndvi_*{index="NDVI"}` deprecated | `spectral_index_*` is canonical |

**Why now, not later:**
- 37 duplicate metric definitions eliminated
- Single Grafana panel works for all indices (filter by `index` label)
- No dashboard migration needed — existing NDVI dashboards filter on metric name `ndvi_*`, which continues to work
- Adding EVI later is just `spectral_index_*{index="EVI"}` — no new metric definitions

**Implementation:**
```python
# metrics.py
from prometheus_client import Counter

spectral_jobs_total = Counter(
    "spectral_jobs_total",
    "Jobs processed per index",
    ["index", "status", "type", "engine"],
)

# NDVI continues to increment the same metric:
spectral_jobs_total.labels(index="NDVI", status="success", ...).inc()

# NDWI increments with different label:
spectral_jobs_total.labels(index="NDWI", status="success", ...).inc()
```

---

## 5. Cache Strategy

| Aspect | Decision |
|--------|----------|
| Key prefix | `ndwi:cache:` (distinct from `ndvi:cache:`) |
| Timeseries TTL | 86400s (24h) — same as NDVI |
| Latest TTL | 21600s (6h) — same as NDVI |
| Cooldown prefix | `ndwi:refresh:` (distinct from `ndvi:refresh:`) |
| Cooldown TTL | 900s (15min) — same as NDVI |
| Raster cache prefix | `ndwi:raster:ptr:` (distinct from `ndvi:raster:ptr:`) |
| Raster cache TTL | 86400s (24h) — same as NDVI |

**Key format:**
```
ndwi:cache:v2:ts:{owner_id}:{farm_id}:{engine}:{start}:{end}:{step_days}:{max_cloud}
ndwi:cache:v2:latest:{owner_id}:{farm_id}:{engine}:{lookback_days}:{max_cloud}
ndwi:refresh:throttle:{user_id}:{farm_id}
ndwi:raster:ptr:{farm_id}:{engine}:{date}:{size}:{max_cloud}
```

**Isolation:** No shared prefix with NDVI. Zero risk of cache poisoning.

---

## 6. Celery Strategy

| Aspect | Decision |
|--------|----------|
| Queues | Share NDVI queues (`ndvi_ingestion`, `ndvi_recompute`, `ndvi_analysis`) |
| Task naming | `run_ndwi_job` (not `run_index_job`) |
| Task class | New `@shared_task` alongside existing NDVI tasks |
| Retry policy | Same as NDVI (max_retries=3, default_retry_delay=60) |
| Daily refresh schedule | `enqueue_daily_ndwi_refresh` at 06:00 UTC (staggered from NDVI 00:00) |
| Weekly gap fill | `enqueue_weekly_ndwi_gap_fill` on Sunday 06:00 UTC |
| Queue isolation | **Deferred.** Separate queues when NDWI traffic exceeds 50% of NDVI. |

**Why share queues:**
- Simpler deployment — no new worker configuration
- NDWI traffic is initially low (same farm count as NDVI)
- Index filtering at task level (`index_type` on `NdviJob`)
- Easy to split later (add routing key per job_type)

---

## 7. Process for Adding NDMI/EVI/SAVI After NDWI

### Template for Index N+1

After NDWI, adding the next spectral index (e.g., EVI) follows this checklist:

| Step | What | Effort |
|------|------|--------|
| 1 | Add `("EVI", "EVI")` to `index_type` choices in models | 5 min |
| 2 | Add `EVI_FORMULA` and band names (e.g., B02_10m for blue, B08_10m for NIR, B04_10m for red) | 1 day |
| 3 | Add engine factories: `_build_evi_stac_engine()`, etc. + register in `ENGINE_FACTORIES` | 1 day |
| 4 | Add views/serializers (`/api/v1/farms/<id>/evi/...`) by copying the NDWI pattern | 1 day |
| 5 | Add quality config (EVI-specific thresholds) | 1 day |
| 6 | Add fusion config (EVI-specific decision tree) | 1 day |
| 7 | Add raster colormap (e.g., green-to-brown for EVI) | 0.5 day |
| 8 | Add tasks (`run_evi_job`, schedule) + metrics fire automatically | 1 day |
| 9 | Tests | 2 days |
| **Total** | | **~8 days** |

### What the NDWI platform provides to Index N+1

- Model with `index_type` discriminator (no migration needed)
- STAC client with `load_index_array(formula=...)` pattern
- Engine parameterization (just pass band names + formula)
- View dispatch pattern (URL prefix → `index_type` mapping)
- Cache isolation (automatic via prefix)
- Celery task pattern (staggered schedule)
- Metrics with `index` label (auto-instruments)
- Test fixtures and mock patterns

---

## 8. Files Modified Per Phase

### P1: Model + Engine (1.5 weeks)

| File | Change |
|------|--------|
| `ndvi/models.py` | Add `index_type` to 4 models, update constraints/indexes |
| `ndvi/stac_client.py` | Add `load_ndwi_array()` (keeps `load_ndvi_array()` unchanged) |
| `ndvi/engines/stac.py` | Add `asset_green` param, `NDWI_FORMULA` |
| `ndvi/engines/sentinelhub.py` | Add NDWI evalscript |
| `ndvi/engines/gee.py` | Add `asset_green` param |
| `ndvi/engines/landsat.py` | Add `asset_green` param |
| `ndvi/engines/modis.py` | Add `UnsupportedIndexError` for NDWI |
| `ndvi/services.py` | `get_engine(index_type=)`, add `ndwi_*` factories to `ENGINE_FACTORIES` |
| `ndvi/tests/test_engines_ndwi.py` | NEW — NDWI formula validation |
| `ndvi/tests/test_migrations.py` | NEW/update — migration 0003 tests |

### P2: API Layer (1 week)

| File | Change |
|------|--------|
| `ndvi/views.py` | Add `NdwiTimeseriesView`, `NdwiLatestView`, `NdwiRefreshView`, `NdwiRasterPngView`, `NdwiRasterQueueView` |
| `ndvi/serializers.py` | Add `NdwiObservationSerializer` with `engine` default `ndwi_stac` |
| `ndvi/urls.py` | Add `ndwi/` URL patterns |
| `ndvi/services.py` | Add cache helpers with `ndwi:cache:` prefix |
| `ndvi/tests/test_views_ndwi.py` | NEW — smoke tests, auth, cache, cooldown |

### P3: Processing Pipeline (1 week)

| File | Change |
|------|--------|
| `ndvi/quality_ndwi.py` | NEW — NDWI confidence scoring, outlier detection, null conditions |
| `ndvi/fusion_ndwi.py` | NEW — NDWI decision tree, conflict detection, water classification |
| `ndvi/raster/png.py` | Add `ndwi_to_png_bytes()` with Blues colormap |
| `ndvi/raster/service.py` | Add `render_ndwi_png()` |
| `ndvi/raster/registry.py` | Add `ndwi_*` raster engine entries |
| `ndvi/tests/test_quality_ndwi.py` | NEW |
| `ndvi/tests/test_fusion_ndwi.py` | NEW |
| `ndvi/tests/test_raster_ndwi.py` | NEW |

### P4: Operationalize (1 week)

| File | Change |
|------|--------|
| `ndvi/tasks.py` | Add `run_ndwi_job`, `enqueue_daily_ndwi_refresh`, `enqueue_weekly_ndwi_gap_fill` |
| `ndvi/metrics.py` | Add `spectral_index_*` metrics with `index` label |
| `config/settings.py` | Add Celery Beat schedule entries |
| `ndvi/tests/test_tasks_ndwi.py` | NEW |
| `ndvi/tests/test_no_regression.py` | NEW — NDVI metric names unchanged, NDVI endpoints work |
| Grafana JSON | Add NDWI dashboard panels (export to repo) |

---

## 9. Rollback Plan Per Phase

### P1 rollback

```bash
python manage.py migrate ndvi 0002  # Drops index_type column, restores old constraints
```

**Data loss:** None. Existing NDVI rows unaffected. `index_type` column dropped cleanly.

**Verification:** NDVI endpoints must return identical results to pre-migration baseline.

### P2 rollback

```bash
# Remove URL patterns (git revert)
git revert HEAD  # or manually remove ndwi/ URL patterns
```

**Data loss:** None. No DB changes in P2.

**Verification:** `GET /api/v1/farms/<id>/ndwi/timeseries/` returns 404.

### P3 rollback

```bash
# Remove NDWI quality/fusion/raster modules (git revert)
git revert HEAD
```

**Data loss:** If NDWI observations were written, they remain in DB with `index_type="NDWI"` but no quality processing or fusion.

**Verification:** NDWI quality metrics stop incrementing.

### P4 rollback

```bash
# Remove Celery Beat entries, remove task functions
git revert HEAD
# Also: delete any NDWI rows if needed
python manage.py shell -c "NdviObservation.objects.filter(index_type='NDWI').delete()"
```

**Data loss:** NDWI data can be cleaned up with the query above. NDVI data untouched.

**Full system rollback:** `git revert <merge-commit>` + `migrate ndvi 0002` (if P1 migration needs rolling back).

---

## 10. Final Go/No-Go Recommendation

### Go Conditions

All of the following must be true before Phase 1 TDD begins:

- [ ] **Design review approved** — simplifications from `10-design-review.md` accepted
- [ ] **Migration rehearsed** — `migrate ndvi 0003` runs < 2s on staging copy of production DB
- [ ] **NDVI baseline confirmed** — all NDVI endpoints return identical results on staging before/after migration dry-run
- [ ] **Metric decision locked** — `spectral_index_*` with `index` label (not `ndwi_*`)
- [ ] **No-rename rule confirmed** — no table renames, no model renames, no field renames
- [ ] **Co-location confirmed** — all code in `ndvi/` app (no separate Django app)

### Verdict

**GO — Ready for Phase 1 TDD.**

| Phase | Earliest start | Latest finish | Dependencies |
|-------|---------------|---------------|-------------|
| P1: Model + Engine | Day 1 | Day 11 | Migration rehearsal complete |
| P2: API Layer | Day 8 | Day 15 | P1 complete |
| P3: Processing | Day 12 | Day 19 | P2 complete |
| P4: Operationalize | Day 18 | Day 25 | P3 complete |
| **Ship** | | **Day 32** | All phases + 1 week buffer |

**32 calendar days** to production NDWI from TDD start. Total engineering effort: ~4.5 weeks.
