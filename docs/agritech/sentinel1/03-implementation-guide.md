# Sentinel-1 SAR Integration — Implementation Guide

**Document:** docs/agritech/sentinel1/03-implementation-guide.md  
**Status:** Implementation Guide  
**Target Phases:** All Phases (1 through 6)  

---

## Overview

This guide details the technical steps for executing all phases of the Sentinel-1 SAR integration, as outlined in the Architecture and Phased Delivery Plan documents.

---

## Phase 1: Foundations & Registries

### 1. Update the Band Registry
Modify `science/formulas/band_registry.py` to map the Sentinel-1 RTC polarizations.

```python
# science/formulas/band_registry.py

BAND_REGISTRY["sentinel1_rtc"] = {
    "vv": "vv",
    "vh": "vh",
}
```

### 2. Update the Formula Registry
Add the radar-based indices to `science/formulas/registry.py`. This ensures the `SpectralComputeEngine` can automatically process these equations.

```python
# science/formulas/registry.py

FORMULA_REGISTRY["RVI"] = {
    "name": "RVI",
    "formula": lambda vv, vh: (4 * vh) / (vv + vh) if (vv + vh) != 0 else float('nan'),
    "bands": ["vv", "vh"],
    "range": (0.0, 1.0),
    "default_colormap": "YlGn",
    "default_min": 0.0,
    "default_max": 0.8,
    "sensor_band_map": {
        "sentinel1_rtc": {"vv": "vv", "vh": "vh"},
    },
    "description": "Radar Vegetation Index for canopy structure monitoring.",
}

FORMULA_REGISTRY["S1_SMI"] = {
    "name": "S1_SMI",
    "formula": lambda vv, vh, alpha=0.70, beta=-0.30, gamma=0.50: alpha * vv + beta * vh + gamma,
    "bands": ["vv", "vh"],
    "range": (0.0, 1.0),
    "default_colormap": "Blues",
    "default_min": 0.0,
    "default_max": 1.0,
    "sensor_band_map": {
        "sentinel1_rtc": {"vv": "vv", "vh": "vh"},
    },
    "description": "Sentinel-1 Soil Moisture Index (estimated surface soil moisture).",
}
```

### 3. Database Migration for Models
Update the choices in `ndvi/models.py` for both `NdviObservation` and `NdviJob`.

```python
# ndvi/models.py
INDEX_CHOICES = [
    # ... existing optical choices ...
    ("RVI", "Radar Vegetation Index"),
    ("S1_SMI", "Sentinel-1 Soil Moisture Index"),
]
```
Execute Django commands to generate and apply migrations:
```bash
python manage.py makemigrations ndvi
python manage.py migrate ndvi
```

### 4. Create Calibration Configuration
Create the decoupled YAML configuration file for the site-specific empirical coefficients.

```yaml
# science/thresholds/s1_smi_calibration.yaml
s1_smi_coefficients:
  maize:
    sandy_clay_loam:
      ascending:  { alpha: 0.72, beta: -0.28, gamma: 0.45 }
      descending: { alpha: 0.68, beta: -0.32, gamma: 0.52 }
  default:
    ascending:  { alpha: 0.70, beta: -0.30, gamma: 0.50 }
    descending: { alpha: 0.70, beta: -0.30, gamma: 0.50 }
```

---

## Phase 2: Rust Preprocessing & Parallel Ingestion

### 1. Rust `ndvi-service` Microservice Updates

**A. COG Windowed Reading:**
Implement HTTP range requests using the `gdal` or `tiff` Rust crates to stream only the spatial bounding box intersecting the farm geometry.

