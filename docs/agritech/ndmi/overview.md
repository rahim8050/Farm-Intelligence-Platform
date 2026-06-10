# NDMI — Normalized Difference Moisture Index

## What is NDMI?

NDMI is a satellite-derived spectral index that serves as a proxy for vegetation moisture content. It uses the difference between near-infrared (NIR) and short-wave infrared (SWIR) reflectance to estimate how much water is present in plant leaves and canopies.

While NDVI measures vegetation greenness (chlorophyll activity) and NDWI detects surface water, NDMI is a proxy for the water content inside vegetation. This makes it a useful leading indicator of drought stress — often visible in NDMI days or weeks before it appears in NDVI.

## Why it matters in agriculture

Water stress is a common yield-limiting factor in rainfed and irrigated agriculture. NDMI offers distinct advantages over other indices:

1. **Early detection** — NDMI drops before NDVI shows stress, because plants close stomata and lose turgor before chlorophyll degrades.
2. **Irrigation scheduling** — NDMI trends can indicate whether a field is losing moisture faster than expected, supporting precision irrigation decisions.
3. **Drought quantification** — NDMI provides a continuous, repeatable estimate of vegetation moisture across entire farms.

## What problem it solves compared to NDVI and NDWI

| Index | Measures | Limitation |
|-------|----------|------------|
| NDVI | Chlorophyll activity / greenness | Stays high even when plants are water-stressed (green but wilting). Also saturates in dense canopies. |
| NDWI | Surface water / open water | Detects ponds, flooded fields, wet soil — not water *inside* plants. |
| NDMI | Vegetation moisture content (proxy) | Estimates leaf water content via SWIR absorption. Does not saturate in dense canopies the way NDVI does. Declines before NDVI during drought onset. |

NDMI fills a gap between "the field looks green" (NDVI) and "the field is flooded" (NDWI). It helps answer: *is the vegetation actually hydrated?*

## Relationship between the three indices

The three indices work as a complementary toolkit:

- **NDVI** tells you where vegetation is and how photosynthetically active it is.
- **NDWI** tells you where open water and saturated soil are.
- **NDMI** tells you how hydrated the vegetation is.

Typical patterns (values are illustrative):

- **Healthy, well-watered crop:** High NDVI (> 0.7), moderate NDWI (0.0–0.2), high NDMI (> 0.5).
- **Drought-stressed crop:** Declining NDMI first, followed by declining NDVI days-weeks later. NDWI may be negative or near-zero.
- **Flooded field:** Low NDVI (drowned vegetation), high NDWI (> 0.3), variable NDMI.
- **Irrigated field after watering:** NDMI rises within 24-48 hours as leaves rehydrate, before NDVI shows significant change.

Used together, these indices provide a more complete picture of crop water status than any single index alone.

## Limitations and Uncertainty

NDMI is a proxy for vegetation moisture, not a direct measurement. Multiple factors introduce uncertainty:

- **Atmospheric effects on SWIR** — Clouds, aerosols, and variable illumination can bias NDMI values. Heavy cloud cover during monsoon seasons may create data gaps of 5–14 days.
- **Crop-type variability** — Different crops have different baseline NDMI values (e.g., maize typically ranges 0.40–0.65, wheat 0.35–0.55). Thresholds that apply to one crop may not hold for another.
- **Seasonal variation** — NDMI varies naturally throughout the growing season. Early-season values are influenced by bare soil exposure, while late-season values decline with senescence. A value that indicates stress mid-season may be normal during dry-down.
- **Soil background influence** — Before canopy closure, soil reflectance contaminates the NDMI signal. Wet soil can elevate NDMI even without vegetation, while dry soil can depress it.
- **Resolution constraints** — SWIR bands are typically coarser than visible/NIR bands (e.g., 20m vs 10m on Sentinel-2), limiting detection of sub-field variability.

See the [Operational Trust Boundaries](./limitations.md#operational-trust-boundaries) section in the Limitations document for conditions where NDMI should not be relied upon.
