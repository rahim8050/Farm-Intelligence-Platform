import requests

from radio.providers.base import RadioProvider, StationData
from radio.providers.registry import register_provider


class TuneInProvider(RadioProvider):
    """TuneIn aggregator radio provider."""

    BASE_URL = "https://api.tunein.com"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or ""

    def get_stations(self) -> list[StationData]:
        if not self.api_key:
            return self._get_fallback_stations()
        return self._fetch_from_api()

    def _get_fallback_stations(self) -> list[StationData]:
        return [
            {
                "id": "tunein_bbc_ws",
                "name": "BBC World Service",
                "genre": "News, Talk",
                "country": "UK",
                "language": "English",
                "stream_url": "http://stream.live.vc.bbcmedia.co.uk/bbc_world_service",
                "format": "MP3",
                "bitrate": 96,
                "website_url": "https://www.bbc.com/worldservice",
            },
        ]

    def _fetch_from_api(self) -> list[StationData]:
        try:
            response = requests.get(
                f"{self.BASE_URL}/stations",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=10,
            )
            response.raise_for_status()
            return self._parse_api_response(response.json())
        except requests.RequestException:
            return self._get_fallback_stations()

    def _parse_api_response(self, data: dict) -> list[StationData]:
        stations: list[StationData] = []
        for item in data.get("Stations", []):
            stations.append(
                {
                    "id": f"tunein_{item.get('id', '')}",
                    "name": item.get("name", ""),
                    "genre": item.get("genre", ""),
                    "country": item.get("country", ""),
                    "language": item.get("language", "English"),
                    "stream_url": item.get("stream_url", ""),
                    "format": item.get("format", "MP3"),
                    "bitrate": item.get("bitrate", 128),
                }
            )
        return stations

    def get_stream_url(self, station_id: str) -> str:
        stations = self.get_stations()
        for station in stations:
            if station["id"] == station_id:
                return station["stream_url"]
        raise ValueError(f"Station not found: {station_id}")


register_provider("tunein", TuneInProvider)
