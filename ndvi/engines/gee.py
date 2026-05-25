"""Google Earth Engine NDVI engine adapter.

Provides NDVI from Google Earth Engine (GEE) as a batch/backfill
engine for offline or historical processing.

This is a stub implementation. Full upstream integration requires
a GEE service account, the ``ee`` Python client, and appropriate
Earth Engine assets (e.g. Landsat, Sentinel-2, or MODIS collections).
"""

from __future__ import annotations

import logging
from datetime import date

from ndvi.engines.base import BBox, NDVIEngine, NdviPoint

logger = logging.getLogger(__name__)


class GeeEngine(NDVIEngine):
    """Google Earth Engine NDVI engine (stub).

    Returns empty results by default. Override or extend to connect
    to the Google Earth Engine API via the ``ee`` Python client.
    """

    def __init__(
        self,
        service_account: str | None = None,
        collection: str | None = None,
    ) -> None:
        self.service_account = service_account
        self.collection = collection or "COPERNICUS/S2_SR_HARMONIZED"

    def get_timeseries(
        self,
        *,
        bbox: BBox,
        start: date,
        end: date,
        step_days: int,
        max_cloud: int,
    ) -> list[NdviPoint]:
        """Return NDVI points from GEE over a date range.

        Stub: returns empty list. Implement upstream integration
        to query GEE via ``ee.ImageCollection`` and return
        computed NDVI statistics.
        """
        logger.info(
            "gee.get_timeseries bbox=%s start=%s end=%s "
            "step_days=%s max_cloud=%s (stub: no upstream)",
            bbox,
            start,
            end,
            step_days,
            max_cloud,
        )
        return []

    def get_latest(
        self,
        *,
        bbox: BBox,
        lookback_days: int,
        max_cloud: int,
    ) -> NdviPoint | None:
        """Return the most recent GEE NDVI point.

        Stub: returns None. Implement upstream integration to
        return the latest NDVI from the configured GEE collection.
        """
        logger.info(
            "gee.get_latest bbox=%s lookback_days=%s max_cloud=%s "
            "(stub: no upstream)",
            bbox,
            lookback_days,
            max_cloud,
        )
        return None
