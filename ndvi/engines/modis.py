"""MODIS NDVI engine adapter.

Provides NDVI from MODIS (Terra/Aqua) as a fallback for temporal
continuity when Sentinel-2 and Landsat are both unavailable.

This is a stub implementation. Full upstream integration requires
configuring a MODIS data provider (NASA LAADS, Google Earth Engine,
or similar).
"""

from __future__ import annotations

import logging
from datetime import date

from ndvi.engines.base import BBox, NDVIEngine, NdviPoint

logger = logging.getLogger(__name__)


class ModisEngine(NDVIEngine):
    """MODIS NDVI engine (stub).

    Returns empty results by default. Override or extend to connect
    to an actual MODIS data provider.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url or "https://ladsweb.modaps.eosdis.nasa.gov/"

    def get_timeseries(
        self,
        *,
        bbox: BBox,
        start: date,
        end: date,
        step_days: int,
        max_cloud: int,
    ) -> list[NdviPoint]:
        """Return NDVI points from MODIS over a date range.

        Stub: returns empty list. Implement upstream integration
        to return actual MODIS NDVI data.
        """
        logger.info(
            "modis.get_timeseries bbox=%s start=%s end=%s "
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
        """Return the most recent MODIS NDVI point.

        Stub: returns None. Implement upstream integration to
        return actual MODIS NDVI data.
        """
        logger.info(
            "modis.get_latest bbox=%s lookback_days=%s max_cloud=%s "
            "(stub: no upstream)",
            bbox,
            lookback_days,
            max_cloud,
        )
        return None
