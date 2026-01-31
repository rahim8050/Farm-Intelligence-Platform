# Fix STAC raster_png on Copernicus

## Root cause

- Copernicus STAC items expose band assets with suffixed names like
  `B04_10m`/`B08_10m`, while the NDVI raster engine was looking only for
  `B04`/`B08`.
- The Django settings module did not surface `NDVI_STAC_COLLECTION` and
  `NDVI_STAC_ASSET_*`, so configuration could not map to Copernicus defaults.

## Required config

Set these environment variables (dotenv format):

```dotenv
NDVI_STAC_COLLECTION=sentinel-2-l2a
NDVI_STAC_ASSET_RED=B04_10m
NDVI_STAC_ASSET_NIR=B08_10m
```

## Restart steps

After updating the environment, restart the Django web process and Celery
workers/beat so the new env values are loaded. For example:

1) Restart the web server (gunicorn/uvicorn/runserver).
2) Restart `celery worker` and `celery beat` (or your process manager units).
