# NDMI Interpretation Guide

## What NDMI Measures

The Normalized Difference Moisture Index (NDMI) measures vegetation moisture
content using the difference between Near-Infrared (NIR) and Short-Wave
Infrared (SWIR1) reflectance:

$$NDMI = \frac{NIR - SWIR1}{NIR + SWIR1}$$

- **NIR** is sensitive to vegetation structure and chlorophyll content.
- **SWIR1** is sensitive to water content in leaves and soil moisture.

High NDMI values indicate high moisture content; low values indicate dry
conditions or moisture stress.

## Typical Ranges

| NDMI Range       | Interpretation                   |
|------------------|----------------------------------|
| 0.4 to 1.0       | High moisture / saturated        |
| 0.2 to 0.4       | Adequate moisture                |
| -0.1 to 0.2      | Moderate / transitional          |
| -0.3 to -0.1     | Moisture stress                  |
| -1.0 to -0.3     | Severe moisture stress / dry     |

## Per-Crop Thresholds

### Maize (Corn)

| Growth Stage     | NDMI Range      | Interpretation                    |
|------------------|-----------------|-----------------------------------|
| Vegetative       | 0.3 - 0.6       | Normal moisture                   |
| Silking / Tassel | 0.2 - 0.5       | Normal — critical water demand    |
| Grain Fill       | 0.1 - 0.4       | Normal — < 0.2 indicates stress   |
| Maturity         | -0.1 - 0.2      | Natural drying                    |

**Irrigation trigger** for maize at grain-fill: NDMI < 0.2 for more than
3 consecutive observations. This indicates moisture stress that can reduce
yield if not addressed.

### Wheat

| Growth Stage     | NDMI Range      | Interpretation                    |
|------------------|-----------------|-----------------------------------|
| Tillering        | 0.2 - 0.5       | Normal moisture                   |
| Stem Extension   | 0.2 - 0.5       | Normal — critical water demand    |
| Heading / Flower | 0.2 - 0.5       | Normal — < 0.15 indicates stress  |
| Grain Fill       | 0.1 - 0.4       | Normal — natural drying           |

### Soybeans

| Growth Stage     | NDMI Range      | Interpretation                    |
|------------------|-----------------|-----------------------------------|
| Vegetative       | 0.3 - 0.6       | Normal moisture                   |
| Flowering        | 0.3 - 0.6       | Normal — critical water demand    |
| Pod Fill         | 0.2 - 0.5       | Normal — < 0.2 indicates stress   |
| Maturity         | -0.1 - 0.2      | Natural drying                    |

## Using NDMI for Irrigation Scheduling

1. **Establish baseline**: Collect 2-3 weeks of NDMI data after planting or
   at the start of the growing season to establish a per-field baseline.
2. **Monitor trend**: A sustained downward trend in NDMI over 5-7 days
   (3+ observations) indicates declining moisture.
3. **Trigger irrigation** when NDMI drops below the crop-specific stress
   threshold for the current growth stage.
4. **Verify recovery** by checking that NDMI rises after irrigation within
   24-48 hours (SWIR1 responds to changes in leaf water content).
5. **Avoid over-irrigation**: NDMI > 0.6 may indicate saturated conditions
   or standing water — check against field capacity and drainage.

## Using NDMI for Drought Monitoring

- **Early warning**: NDMI consistently below -0.1 for > 10 days across
  multiple fields indicates developing drought conditions.
- **Severity assessment**:
  - Mild: NDMI -0.1 to -0.2
  - Moderate: NDMI -0.2 to -0.3
  - Severe: NDMI < -0.3
- **Recovery monitoring**: After precipitation, NDMI should increase within
  2-5 days depending on soil type and drainage.

## Relationship Between NDMI and NDVI

NDMI and NDVI are complementary indices:

| Condition            | NDVI     | NDMI     | Interpretation                     |
|----------------------|----------|----------|------------------------------------|
| Healthy, well-watered| High     | High     | Optimal conditions                 |
| Green but stressed   | High     | Low-Mod  | Chlorophyll present but low moisture|
| Dormant / dry        | Low      | Low      | Senesced or dead vegetation        |
| Wet soil / water     | Low-Mod  | High     | Bare soil with high moisture       |

Key insight: **NDMI drops before NDVI** during moisture stress. Plants
close stomata and reduce transpiration (detectable by SWIR1) before
chlorophyll degradation (detectable by NDVI). This makes NDMI an earlier
indicator of crop water stress — typically 3-7 days ahead of NDVI decline.

## Known Limitations

- **SWIR sensitivity to soil moisture**: NDMI may show high values on bare
  wet soil, which is not vegetation moisture. Use a vegetation mask (NDVI > 0.2
  or similar) to filter non-vegetation pixels.
- **Shadow effects**: Cloud shadows and terrain shadows depress SWIR1,
  causing anomalously high NDMI values. Quality flags detect these conditions.
- **Saturation**: NDMI can saturate in dense, fully-wet canopies. Values
  above 0.8 should be treated as "very high moisture" without precise
  quantification.
- **Crop type specific**: Thresholds vary by crop type, growth stage, and
  local climate. The values in this guide are starting points — calibrate
  using field observations.

## Example

> **Scenario**: Maize field at grain-fill stage. NDMI readings over 5 days:
> 0.35, 0.32, 0.28, 0.24, 0.19.
>
> **Interpretation**: NDMI has declined from adequate to below the stress
> threshold (0.2) for maize at grain-fill. The trend is sustained (5
> observations over 5 days). Action: Irrigate immediately.
>
> **Expected recovery**: Within 48 hours of irrigation, NDMI should return
> to 0.25-0.35 range.
