"""Landsat Collection 2 SR NDVI engine adapter.

Provides NDVI from Landsat 8/9 Surface Reflectance as a fallback
when Sentinel-2 is unavailable or unreliable.

This is a stub implementation. Full upstream integration requires
configuring a Landsat data provider (USGS Earth Explorer, Google
Earth Engine, or similar).
"""

from __future__ import annotations

import logging
from datetime import date

from ndvi.engines.base import BBox, NDVIEngine, NdviPoint

logger = logging.getLogger(__name__)


class LandsatEngine(NDVIEngine):
    """Landsat Collection 2 SR NDVI engine (stub).

    Returns empty results by default. Override or extend to connect
    to an actual Landsat data provider.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url or "https://landsatlook.usgs.gov/"

    def get_timeseries(
        self,
        *,
        bbox: BBox,
        start: date,
        end: date,
        step_days: int,
        max_cloud: int,
    ) -> list[NdviPoint]:
        """Return NDVI points from Landsat over a date range.

        Stub: returns empty list. Implement upstream integration
        to return actual Landsat NDVI data.
        """
        logger.info(
            "landsat.get_timeseries bbox=%s start=%s end=%s "
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
        """Return the most recent Landsat NDVI point.

        Stub: returns None. Implement upstream integration to
        return actual Landsat NDVI data.
        """
        logger.info(
            "landsat.get_latest bbox=%s lookback_days=%s max_cloud=%s "
            "(stub: no upstream)",
            bbox,
            lookback_days,
            max_cloud,
        )
        return None
