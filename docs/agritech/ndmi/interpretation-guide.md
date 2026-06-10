# NDMI Interpretation Guide

## How to read NDMI values

NDMI values range from -1 to 1. The scale is not linear — the meaningful range for agricultural applications is typically -0.2 to 0.8.

### Quick reference (typical ranges — not universal thresholds)

| Value | Typical interpretation | Suggested action |
|-------|----------------------|------------------|
| 0.5 – 0.8 | Well-watered, healthy vegetation | No action needed |
| 0.3 – 0.5 | Adequate moisture, early season or low-density crop | Monitor weekly |
| 0.1 – 0.3 | Low moisture — possible stress | Inspect field within 2-3 days |
| 0.0 – 0.1 | Water stress likely | Irrigate if possible |
| < 0.0 | Severe stress or non-vegetated | Investigate immediately |

### Per-crop thresholds

Different crops have different baseline NDMI values:

| Crop | Healthy range | Stress threshold | Critical |
|------|-------------|-----------------|----------|
| Maize (corn) | 0.40 – 0.65 | < 0.30 | < 0.15 |
| Soybean | 0.45 – 0.70 | < 0.35 | < 0.20 |
| Wheat | 0.35 – 0.55 | < 0.25 | < 0.10 |
| Cotton | 0.30 – 0.60 | < 0.20 | < 0.10 |
| Rice (flooded) | 0.50 – 0.75 | < 0.35 | < 0.20 |
| Vineyards | 0.30 – 0.55 | < 0.20 | < 0.10 |
| Orchards | 0.35 – 0.60 | < 0.25 | < 0.15 |

These thresholds are guidelines. Local varieties, soil types, and climate can shift them.

## What high and low values mean

### High NDMI (> 0.5)

- Vegetation is well-hydrated
- Canopy is dense enough to produce strong NIR reflectance
- SWIR absorption is high because leaves contain abundant water
- Expected after rainfall or irrigation
- Can also occur in riparian vegetation, wetlands, or irrigated crops

### Moderate NDMI (0.3 – 0.5)

- Adequate moisture for normal function
- May indicate lower-density vegetation (early growth stage, sparse planting)
- May indicate moderate water stress beginning
- Requires context: is this normal for the growth stage?

### Low NDMI (0.0 – 0.3)

- Water stress is present
- Leaves are losing turgor, stomata may be closed
- Photosynthesis is impaired even if NDVI is still high
- Requires irrigation intervention if in a sensitive growth stage

### Negative NDMI (< 0.0)

- Severe water stress or non-vegetated surface
- Vegetation may be senesced, dormant, or dead
- Bare soil, fallow fields, and built surfaces also produce negative NDMI
- Check land cover classification before interpreting as stressed vegetation

## Common misconceptions

**Misconception 1: "High NDMI always means healthy crops."**

Not necessarily. High NDMI can occur on weedy fields (weeds have high moisture content), riparian buffers, or recently irrigated bare soil. Cross-reference with NDVI to confirm the signal comes from crop vegetation.

**Misconception 2: "Low NDMI always means drought."**

Not necessarily. Low NDMI can also indicate:
- Early growth stage with incomplete canopy cover
- Post-harvest residue
- Disease that affects water transport (e.g., fusarium, bacterial wilt)
- Root damage from pests or compaction
- Salinity stress (osmotic effect reduces water uptake)

**Misconception 3: "NDMI replaces field scouting."**

NDMI is an early warning system, not a standalone diagnostic. It indicates *that* a field has a water problem, but not *why*. Field scouting is still needed to determine the cause (irrigation system failure? Disease? Soil variability?).

**Misconception 4: "NDMI values are comparable across satellites."**

Partially true but requires caution. NDMI from Sentinel-2 (10m/20m resolution) will generally agree with Landsat (30m) on the same day for the same field, but systematic offsets can occur due to different spectral response functions. Trends over time are more reliable than absolute values.

## Seasonal effects on NDMI

### Spring
- NDMI rises as crops emerge and canopy develops
- Early-season values are low due to incomplete ground cover (bare soil signal mixed in)
- Soil moisture from snowmelt or spring rains may elevate NDMI on wet bare soil

### Summer
- Peak NDMI during vegetative growth and flowering
- Diurnal variation: NDMI is slightly lower in afternoon (transpiration reduces leaf water) than morning
- Summer storms cause temporary NDMI spikes (rain on canopy, then recovery as leaves dry)

### Autumn
- NDMI declines as crops senesce and dry down
- The rate of decline varies by crop: corn dry-down is faster than soybean
- Natural senescence produces a gradual, steady decline — abrupt drops indicate stress or disease

### Winter
- NDMI is low or negative on fallow fields and crop residue
- Evergreen crops (winter wheat, alfalfa) maintain positive NDMI but lower than summer
- Snow cover produces highly variable NDMI (SWIR responds differently to frozen water)

### Interpreting trends vs. absolute values

**Trends are more reliable than single values.** A single NDMI reading of 0.35 could be:
- A healthy wheat field in early grain fill
- A stressed corn field needing irrigation
- A vineyard with partial ground cover

But a trend — NDMI declining from 0.50 to 0.35 over 7 days — unambiguously indicates declining moisture. Always compare to the field's own recent history.
