# NDMI Scientific Background

## Formula

NDMI is calculated as:

```
NDMI = (NIR - SWIR) / (NIR + SWIR)
```

Where:
- **NIR** = near-infrared reflectance (0.8–0.9 µm)
- **SWIR** = short-wave infrared reflectance (1.6–1.7 µm, specifically SWIR1)

The result is a dimensionless value between -1 and 1.

## Meaning of NIR and SWIR bands

### Near-Infrared (NIR)

NIR radiation interacts strongly with leaf cellular structure. Healthy plant cells have air-water interfaces in their mesophyll tissue that reflect NIR strongly. When a plant loses water, the cell structure collapses and NIR reflectance decreases. This is why NIR is used in nearly all vegetation indices.

Key property: NIR reflectance is high for healthy vegetation (40–50%), low for dry soil (15–25%), and very low for water (1–5%).

### Short-Wave Infrared (SWIR)

SWIR radiation is absorbed by water molecules. Liquid water in leaves absorbs SWIR strongly — the more water a leaf contains, the less SWIR it reflects. When a leaf dries out, SWIR reflectance increases sharply.

This makes SWIR a **sensitive indicator of leaf water content**. Unlike NIR, which responds to cellular structure, SWIR responds primarily to the water molecules present in the leaf.

Key property: SWIR reflectance is low for hydrated vegetation (10–20%), high for dry vegetation (30–50%).

## Why SWIR is sensitive to vegetation moisture

The sensitivity of SWIR to water comes from the absorption spectrum of liquid water. Water molecules have strong absorption features at several wavelengths in the SWIR region:

- **1.45 µm** — strong water absorption band
- **1.94 µm** — very strong water absorption band
- **2.5 µm** — strong water absorption band

The SWIR1 band at 1.6 µm sits between these absorption features, in a region where water absorption is moderate but significant. This makes it sensitive to leaf water content without being so strongly absorbed that the signal saturates.

As a leaf loses water:
1. The amount of liquid water in the leaf decreases
2. SWIR absorption decreases
3. SWIR reflectance increases
4. The NDMI formula `(NIR - SWIR) / (NIR + SWIR)` produces a lower value

## SWIR sensitivity by target type

SWIR reflectance responds differently depending on what the sensor is observing:

| Target | SWIR sensitivity | Mechanism |
|--------|-----------------|-----------|
| Vegetation moisture | **Strong** — primary signal | Water in leaf tissue absorbs SWIR proportionally to water content |
| Soil moisture | Moderate / indirect | Water in soil pore spaces absorbs SWIR, but signal is weaker and confounded by soil mineralogy and texture |
| Open water | Strong — but spectrally distinct | Standing water absorbs nearly all SWIR, producing very low reflectance that is distinguishable from vegetation moisture by spatial context and NDWI cross-reference |

**Practical implication:** A low NDMI value could indicate dry vegetation, dry soil, or open water. Cross-referencing with NDWI and land cover classification disambiguates the source.

## Interpretation of NDMI values

The following ranges represent **typical values, not universal thresholds**. Actual values vary by crop type, growth stage, climate, and satellite sensor.

| NDMI range | Typical interpretation |
|------------|----------------------|
| 0.6 – 1.0 | Very high moisture content. Dense, well-watered vegetation (rainforest, irrigated crops at peak). |
| 0.3 – 0.6 | Moderate to high moisture. Healthy crops with adequate water. |
| 0.1 – 0.3 | Low moisture. Early drought stress. Crop may still look green (NDVI may still be high). |
| -0.1 – 0.1 | Very low moisture. Severe water stress. Vegetation may be senescing or dormant. |
| -1.0 – -0.1 | Dry or non-vegetated surfaces. Bare soil, dry vegetation, urban areas. |

See the [Interpretation Guide](./interpretation-guide.md) for crop-specific ranges and seasonal context.

## Key scientific references

- NDMI was originally developed as a variant of the Normalized Difference Water Index (NDWI) but using SWIR instead of green reflectance, making it more sensitive to vegetation moisture than to surface water.
- SWIR-based moisture indices correlate strongly with leaf water potential and stomatal conductance in field studies.
- NDMI is less affected by atmospheric effects than NDVI because both NIR and SWIR are influenced similarly by atmospheric scattering, and the ratio normalizes much of the noise.
