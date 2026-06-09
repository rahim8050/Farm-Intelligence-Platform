# NDWI Data Model

**Document:** 02-data-model.md
**Stage:** Design (pre-implementation)
**Status:** Draft for review

---

## Overview

The NDWI data model reuses the same table structure as NDVI via a shared `SpectralObservation` model with an `index_type` discriminator field. No new database tables are created for NDWI raw observations. NDWI-specific quality (V2) and raster artifacts use index-scoped tables.

## Models

### `SpectralObservation` (renamed from `NdviObservation`)

The existing `NdviObservation` model gains an `index_type` field. The table name changes from `ndvi_ndviobservation` to `ndvi_spectralobservation`.

| Field | Type | Notes |
|-------|------|-------|
| `index_type` | `CharField(max_length=16, choices=["NDVI", "NDWI", ...])` | **NEW** — discriminator. Default `"NDVI"` for existing rows. |
| `farm` | `ForeignKey(Farm, ...)` | Unchanged |
| `engine` | `CharField(max_length=64)` | Unchanged. For NDWI, values like `"ndwi_stac"`, `"ndwi_sentinelhub"`. |
| `bucket_date` | `DateField()` | Unchanged |
| `mean` | `FloatField()` | Stores NDWI value (range [-1, 1]). Semantics differ from NDVI. |
| `min` | `FloatField(null=True)` | Unchanged |
| `max` | `FloatField(null=True)` | Unchanged |
| `sample_count` | `IntegerField(null=True)` | Unchanged |
| `cloud_fraction` | `FloatField(null=True)` | Unchanged |
| `valid_pixel_fraction` | `FloatField(null=True)` | Unchanged |
| `quality_flags` | `JSONField(default=dict)` | Same flags + `water_detected` (SCL class 6) — already tracked |
| `version` | `CharField(max_length=32)` | Default `"v1-legacy"` |
| `state` | `CharField(max_length=16)` | Unchanged |
| `is_latest` | `BooleanField(default=True)` | Unchanged |
| `acquired_at` | `DateTimeField(null=True)` | Unchanged |
| `computed_at` | `DateTimeField(null=True)` | Unchanged |
| `ingested_at` | `DateTimeField(null=True)` | Unchanged |
| `source_scene_id` | `CharField(max_length=256, null=True)` | Unchanged |
| `provenance` | `JSONField(default=dict)` | Unchanged |
| `provenance_hash` | `CharField(max_length=64, null=True, db_index=True)` | Unchanged |

#### Updated Constraints

```python
# Constraint 1: Unique per index/farm/engine/date/version (was: per farm/engine/date/version)
models.UniqueConstraint(
    fields=["farm", "engine", "bucket_date", "version", "index_type"],
    name="uniq_observation_per_index_farm_engine_bucket_version",
)

# Constraint 2: One latest per index/farm/engine/date
models.UniqueConstraint(
    fields=["farm", "engine", "bucket_date", "index_type"],
    condition=Q(is_latest=True),
    name="uniq_observation_latest_per_index",
)

# Constraint 3: Dedup by scene per index/farm/engine
models.UniqueConstraint(
    fields=["farm", "engine", "source_scene_id", "provenance_hash", "index_type"],
    condition=Q(source_scene_id__isnull=False),
    name="uniq_scene_per_index_farm_engine",
)
```

#### Updated Indexes

All existing indexes gain an `index_type` leading column (or a partial index on `index_type="NDWI"`):

```python
indexes = [
    models.Index(fields=["index_type", "farm", "bucket_date"]),
    models.Index(fields=["index_type", "engine", "bucket_date"]),
    models.Index(fields=["index_type", "farm", "engine", "bucket_date", "is_latest"]),
    models.Index(fields=["index_type", "version", "engine"]),
    models.Index(fields=["index_type", "state", "engine"]),
    models.Index(fields=["index_type", "acquired_at", "engine"]),
    models.Index(fields=["index_type", "source_scene_id", "engine"]),
]
```

