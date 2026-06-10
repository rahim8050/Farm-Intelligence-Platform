# NDMI Agricultural Use Cases

## Drought detection

NDMI is one of the earliest remote-sensing indicators of agricultural drought. Because NDMI responds to leaf water content, it begins declining within days of the onset of water stress — before visible wilting or NDVI decline.

**Practical use:** A farm operations team monitors NDMI across all fields daily. A sustained NDMI decline over 3-5 days below a crop-specific threshold triggers a field inspection. This enables intervention (irrigation, drainage adjustment) before the crop shows visible stress.

**Example:** In a field of maize at the grain-fill stage:
- Day 1: NDMI = 0.52 (healthy)
- Day 3: NDMI = 0.44 (declining — possible stress onset)
- Day 5: NDMI = 0.35 (confirmed stress — irrigation triggered)
- Day 7: NDMI = 0.48 (recovery after irrigation)

NDVI on Day 5 would still read > 0.7 and show no sign of stress.

## Irrigation planning

NDMI supports precision irrigation by revealing within-field variability in water status that is not apparent from ground observation alone.

**Practical use:** An NDMI map of a center-pivot irrigated field shows a gradient from the pivot point (lower NDMI, drier) to the outer edge (higher NDMI, wetter). This indicates the pivot is under-watering the inner circles — a common sprinkler overlap issue. The farmer adjusts sprinkler nozzles to compensate.

**NDMI for irrigation scheduling:**
| NDMI trend | Action |
|-----------|--------|
| Stable or rising above threshold | No irrigation needed |
| Declining but above threshold | Monitor — irrigation may be needed in 2-3 days |
| Below threshold | Irrigate immediately |
| Below threshold and still declining despite irrigation | Investigate system malfunction or drainage issue |

## Vegetation water stress monitoring

NDMI can differentiate between types of water stress that appear similar in the field:

| Stress type | NDMI signature | NDVI signature |
|------------|---------------|----------------|
| Soil moisture deficit | Gradual decline over 5-10 days | Decline starts 3-7 days later |
| Root damage / disease | Rapid decline over 1-3 days | Normal or slightly declining |
| Over-irrigation / waterlogging | NDMI drops (roots cannot transpire) | Declines slowly |
| Nutrient deficiency | Normal NDMI for water status | Low NDVI |

This differentiation is difficult to achieve with ground observation alone and challenging with NDVI-only monitoring.

## Crop health interpretation over time

Tracking NDMI across a growing season reveals patterns that inform next season's management:

**Early season (emergence–vegetative):**
- NDMI reflects soil moisture availability to young plants.
- Low NDMI in early season may indicate poor emergence or dry seedbed.

**Mid season (flowering–grain fill):**
- NDMI is most sensitive during peak water demand.
- A mid-season NDMI dip below 0.3 for 5+ days in corn has been associated with 10–30% yield potential reduction.

**Late season (maturity–senescence):**
- NDMI declines naturally as plants senesce.
- The rate of decline indicates whether senescence is normal (gradual) or stress-induced (rapid).
- Abnormally rapid late-season NDMI decline suggests premature senescence from disease or severe drought.

**Post-harvest:**
- Residual NDMI on crop residue indicates residue moisture content, which affects tillage timing and residue decomposition.
