from radio.providers.base import RadioProvider, StationData
from radio.providers.registry import register_provider

SOMAFM_STATIONS: list[StationData] = [
    {
        "id": "somafm_groovesalad",
        "name": "SomaFM Groove Salad",
        "genre": "Ambient, Downtempo",
        "country": "USA",
        "language": "English",
        "stream_url": "https://ice.somafm.com/groovesalad",
        "format": "MP3",
        "bitrate": 128,
        "logo_url": "https://somafm.com/img3/groovesalad-200.jpg",
        "website_url": "https://somafm.com/groovesalad",
    },
    {
        "id": "somafm_dronezone",
        "name": "SomaFM Drone Zone",
        "genre": "Ambient, Atmospheric",
        "country": "USA",
        "language": "English",
        "stream_url": "https://ice.somafm.com/dronezone",
        "format": "MP3",
        "bitrate": 128,
        "logo_url": "https://somafm.com/img3/dronezone-200.jpg",
        "website_url": "https://somafm.com/dronezone",
    },
    {
        "id": "somafm_deepspaceone",
        "name": "SomaFM Deep Space One",
        "genre": "Ambient, Electronic",
        "country": "USA",
        "language": "English",
        "stream_url": "https://ice.somafm.com/deepspaceone",
        "format": "MP3",
        "bitrate": 128,
        "logo_url": "https://somafm.com/img3/deepspaceone-200.jpg",
        "website_url": "https://somafm.com/deepspaceone",
    },
    {
        "id": "somafm_spacestation",
        "name": "SomaFM Space Station Soma",
        "genre": "Ambient, Electronica",
        "country": "USA",
        "language": "English",
        "stream_url": "https://ice.somafm.com/spacestation",
        "format": "MP3",
        "bitrate": 128,
        "logo_url": "https://somafm.com/img3/spacestation-200.jpg",
        "website_url": "https://somafm.com/spacestation",
    },
    {
        "id": "somafm_secretagent",
        "name": "SomaFM Secret Agent",
        "genre": "Lounge, Spy",
        "country": "USA",
        "language": "English",
        "stream_url": "https://ice.somafm.com/secretagent",
        "format": "MP3",
        "bitrate": 128,
        "logo_url": "https://somafm.com/img3/secretagent-200.jpg",
        "website_url": "https://somafm.com/secretagent",
    },
    {
        "id": "somafm_defcon",
        "name": "SomaFM DEF CON Radio",
        "genre": "Electronic, Gaming",
        "country": "USA",
        "language": "English",
        "stream_url": "https://ice.somafm.com/defcon",
        "format": "MP3",
        "bitrate": 128,
        "logo_url": "https://somafm.com/img3/defcon-200.jpg",
        "website_url": "https://somafm.com/defcon",
    },
    {
        "id": "somafm_beatblender",
        "name": "SomaFM Beat Blender",
        "genre": "House, Electronica",
        "country": "USA",
        "language": "English",
        "stream_url": "https://ice.somafm.com/beatblender",
        "format": "MP3",
        "bitrate": 128,
        "logo_url": "https://somafm.com/img3/beatblender-200.jpg",
        "website_url": "https://somafm.com/beatblender",
    },
]


class SomaFMProvider(RadioProvider):
    """SomaFM independent radio provider."""

    def get_stations(self) -> list[StationData]:
        return SOMAFM_STATIONS

    def get_stream_url(self, station_id: str) -> str:
        for station in SOMAFM_STATIONS:
            if station["id"] == station_id:
                return station["stream_url"]
        raise ValueError(f"Station not found: {station_id}")


register_provider("somafm", SomaFMProvider)
