"""Tests for the listening-session stop endpoint.

Covers the service function ``stop_listening_session`` and
the HTTP endpoint ``POST /api/v1/radio/history/<id>/stop/``.
"""

from __future__ import annotations

import secrets

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from radio.models import Provider, Station
from radio.services import record_listening_session, stop_listening_session

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


class StopListeningSessionServiceTestCase(TestCase):
    """Service-layer coverage for ``stop_listening_session``."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="alice", password=secrets.token_urlsafe(12)
        )
        self.provider = Provider.objects.create(
            slug="test", name="Test", is_active=True
        )
        self.station = _station(self.provider, "test_stop", "Test Stop")
        self.session = record_listening_session(self.user, self.station)
        assert self.session is not None

    def test_stops_open_session(self) -> None:
        result = stop_listening_session(self.user, self.session.id)
        self.assertIsNotNone(result)
        self.assertIsNotNone(result.ended_at)

    def test_stop_is_idempotent(self) -> None:
        first = stop_listening_session(self.user, self.session.id)
        assert first is not None
        ts = first.ended_at
        second = stop_listening_session(self.user, self.session.id)
        assert second is not None
        self.assertEqual(second.ended_at, ts)

    def test_wrong_user_returns_none(self) -> None:
        other = User.objects.create_user(
            username="bob", password=secrets.token_urlsafe(12)
        )
        result = stop_listening_session(other, self.session.id)
        self.assertIsNone(result)
        self.session.refresh_from_db()
        self.assertIsNone(self.session.ended_at)

    def test_nonexistent_session_returns_none(self) -> None:
        result = stop_listening_session(self.user, 99999)
        self.assertIsNone(result)


class StopSessionEndpointTestCase(TestCase):
    """Endpoint coverage for ``POST /api/v1/radio/history/<id>/stop/``."""

    def setUp(self) -> None:
        self.client_api = APIClient()
        self.user = User.objects.create_user(
            username="carol", password=secrets.token_urlsafe(12)
        )
        self.provider = Provider.objects.create(
            slug="test", name="Test", is_active=True
        )
        self.station = _station(
            self.provider, "test_endpoint", "Test Endpoint"
        )
        self.session = record_listening_session(self.user, self.station)
        assert self.session is not None

    def test_requires_authentication(self) -> None:
        response = self.client_api.post(
            reverse("radio-history-stop", args=[self.session.id])
        )
        self.assertEqual(response.status_code, 401)

    def test_stops_session(self) -> None:
        self.client_api.force_authenticate(self.user)
        response = self.client_api.post(
            reverse("radio-history-stop", args=[self.session.id])
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], 0)
        self.assertEqual(body["message"], "Listening session stopped")
        self.session.refresh_from_db()
        self.assertIsNotNone(self.session.ended_at)

    def test_nonexistent_session_returns_404(self) -> None:
        self.client_api.force_authenticate(self.user)
        response = self.client_api.post(
            reverse("radio-history-stop", args=[99999])
        )
        self.assertEqual(response.status_code, 404)

    def test_other_users_session_returns_404(self) -> None:
        other = User.objects.create_user(
            username="dave", password=secrets.token_urlsafe(12)
        )
        self.client_api.force_authenticate(other)
        response = self.client_api.post(
            reverse("radio-history-stop", args=[self.session.id])
        )
        self.assertEqual(response.status_code, 404)
