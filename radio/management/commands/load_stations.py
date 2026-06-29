from typing import Any

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

        radiobrowser_provider, _ = Provider.objects.get_or_create(
            slug="radiobrowser",
            defaults={
                "name": "Radio Browser",
                "provider_type": ProviderType.API_BASED,
                "website_url": "https://www.radio-browser.info",
                "is_active": True,
            },
        )
        self.stdout.write(f"Provider: {radiobrowser_provider.name}")

        bbc_stations = [
            {
                "id": "bbc_radio1",
                "name": "BBC Radio 1",
                "provider": bbc_provider,
                "genre": "Pop, Chart",
                "country": "United Kingdom",
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
                "country": "United Kingdom",
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
                "country": "United Kingdom",
                "language": "English",
                "stream_url": "https://a.files.bbci.co.uk/media/live/manifesto/audio/simulcast/hls/uk/high/cfs/bbc_radio_two.m3u8",
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
                "country": "United States",
                "language": "English",
                "stream_url": "https://ice5.somafm.com/groovesalad-128-mp3",
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
                "country": "United States",
                "language": "English",
                "stream_url": "https://ice5.somafm.com/dronezone-128-mp3",
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
                "country": "United States",
                "language": "English",
                "stream_url": "https://ice5.somafm.com/deepspaceone-128-mp3",
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
                "country": "United States",
                "language": "English",
                "stream_url": "https://ice5.somafm.com/spacestation-128-mp3",
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
                "country": "United States",
                "language": "English",
                "stream_url": "https://ice5.somafm.com/secretagent-128-mp3",
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
                "country": "United States",
                "language": "English",
                "stream_url": "https://ice5.somafm.com/defcon-128-mp3",
                "format": "MP3",
                "bitrate": 128,
                "logo_url": "https://somafm.com/img3/defcon-200.jpg",
                "website_url": "https://somafm.com/defcon",
                "is_active": True,
            },
            {
                "id": "somafm_beatblender",
                "name": "SomaFM Beat Blender",
                "provider": somafm_provider,
                "genre": "House, Electronica",
                "country": "United States",
                "language": "English",
                "stream_url": "https://ice5.somafm.com/beatblender-128-mp3",
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
                "country": "United Kingdom",
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
            station, created = Station.objects.update_or_create(
                id=station_data["id"],
                defaults=station_data,
            )
            if created:
                self.stdout.write(f"Created station: {station.name}")
            else:
                self.stdout.write(f"Updated station: {station.name}")

        def _trunc(s: str, maxlen: int) -> str:
            return s[:maxlen] if len(s) > maxlen else s

        field_maxlen = {
            "name": 200,
            "genre": 100,
            "country": 100,
            "language": 100,
            "stream_url": 200,
            "format": 20,
            "logo_url": 200,
            "website_url": 200,
            "metadata_url": 200,
        }

        radiobrowser_count = 0
        radiobrowser_errors = 0
        try:
            from radio.providers.radiobrowser import RadioBrowserProvider

            provider = RadioBrowserProvider()
            rb_stations = provider.get_stations()
            for s in rb_stations:
                s_data: dict[str, Any] = dict(s)
                s_data["provider"] = radiobrowser_provider
                s_data["is_active"] = True
                for field, maxlen in field_maxlen.items():
                    val = s_data.get(field)
                    if val is not None and isinstance(val, str):
                        s_data[field] = _trunc(val, maxlen)
                try:
                    Station.objects.update_or_create(
                        id=s_data["id"],
                        defaults=s_data,
                    )
                    radiobrowser_count += 1
                except Exception:
                    radiobrowser_errors += 1
            msg = f"Loaded {radiobrowser_count} Radio Browser stations"
            if radiobrowser_errors:
                msg += f", {radiobrowser_errors} skipped"
            self.stdout.write(self.style.SUCCESS(msg))
        except Exception as exc:
            self.stdout.write(
                self.style.WARNING(
                    f"Radio Browser API fetch failed (non-fatal): {exc}"
                )
            )

        self.stdout.write(
            self.style.SUCCESS("Radio stations loaded successfully")
        )
