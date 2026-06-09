# NDWI Quality & Fusion

**Document:** 05-quality-fusion.md
**Stage:** Design (pre-implementation)
**Status:** Draft for review

---

## Rationale for Separate Module

NDWI quality scoring and fusion are **not** drop-in replacements for NDVI. While the mathematical framework (confidence = weighted sum of components) is identical, the **threshold values** differ because:

1. NDWI values cluster differently — open water gives NDWI ≥ 0.2, wet soil gives 0.0–0.2, dry vegetation gives negative values.
2. Temporal variability is higher for NDWI (irrigation cycles, rainfall events change water content rapidly).
3. Outlier detection needs different sensitivity — a sudden NDWI spike likely indicates irrigation or flooding (valuable signal, not noise).
4. Source reliability differs — Sentinel-2's 10m Green band is more informative for water detection than Landsat's 30m.

## Quality Architecture

### Source Weights

```python
NDWI_SOURCE_WEIGHTS = {
    "ndwi_stac": 1.00,
    "ndwi_sentinelhub": 1.00,
    "ndwi_gee": 1.00,
    "ndwi_landsat": 0.80,
}
```

MODIS is excluded from NDWI (no Green band in MODIS NDVI product).

### Confidence Component Weights

Same structure as NDVI but adjusted for NDWI's higher temporal variability:

```python
NDWI_CONFIDENCE_WEIGHTS = {
    "source": 0.30,
    "cloud": 0.20,        # Lower — NDWI works through thin cloud better than NDVI
    "valid_pixel": 0.20,  # Lower — water detection needs fewer pixels
    "recency": 0.15,      # Higher — NDWI changes faster (irrigation/rain)
    "temporal": 0.15,     # Higher — temporal consistency matters less for a fast-moving index
}
```

### Threshold Values (Initial)

| Threshold | NDVI value | NDWI value (initial) | Notes |
|-----------|-----------|---------------------|-------|
| Rolling window | 5 | 7 | NDWI more variable; larger window needed |
| Outlier threshold | 0.15 | 0.25 | NDWI jumps more; avoid false outliers |
| Accept threshold | 0.75 | 0.70 | Lower initial — NDWI naturally noisier |
| Low confidence | 0.50 | 0.45 | Slightly more tolerant |
| Valid pixel reject | 0.30 | 0.25 | Water detection tolerates more invalid pixels |
| Recency max days | 14 | 10 | NDWI changes faster; older data is less useful |
| Temporal deviation | 0.20 | 0.30 | Wider — NDWI jumps are often real signals |
| Min smooth values | 3 | 3 | Same |
| Min rolling context | 3 | 4 | Needs more context due to variability |
| Max confidence w/o context | 0.49 | 0.49 | Same |

### Confidence Scoring Formula

Identical to NDVI:

```python
confidence = (
    NDWI_CONFIDENCE_WEIGHTS["source"] * components.source_weight
    + NDWI_CONFIDENCE_WEIGHTS["cloud"] * components.cloud_weight
    + NDWI_CONFIDENCE_WEIGHTS["valid_pixel"] * components.valid_pixel_weight
    + NDWI_CONFIDENCE_WEIGHTS["recency"] * components.recency_weight
    + NDWI_CONFIDENCE_WEIGHTS["temporal"] * components.temporal_consistency_weight
)
```

### NDWI-Specific Null Return Conditions

Same order as NDVI but with NDWI-specific reasoning:

1. `valid_pixel_fraction < 0.25` — `"low_valid_pixel_fraction"` (more lenient than NDVI's 0.30)
2. `confidence < 0.45` — `"low_confidence"`
3. Raw NDWI is None — `"missing_ndwi_value"`
4. Acquisition_at is None — `"missing_acquisition_time"`
5. Prior V2 count < 4 AND engine not Sentinel-2 — `"insufficient_rolling_context"` (higher than NDVI's 3)
6. Outlier rejected — `"outlier_rejected"` (higher threshold: 0.25 vs NDVI's 0.15)

### Outlier Detection

```python
def is_ndwi_outlier(raw_ndwi, rolling_median, confidence, valid_pixel_fraction):
    """More conservative than NDVI — NDWI jumps are often real water events."""
    deviation = abs(raw_ndwi - rolling_median)
    if deviation < 0.25:  # Higher than NDVI's 0.15
        return False
    if confidence >= 0.70:  # Higher than NDVI's 0.75
        return False
    if valid_pixel_fraction is not None and valid_pixel_fraction >= 0.60:  # Lower than NDVI's 0.70
        return False
    return True
```

### Smoothed NDWI

Median of `[raw_ndwi] + prior_v2_values` (requires ≥ 3 total values, same as NDVI).

## Fusion Architecture

### Source Priority

```python
NDWI_SOURCE_PRIORITY = ["ndwi_stac", "ndwi_sentinelhub", "ndwi_gee", "ndwi_landsat"]
```

### Confidence Degradation

```python
NDWI_CONFIDENCE_DEGRADATION = {
    "ndwi_stac": 1.00,
    "ndwi_sentinelhub": 1.00,
    "ndwi_gee": 1.00,
    "ndwi_landsat": 0.90,
}
```

### Source Confidence Thresholds

```python
NDWI_SOURCE_CONFIDENCE_THRESHOLDS = {
    "ndwi_stac": 0.70,
    "ndwi_sentinelhub": 0.70,
    "ndwi_gee": 0.70,
    "ndwi_landsat": 0.65,
}
```

### Conflict Detection

```python
NDWI_CONFLICT_THRESHOLD = 0.15       # Higher than NDVI (0.10) — NDWI is more variable
NDWI_CONFLICT_CONFIDENCE_CAP = 0.70  # Lower than NDVI (0.75)
```

### Decision Tree

```python
def ndwi_select_by_decision_tree(candidates):
    """Identical structure to NDVI but with NDWI thresholds."""
    # 1. If exactly 1 primary (stac/sentinelhub/gee) with confidence >= 0.70 -> select
    # 2. Else if exactly 1 Landsat with confidence >= 0.65 -> select (fallback)
    # 3. Else sort by (confidence DESC, source_priority ASC), pick highest
    # 4. Tie-break: source priority order
    # 5. If no candidates -> NULL
```

## NDWI-Specific Interpretation Rules

### Water Classification (Post-Fusion)

```python
def classify_ndwi(value):
    if value >= 0.20:
        return "open_water"
    elif value >= 0.00:
        return "wet_soil"
    elif value >= -0.30:
        return "dry_soil"
    else:
        return "vegetation_dominated"
```

This classification is used for:
- Setting `quality_flags["ndwi_water_class"]`
- Alerting when a field transitions from "dry_soil" to "open_water" (potential flood)

### Sentinel-1 Integration

Same boundary as NDVI — Sentinel-1 can set `quality_flags["s1_wet_soil"]` and `quality_flags["s1_flooding"]` but never affects the NDWI value.

## Open Questions

1. Should the outlier threshold be per-region? (Irrigated fields have higher NDWI variance than rainfed.)
2. Should MODIS be supported via a different MODIS product (e.g., `MCD43A4` NIR + Green) rather than skipped?
3. Initial thresholds will be tuned after 2 weeks of production data — what is the acceptable null rate for farm ops?
