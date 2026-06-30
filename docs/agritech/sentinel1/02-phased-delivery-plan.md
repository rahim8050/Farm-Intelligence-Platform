# Sentinel-1 Radar Integration Phased Delivery Plan

**Document:** docs/agritech/sentinel1/02-phased-delivery-plan.md  
**Stage:** Design (pre-implementation)  
**Status:** Approved for implementation  

---

## Overview

The Sentinel-1 Radar integration is structured into 6 delivery phases. Phases 1 and 2 establish parallel radar index ingestion using the Rust microservices for preprocessing. Phase 3 implements gap-filling, and Phase 4 integrates optical-radar data fusion. Phases 5 and 6 are deferred for future scale (machine learning and multi-sensor fusion).

---

## Phase Breakdown

### Phase 1: Foundations & Registries (1 week)
*Status: Complete*

**Objective:** Map Sentinel-1 sensor bands and calculations in the registries and update database constraints.

**Deliverables:**
* **Band Registry:** Add `sentinel1_rtc` polarization mapping in [science/formulas/band_registry.py](file:///home/rahim/projects/Farm-Intelligence-Platform/science/formulas/band_registry.py).
* **Formula Registry:** Define `RVI` and `S1_SMI` math formulas in [science/formulas/registry.py](file:///home/rahim/projects/Farm-Intelligence-Platform/science/formulas/registry.py) with colormap and range settings.
* **Database Migration:** Generate and apply Django migrations adding `"RVI"` and `"S1_SMI"` to `NdviObservation.index_type` choices.
* **Calibration Config:** Create `science/thresholds/s1_smi_calibration.yaml` containing the initial crop- and soil-specific coefficients ($\alpha, \beta, \gamma$).

**Acceptance Criteria:**
* [ ] Database migration runs successfully and backward compatibility with NDVI/NDWI is maintained.
* [ ] Running `python manage.py check` reports no model or system issues.
* [ ] `mypy` runs clean on updated registry and model files.

---

### Phase 2: Rust Preprocessing & Parallel Ingestion (2 weeks)
*Status: Ready for implementation*

**Objective:** Implement the high-performance SAR preprocessing pipeline inside the Rust `ndvi-service` microservice and wire it to Django Celery workers.

**Deliverables:**
* **Rust COG Reader:** Implement windowed Cloud-Optimized GeoTIFF (COG) HTTP range-request reading using `gdal` or `tiff` crates in `ndvi-service`.
* **Rust Preprocessing Engine:**
  * Implement linear-to-decibel conversion ($dB = 10 \cdot \log_{10}(value)$).
  * Implement a 3x3/5x5 Refined Lee speckle filter parallelized using `Rayon` multithreading.
  * Implement cosine-based local incidence angle normalization.
  * Implement safety checks for divide-by-zero, `NaN` masking, and NoData boundary removal.
* **Django Orchestration integration:**
  * Update `StacDataProvider` to fetch `sentinel-1-rtc` scenes.
  * Delegate crop area backscatter arrays to the Rust microservice via HTTP payload.
  * Save the returned computed statistics in PostgreSQL with `index_type="RVI"` or `"S1_SMI"`.
* **Schedules:** Define `enqueue_daily_rvi_refresh` Celery Beat scheduler configurations in settings.

**Dependencies:** Phase 1 complete.

**Acceptance Criteria:**
* [ ] Rust unit tests verify the speckle filter kernel and linear-to-dB logs matches python mock matrices.
* [ ] Rust heap memory allocations remain $\le 64$ MB under load.
* [ ] Running `rvi-daily-refresh` schedules successfully enqueues and saves observations.
* [ ] Swagger/OpenAPI documentation shows `RVI` and `S1_SMI` response shapes correctly.

---

### Phase 3: Temporal Gap Filling (1.5 weeks)
*Status: Scoped*

**Objective:** Use the all-weather Sentinel-1 radar observations to fill temporal timeline gaps on the dashboard when optical sensors (NDVI, NDMI) are blinded by cloud cover.

**Deliverables:**
* **Gap-Fill Logic:** Create gap-fill routing in `science/fusion/radar.py`.
* **API timeseries merging:** Update Django timeseries views to return radar-derived estimates dynamically when optical observations are missing within a lookback window, flagging the data points with a `source="radar_estimation"` metadata tag.
* **Nextcloud UI Integration:** Update frontend charts to overlay RVI/S1-SMI data points onto the NDVI/NDMI timelines during cloudy intervals.

**Dependencies:** Phase 2 complete.

**Acceptance Criteria:**
* [ ] Timeseries API response merges radar and optical data points cleanly without duplicate timestamps.
* [ ] Front-end chart correctly displays optical data in primary colors and radar-derived gap-fillers in distinct dashed patterns.

---

### Phase 4: Blended Optical/Radar Fusion (2 weeks)
*Status: Scoped*

**Objective:** Build a unified fusion engine that blends structural optical canopy readings (NDVI) and volumetric microwave geometry (RVI) to generate a single, highly calibrated crop health curve.

**Deliverables:**
* **Fusion Engine:** Implement `science/fusion/fused_engine.py` using weight-averaging models to blend the indices.
* **Dynamic Calibration:** Read the local farm soil moisture baseline trends to dynamically calibrate the S1-SMI coefficients at runtime.

**Dependencies:** Phase 3 complete.

**Acceptance Criteria:**
* [ ] Fused curve output correlates with historical ground-truth moisture indicators with $R^2 \ge 0.85$ on clear-sky test scenes.

---

### Phase 5: Machine-Learning Crop Health (Deferred)
*Trigger:* 50+ active crop cycles monitored end-to-end.

**Objective:** Deploy machine learning regression models on fused S1/S2 time series to predict harvest yield and identify crop classification.

---

### Phase 6: Multi-Sensor Fusion (Deferred)
*Trigger:* Integration of Planet or Sentinel-3 thermal datasets.

**Objective:** Combine Sentinel-2, Sentinel-1, Landsat-8, Planet scope, weather station precipitation, and digital elevation models (DEM) into a physical canopy model.