**B. Radar Preprocessing Engine:**
Implement the mathematical processing steps in Rust to prevent memory spikes in the Django Celery workers.
* **Linear to dB Conversion:** Map backscatter values with $10 \cdot \log_{10}(intensity)$. Mask out exact `0` NoData border pixels to prevent domain errors.
* **Refined Lee Speckle Filter:** Implement a convolution kernel (3x3 or 5x5) using the `rayon` crate for multithreaded processing over spatial tiles.
* **Safety Boundaries:** Ensure NaNs propagate safely and `valid_pixel_fraction` thresholds (e.g., `< 0.70`) correctly flag invalid scene outputs.

### 2. Django Orchestration Updates

**A. StacDataProvider Integration:**
Update the provider (e.g., `ndvi/providers/stac.py`) to dispatch queries to the Copernicus Data Space API specifically for the `sentinel-1-rtc` collection. Ensure orbit direction metadata is parsed and stored.

**B. Proxy Delegation:**
Update the ingestion pipeline to route raw radar processing requests over HTTP to the local Rust microservice, passing bounding boxes and STAC URLs. Handle the response by executing the `SpectralComputeEngine`.

### 3. Celery Scheduled Tasks
Define periodic tasks in the configuration for daily retrieval and refreshing of radar metrics.

```python
# config/settings.py or celerybeat-schedule configs
CELERY_BEAT_SCHEDULE["refresh_daily_radar_indices"] = {
    "task": "ndvi.tasks.enqueue_daily_rvi_refresh",
    "schedule": crontab(hour=2, minute=0),  # Execute nightly
}
```

---

## Phase 3: Temporal Gap Filling

### 1. Gap-Fill Routing Logic
Create `science/fusion/radar.py` to handle the substitution logic. The system must query recent radar observations (`RVI` and `S1_SMI`) whenever there is a continuous block of missing optical data (e.g., `NDVI` or `NDMI`) exceeding a 7-day window.

### 2. API Timeseries Merging
Modify the Django view responsible for serving farm timeseries data.
* Detect optical gaps and dynamically query the radar proxy logic.
* Merge the data points on the temporal axis.
* Tag radar-injected data points with `source="radar_estimation"` in the JSON response payload.

### 3. Nextcloud UI Updates
Update the frontend charting components to overlay `RVI` onto `NDVI` and `S1_SMI` onto `NDMI`. 
* Use distinct rendering styles (e.g., dashed lines or hollow markers) to visually indicate that the data point is a radar-derived estimate rather than an optical measurement.

---

## Phase 4: Blended Optical/Radar Fusion

### 1. Fused Engine Implementation
Implement `science/fusion/fused_engine.py` to create a unified crop health metric.
* Use a weighted-averaging algorithm to combine structural optical canopy readings (NDVI) with volumetric microwave geometry (RVI).
* Normalize values dynamically using moving average bounds to account for diverging signal properties.

### 2. Dynamic Runtime Calibration
Update the engine to retrieve local farm soil moisture baselines and weather events (from the `weather-service`). 
* Apply adjustments to the `s1_smi_calibration.yaml` coefficients ($\alpha, \beta, \gamma$) programmatically at runtime to produce highly tailored metrics.

---

## Phase 5: Machine-Learning Crop Health (Deferred)

*This phase triggers only after 50+ active crop cycles have been monitored end-to-end to ensure sufficient training data.*

### 1. ML Regression Models
Develop regression models leveraging both optical and radar feature vectors (`NDVI`, `NDWI`, `RVI`, `S1_SMI`) across time.
* Train models to predict harvest yields and automatically classify crop types based on temporal signatures.
* Expose these predictions via new Django API endpoints.

---

## Phase 6: Multi-Sensor Fusion (Deferred)

*This phase triggers alongside the integration of commercial datasets (like Planet scope).*

### 1. Comprehensive Physical Canopy Model
Integrate all available data sources into a single physical state model:
* **Optical:** Sentinel-2, Landsat-8, Planet
* **Radar:** Sentinel-1
* **Contextual:** High-resolution DEM elevations, Open-Meteo/NASA POWER precipitation and temperature history
* Generate 3D canopy models and moisture depth profiling based on combined multi-sensor analytics.
