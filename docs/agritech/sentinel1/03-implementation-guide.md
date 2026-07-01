# Sentinel-1 SAR Integration — Implementation Guide

**Document:** docs/agritech/sentinel1/03-implementation-guide.md  
**Status:** Implementation Guide  
**Target Phases:** Phase 1 & Phase 2  

---

## Overview

This guide details the technical steps for executing **Phase 1** (Foundations & Registries) and **Phase 2** (Rust Preprocessing & Parallel Ingestion) of the Sentinel-1 SAR integration, as outlined in the Architecture and Phased Delivery Plan.

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
**Next Steps:**
Once Phase 1 and 2 are fully implemented and passing unit tests, validation against in-situ soil moisture and optical canopy checks (Phase 3 and 4) will commence.
