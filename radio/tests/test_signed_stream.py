"""Tests for the signed stream URL endpoint.

Covers auth gating, JWT generation, station lookup, and
listening-history recording.
"""

from __future__ import annotations

import datetime
import secrets

import jwt as pyjwt
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from radio.models import ListeningHistory, Provider, Station

User = get_user_model()


def _station(provider: Provider, id_: str, name: str) -> Station:
    return Station.objects.create(
        id=id_,
        name=name,
        provider=provider,
        country="UK",
        language="English",
        stream_url=f"https://example.test/{id_}",
        is_active=True,
    )


class SignedStreamViewTestCase(TestCase):
    """Endpoint coverage for signed stream URL.

    URL: ``GET /api/v1/radio/stations/<id>/stream/signed/``.
    """

    def setUp(self) -> None:
        self.client_api = APIClient()
        self.user = User.objects.create_user(
            username="dave", password=secrets.token_urlsafe(12)
        )
        self.provider = Provider.objects.create(
            slug="test", name="Test", is_active=True
        )
        self.station = _station(self.provider, "test_station", "Test Station")

    def test_requires_authentication(self) -> None:
        response = self.client_api.get(
            reverse("station-stream-signed", args=[self.station.id])
        )
        self.assertEqual(response.status_code, 401)

    def test_returns_signed_url(self) -> None:
        self.client_api.force_authenticate(self.user)
        response = self.client_api.get(
            reverse("station-stream-signed", args=[self.station.id])
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], 0)
        self.assertEqual(body["message"], "Signed stream URL generated")

        data = body["data"]
        self.assertEqual(data["station_name"], "Test Station")
        self.assertEqual(data["stream_url"], self.station.stream_url)
        self.assertEqual(data["format"], "MP3")
        self.assertEqual(data["bitrate"], 128)

        token = data["token"]
        decoded = pyjwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=["HS256"],
        )
        self.assertEqual(decoded["station_id"], self.station.id)
        self.assertEqual(decoded["user_id"], str(self.user.id))
        self.assertEqual(decoded["purpose"], "stream_access")
        self.assertIn("exp", decoded)
        self.assertIn("iat", decoded)

    def test_token_ttl_matches_setting(self) -> None:
        self.client_api.force_authenticate(self.user)
        with self.settings(RADIO_SIGNED_STREAM_TTL_SECONDS=120):
            response = self.client_api.get(
                reverse("station-stream-signed", args=[self.station.id])
            )
        body = response.json()
        token = body["data"]["token"]
        decoded = pyjwt.decode(
            token, settings.SECRET_KEY, algorithms=["HS256"]
        )
        now_ts = int(timezone.now().timestamp())
        self.assertAlmostEqual(decoded["exp"], now_ts + 120, delta=5)

    def test_expires_at_in_response(self) -> None:
        self.client_api.force_authenticate(self.user)
        with self.settings(RADIO_SIGNED_STREAM_TTL_SECONDS=300):
            response = self.client_api.get(
                reverse("station-stream-signed", args=[self.station.id])
            )
        body = response.json()
        expires_at = body["data"]["expires_at"]
        expected = timezone.now() + datetime.timedelta(seconds=300)
        parsed = datetime.datetime.fromisoformat(expires_at)
        self.assertAlmostEqual(
            parsed.timestamp(), expected.timestamp(), delta=5
        )

    def test_station_not_found_returns_404(self) -> None:
        self.client_api.force_authenticate(self.user)
        response = self.client_api.get(
            reverse("station-stream-signed", args=["nonexistent"])
        )
        self.assertEqual(response.status_code, 404)

    def test_inactive_station_returns_404(self) -> None:
        inactive = _station(self.provider, "inactive_station", "Inactive")
        inactive.is_active = False
        inactive.save()
        self.client_api.force_authenticate(self.user)
        response = self.client_api.get(
            reverse("station-stream-signed", args=[inactive.id])
        )
        self.assertEqual(response.status_code, 404)

    def test_records_listening_history(self) -> None:
        self.client_api.force_authenticate(self.user)
        self.client_api.get(
            reverse("station-stream-signed", args=[self.station.id])
        )
        self.assertTrue(
            ListeningHistory.objects.filter(
                user=self.user, station=self.station
            ).exists()
        )

    def test_different_user_gets_different_token(self) -> None:
        user2 = User.objects.create_user(
            username="eve", password=secrets.token_urlsafe(12)
        )
        self.client_api.force_authenticate(self.user)
        resp1 = self.client_api.get(
            reverse("station-stream-signed", args=[self.station.id])
        )
        self.client_api.force_authenticate(user2)
        resp2 = self.client_api.get(
            reverse("station-stream-signed", args=[self.station.id])
        )
        token1 = resp1.json()["data"]["token"]
        token2 = resp2.json()["data"]["token"]
        self.assertNotEqual(token1, token2)
        decoded1 = pyjwt.decode(
            token1, settings.SECRET_KEY, algorithms=["HS256"]
        )
        decoded2 = pyjwt.decode(
            token2, settings.SECRET_KEY, algorithms=["HS256"]
        )
        self.assertEqual(decoded1["user_id"], str(self.user.id))
        self.assertEqual(decoded2["user_id"], str(user2.id))
