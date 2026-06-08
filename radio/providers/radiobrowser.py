"""Radio Browser API provider.

Fetches stations from the community-maintained Radio Browser directory
(https://www.radio-browser.info/) which catalogs ~30k stations.
Open-licensed, no API key required.
"""

from __future__ import annotations

import requests

from radio.providers.base import RadioProvider, StationData
from radio.providers.registry import register_provider

RADIO_BROWSER_API_BASE = "https://de1.api.radio-browser.info/json"


class RadioBrowserProvider(RadioProvider):
    """Provider that fetches stations from the Radio Browser API.

    Returns the top stations by click count (popularity). Caches the
    API response on the instance for the lifetime of the object.
    """

    DEFAULT_LIMIT = 100

    def __init__(self, limit: int | None = None) -> None:
        self._limit = limit or self.DEFAULT_LIMIT
        self._stations: list[StationData] | None = None

    def get_stations(self) -> list[StationData]:
        if self._stations is not None:
            return self._stations
        self._stations = self._fetch_stations()
        return self._stations

    def _fetch_stations(self) -> list[StationData]:
        try:
            response = requests.get(
                f"{RADIO_BROWSER_API_BASE}/stations/search",
                params={
                    "limit": self._limit,
                    "hidebroken": "true",
                    "order": "clickcount",
                    "reverse": "true",
                },
                timeout=15,
            )
            response.raise_for_status()
            return self._parse_response(response.json())
        except requests.RequestException:
            return []

    def _parse_response(self, data: list[dict]) -> list[StationData]:
        stations: list[StationData] = []
        for item in data:
            sid = item.get("stationuuid", "")
            if not sid:
                continue
            stations.append(
                {
                    "id": f"radiobrowser_{sid}",
                    "name": (item.get("name", "") or "").strip(),
                    "genre": item.get("tags", "") or "",
                    "country": item.get("country", "") or "",
                    "language": item.get("language", "") or "",
                    "stream_url": (
                        item.get("url_resolved") or item.get("url") or ""
                    ),
                    "format": (item.get("codec", "") or "").upper(),
                    "bitrate": item.get("bitrate", 128) or 128,
                    "logo_url": item.get("favicon") or None,
                    "website_url": item.get("homepage") or None,
                }
            )
        return stations

    def get_stream_url(self, station_id: str) -> str:
        if not station_id.startswith("radiobrowser_"):
            raise ValueError(f"Not a Radio Browser station: {station_id}")
        stations = self.get_stations()
        for station in stations:
            if station["id"] == station_id:
                return station["stream_url"]
        raise ValueError(f"Station not found: {station_id}")


register_provider("radiobrowser", RadioBrowserProvider)
