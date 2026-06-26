"""Abstract DataProvider protocol for spectral data sources.

All providers (STAC, SentinelHub, GEE, etc.) implement this protocol
so that ``SpectralComputeEngine`` can be provider-agnostic.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

import numpy as np

from ndvi.engines.base import BBox
from ndvi.stac_client import StacItem


@runtime_checkable
class DataProvider(Protocol):
    """Abstract interface to satellite data providers.

    Implementations wrap STAC APIs, Sentinel Hub, GEE, or any other
    source of spectral band imagery.
    """

    sensor_key: str
    """Identifier for the sensor (e.g. ``"sentinel2_l2a"``)."""

    def search(
        self,
        bbox: BBox,
        start: date,
        end: date,
        max_cloud: int,
    ) -> list[StacItem]:
        """Search for items matching the given spatio-temporal window."""
        ...

    def load_band(
        self,
        item: StacItem,
        band_asset_key: str,
        bbox: BBox,
    ) -> np.ndarray:
        """Load a single band array from a STAC item."""
        ...

    def get_latest(
        self,
        bbox: BBox,
        lookback_days: int,
        max_cloud: int,
    ) -> StacItem | None:
        """Return the most recent item within the lookback window."""
        ...
