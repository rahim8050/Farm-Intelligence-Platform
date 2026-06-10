# NDVI vs NDMI vs NDWI — Conceptual Comparison

## What each index measures

| Index | Measures | Physical basis | Best for |
|-------|----------|---------------|----------|
| NDVI | Chlorophyll activity / green biomass | NIR reflected by cell structure; Red absorbed by chlorophyll | Vegetation extent, crop health, yield estimation |
| NDMI | Vegetation moisture content (proxy) | NIR reflected by cell structure; SWIR absorbed by water | Drought detection, irrigation scheduling, water stress |
| NDWI | Surface water / open water | Green reflected by water; NIR absorbed by water | Flood detection, pond mapping, irrigation coverage |

## When each is used

| Scenario | Primary index | Supporting index |
|----------|--------------|------------------|
| **Crop health assessment** | NDVI | NDMI (helps distinguish water stress from other causes) |
| **Drought monitoring** | NDMI | NDVI (provides context on vegetation condition) |
| **Irrigation scheduling** | NDMI | NDWI (can indicate whether water reached the field) |
| **Flood mapping** | NDWI | NDMI (may show flooded vegetation stress) |
| **Yield prediction** | NDVI + NDMI combined | — |
| **Early stress detection** | NDMI | NDVI (responds days to weeks later) |
| **Drainage assessment** | NDWI (wet spots) + NDMI (waterlogged vegetation stress) | — |
| **Wildfire risk** | NDMI (fuel moisture proxy) | NDVI (fuel load proxy) |

## How farmers interpret differences

### Scenario: A corn field in July

| Observation | NDVI | NDWI | NDMI | Likely cause |
|------------|------|------|------|-------------|
| Dark green, no visible issues | 0.85 | 0.05 | 0.55 | Healthy, well-watered |
| Looks green, feels dry | 0.82 | 0.02 | 0.28 | **Early drought stress** — NDMI catches this first |
| Wilting visible | 0.65 | -0.05 | 0.12 | Severe drought — NDVI now also declining |
| Standing water after rain | 0.45 | 0.40 | 0.15 | Flooding — NDWI confirms, NDMI shows stress |
| Recently irrigated | 0.83 | 0.15 | 0.50 | NDWI spike from surface water, NDMI rising as leaves rehydrate |

### Scenario: Seasonal progression for irrigated wheat

| Month | NDVI | NDMI | Interpretation |
|-------|------|------|---------------|
| March (tillering) | 0.30 | 0.35 | Young crop, adequate moisture |
| April (stem elongation) | 0.60 | 0.50 | Rapid growth, well-watered |
| May (flowering) | 0.75 | 0.48 | Peak greenness, NDMI slightly dropping (normal water demand increase) |
| June (grain fill) | 0.72 | 0.32 | NDMI declining faster than NDVI — irrigation needed soon |
| July (senescence) | 0.40 | 0.15 | Both declining naturally |

### Common misinterpretation

**"NDMI is just a different version of NDVI."**

This is a misunderstanding. NDVI and NDMI measure different properties:

- NDVI can be high on a water-stressed crop that is still green (e.g., soybean with stomatal closure but green leaves).
- NDMI can be elevated on a non-vegetated surface that happens to be wet (e.g., wet bare soil after rain — SWIR responds to water in any medium, with strongest sensitivity to vegetation moisture, moderate sensitivity to soil moisture, and strong but spectrally distinct response to open water).

The two indices are complementary, not redundant. When NDVI and NDMI diverge (high NDVI + low NDMI), it may indicate a crop that appears healthy but is experiencing water stress — one of the most valuable early warnings that NDMI can provide.
