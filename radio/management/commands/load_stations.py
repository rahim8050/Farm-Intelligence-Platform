from django.core.management.base import BaseCommand

from radio.models import Provider, Station


class Command(BaseCommand):
    help = "Load initial radio station data"

    def handle(self, *args: object, **options: object) -> None:
        provider, _ = Provider.objects.get_or_create(
            slug="bbc",
            defaults={
                "name": "BBC",
                "website_url": "https://www.bbc.co.uk",
                "logo_url": "https://www.bbc.co.uk/favicon.ico",
                "is_active": True,
            },
        )
        self.stdout.write(f"Provider: {provider.name}")

        stations_data = [
            {
                "id": "bbc_1xtra",
                "name": "BBC 1Xtra",
                "provider": provider,
                "genre": "Hip Hop",
                "country": "UK",
                "language": "English",
                "stream_url": "http://stream.live.vc.bbcmedia.co.uk/bbc_1xtra",
                "format": "MP3",
                "bitrate": 128,
                "is_active": True,
            },
        ]

        for station_data in stations_data:
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
