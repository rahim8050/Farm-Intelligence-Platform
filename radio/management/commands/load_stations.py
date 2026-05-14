from django.core.management.base import BaseCommand

from radio.models import Provider, ProviderType, Station


class Command(BaseCommand):
    help = "Load initial radio station data"

    def handle(self, *args: object, **options: object) -> None:
        bbc_provider, _ = Provider.objects.get_or_create(
            slug="bbc",
            defaults={
                "name": "BBC",
                "provider_type": ProviderType.BROADCASTER,
                "website_url": "https://www.bbc.co.uk",
                "logo_url": "https://www.bbc.co.uk/favicon.ico",
                "is_active": True,
            },
        )
        self.stdout.write(f"Provider: {bbc_provider.name}")

        somafm_provider, _ = Provider.objects.get_or_create(
            slug="somafm",
            defaults={
                "name": "SomaFM",
                "provider_type": ProviderType.BROADCASTER,
                "website_url": "https://somafm.com",
                "logo_url": "https://somafm.com/favicon.ico",
                "is_active": True,
            },
        )
        self.stdout.write(f"Provider: {somafm_provider.name}")

        tunein_provider, _ = Provider.objects.get_or_create(
            slug="tunein",
            defaults={
                "name": "TuneIn",
                "provider_type": ProviderType.AGGREGATOR,
                "website_url": "https://tunein.com",
                "is_active": True,
            },
        )
        self.stdout.write(f"Provider: {tunein_provider.name}")

        bbc_stations = [
            {
                "id": "bbc_radio1",
                "name": "BBC Radio 1",
                "provider": bbc_provider,
                "genre": "Pop, Chart",
                "country": "UK",
                "language": "English",
                "stream_url": "http://as-hls-ww-live.akamaized.net/pool_01505109/live/ww/bbc_radio_one/bbc_radio_one.isml/bbc_radio_one-audio%3d96000.norewind.m3u8",
                "format": "HLS",
                "bitrate": 96,
                "is_active": True,
            },
            {
                "id": "bbc_1xtra",
                "name": "BBC 1Xtra",
                "provider": bbc_provider,
                "genre": "Hip Hop, R&B",
                "country": "UK",
                "language": "English",
                "stream_url": "http://as-hls-ww-live.akamaized.net/pool_92079267/live/ww/bbc_1xtra/bbc_1xtra.isml/bbc_1xtra-audio%3d96000.norewind.m3u8",
                "format": "HLS",
                "bitrate": 96,
                "is_active": True,
            },
            {
                "id": "bbc_radio2",
                "name": "BBC Radio 2",
                "provider": bbc_provider,
                "genre": "Adult Contemporary",
                "country": "UK",
                "language": "English",
                "stream_url": "http://as-hls-ww-live.akamaized.net/pool_01505109/live/ww/bbc_radio_two/bbc_radio_two.isml/bbc_radio_two-audio%3d96000.norewind.m3u8",
                "format": "HLS",
                "bitrate": 96,
                "is_active": True,
            },
        ]

        somafm_stations = [
            {
                "id": "somafm_groovesalad",
                "name": "SomaFM Groove Salad",
                "provider": somafm_provider,
                "genre": "Ambient, Downtempo",
                "country": "USA",
                "language": "English",
                "stream_url": "https://ice.somafm.com/groovesalad",
                "format": "MP3",
                "bitrate": 128,
                "logo_url": "https://somafm.com/img3/groovesalad-200.jpg",
                "website_url": "https://somafm.com/groovesalad",
                "is_active": True,
            },
            {
                "id": "somafm_dronezone",
                "name": "SomaFM Drone Zone",
                "provider": somafm_provider,
                "genre": "Ambient, Atmospheric",
                "country": "USA",
                "language": "English",
                "stream_url": "https://ice.somafm.com/dronezone",
                "format": "MP3",
                "bitrate": 128,
                "logo_url": "https://somafm.com/img3/dronezone-200.jpg",
                "website_url": "https://somafm.com/dronezone",
                "is_active": True,
            },
            {
                "id": "somafm_deepspaceone",
                "name": "SomaFM Deep Space One",
                "provider": somafm_provider,
                "genre": "Ambient, Electronic",
                "country": "USA",
                "language": "English",
                "stream_url": "https://ice.somafm.com/deepspaceone",
                "format": "MP3",
                "bitrate": 128,
                "logo_url": "https://somafm.com/img3/deepspaceone-200.jpg",
                "website_url": "https://somafm.com/deepspaceone",
                "is_active": True,
            },
            {
                "id": "somafm_spacestation",
                "name": "SomaFM Space Station Soma",
                "provider": somafm_provider,
                "genre": "Ambient, Electronica",
                "country": "USA",
                "language": "English",
                "stream_url": "https://ice.somafm.com/spacestation",
                "format": "MP3",
                "bitrate": 128,
                "logo_url": "https://somafm.com/img3/spacestation-200.jpg",
                "website_url": "https://somafm.com/spacestation",
                "is_active": True,
            },
            {
                "id": "somafm_secretagent",
                "name": "SomaFM Secret Agent",
                "provider": somafm_provider,
                "genre": "Lounge, Spy",
                "country": "USA",
                "language": "English",
                "stream_url": "https://ice.somafm.com/secretagent",
                "format": "MP3",
                "bitrate": 128,
                "logo_url": "https://somafm.com/img3/secretagent-200.jpg",
                "website_url": "https://somafm.com/secretagent",
                "is_active": True,
            },
            {
                "id": "somafm_defcon",
                "name": "SomaFM DEF CON Radio",
                "provider": somafm_provider,
                "genre": "Electronic, Gaming",
                "country": "USA",
                "language": "English",
                "stream_url": "https://ice.somafm.com/defcon",
                "format": "MP3",
                "bitrate": 128,
                "logo_url": "https://somafm.com/img3/defcon-200.jpg",
                "website_url": "https://somafm.com/defcon",
                "is_active": True,
            },
            {
                "id": "somafm_lounge",
                "name": "SomaFM Lounge",
                "provider": somafm_provider,
                "genre": "Lounge, Jazz",
                "country": "USA",
                "language": "English",
                "stream_url": "https://ice.somafm.com/lounge",
                "format": "MP3",
                "bitrate": 128,
                "logo_url": "https://somafm.com/img3/lounge-200.jpg",
                "website_url": "https://somafm.com/lounge",
                "is_active": True,
            },
            {
                "id": "somafm_beatblender",
                "name": "SomaFM Beat Blender",
                "provider": somafm_provider,
                "genre": "House, Electronica",
                "country": "USA",
                "language": "English",
                "stream_url": "https://ice.somafm.com/beatblender",
                "format": "MP3",
                "bitrate": 128,
                "logo_url": "https://somafm.com/img3/beatblender-200.jpg",
                "website_url": "https://somafm.com/beatblender",
                "is_active": True,
            },
        ]

        tunein_stations = [
            {
                "id": "tunein_bbc_ws",
                "name": "BBC World Service",
                "provider": tunein_provider,
                "genre": "News, Talk",
                "country": "UK",
                "language": "English",
                "stream_url": "http://stream.live.vc.bbcmedia.co.uk/bbc_world_service",
                "format": "MP3",
                "bitrate": 96,
                "website_url": "https://www.bbc.com/worldservice",
                "is_active": True,
            },
        ]

        all_stations = bbc_stations + somafm_stations + tunein_stations

        for station_data in all_stations:
            station, created = Station.objects.get_or_create(
                id=station_data["id"],
                defaults=station_data,
            )
            if created:
                self.stdout.write(f"Created station: {station.name}")
            else:
                self.stdout.write(f"Station already exists: {station.name}")

        self.stdout.write(
            self.style.SUCCESS("Radio stations loaded successfully")
        )