### `SpectralDerivedObservation` (renamed from `NdviDerivedObservation`)

V2 quality output model. Gains `index_type` discriminator.

| Field | Change |
|-------|--------|
| `index_type` | **NEW** — `CharField(max_length=16)` with choices |
| `v1_observation` | FK to `SpectralObservation` (renamed) |
| `selected_ndvi` → `selected_index_value` | Renamed | Renamed for generality |
| `smoothed_ndvi` → `smoothed_index_value` | Renamed | Renamed for generality |

### `IndexJob` (renamed from `NdviJob`)

Async job envelope. Gains `index_type` discriminator.

| Field | Change |
|-------|--------|
| `index_type` | **NEW** — `CharField(max_length=16)` |
| `job_type` | Choices grow: `ndwi_refresh_latest`, `ndwi_gap_fill`, `ndwi_backfill`, `ndwi_raster_png` |
| `engine` | Values grow: `ndwi_stac`, `ndwi_sentinelhub`, etc. |

### `IndexRasterArtifact` (renamed from `NdviRasterArtifact`)

Persisted PNG artifact. Gains `index_type` discriminator.

| Field | Change |
|-------|--------|
| `index_type` | **NEW** — `CharField(max_length=16)` |
| `image` | Upload path changes to `ndwi/rasters/%Y/%m/%d/` |

## Migration Plan

### Migration 0003 (non-negotiable rename + discriminator)

```python
# Pseudocode — full migration TBD during implementation
class Migration(migrations.Migration):
    dependencies = [("ndvi", "0002_audio_alert_delivery_tracking")]

    operations = [
        # 1. Add index_type with default
        migrations.AddField(
            model_name="ndviobservation",
            name="index_type",
            field=models.CharField(
                max_length=16,
                default="NDVI",
                choices=[("NDVI", "NDVI"), ("NDWI", "NDWI")],
            ),
        ),
        # 2. Same for NdviDerivedObservation, NdviJob, NdviRasterArtifact
        # 3. Rename constraints (drop old, create new with index_type)
        # 4. Rename indexes (drop old, create new with index_type lead column)
        # 5. Rename tables
        migrations.AlterModelTable("NdviObservation", "ndvi_spectralobservation"),
        migrations.AlterModelTable("NdviDerivedObservation", "ndvi_spectralderivedobservation"),
        migrations.AlterModelTable("NdviJob", "ndvi_indexjob"),
        migrations.AlterModelTable("NdviRasterArtifact", "ndvi_indexrasterartifact"),
        # 6. Rename Python model names
        migrations.RenameModel("NdviObservation", "SpectralObservation"),
        migrations.RenameModel("NdviDerivedObservation", "SpectralDerivedObservation"),
        migrations.RenameModel("NdviJob", "IndexJob"),
        migrations.RenameModel("NdviRasterArtifact", "IndexRasterArtifact"),
    ]
```

### Rollback Considerations

| Scenario | Rollback action | Data loss? |
|----------|----------------|------------|
| Migration fails during `add_field` | Drop `index_type` column | No — default `"NDVI"` already set |
| Migration fails during table rename | Tables still have old names | No |
| NDWI data written and rollback needed | Delete rows where `index_type="NDWI"`, drop column | NDWI data only |
| NDWI data written and rollback needed after model rename | Revert model names, keep `index_type` column | NDWI data lost, NDVI intact |

### Partitioning Consideration

If the `SpectralObservation` table grows past 100M rows, consider partitioning by `index_type`:

```sql
CREATE TABLE ndvi_spectralobservation (
    -- same columns
) PARTITION BY LIST (index_type);

CREATE TABLE ndvi_observations_ndvi PARTITION OF ndvi_spectralobservation
    FOR VALUES IN ('NDVI');

CREATE TABLE ndvi_observations_ndwi PARTITION OF ndvi_spectralobservation
    FOR VALUES IN ('NDWI');
```

This is **deferred** — not part of the initial migration. Existing rows would need to be moved into partitions, which requires downtime or a shadow-table approach.
