from __future__ import annotations

from abc import ABC, abstractmethod
from typing import NotRequired, TypedDict


class StationData(TypedDict):
    id: str
    name: str
    genre: str
    country: str
    language: str
    stream_url: str
    format: str
    bitrate: int
    logo_url: NotRequired[str]
    website_url: NotRequired[str]


class RadioProvider(ABC):
    """Abstract base class for radio providers."""

    @abstractmethod
    def get_stations(self) -> list[StationData]:
        """Return list of available stations."""
        pass

    @abstractmethod
    def get_stream_url(self, station_id: str) -> str:
        """Return stream URL for station."""
        pass

    def health_check(self, station_id: str) -> bool:
        """Check if station is available. Override for custom checks."""
        stations = self.get_stations()
        return any(s["id"] == station_id for s in stations)
