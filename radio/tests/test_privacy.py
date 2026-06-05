"""User-data isolation tests for the radio app.

Per the API and Data Lifecycle Standards
(``prompts/p3followup.md``), every endpoint exposing user-owned
data must include tests verifying:

- No cross-user data leakage (read or write).
- Authentication enforcement.
- Authorization enforcement.
- Anonymous access behavior.

These tests focus on the privacy boundary; happy-path coverage
lives in ``test_favorites.py`` and ``test_history.py``.
"""

from __future__ import annotations

import secrets

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APITestCase

from radio.models import Favorite, ListeningHistory, Provider, Station


def _station(
    provider: Provider, station_id: str, name: str = "Test Station"
) -> Station:
    return Station.objects.create(
        id=station_id,
        name=name,
        provider=provider,
        country="Kenya",
        language="English",
        stream_url=f"https://example.com/{station_id}.mp3",
        is_active=True,
    )


class FavoritePrivacyTests(APITestCase):
    """``/api/v1/radio/favorites/`` privacy boundary."""

    def setUp(self) -> None:
        self.user_model = get_user_model()
        self.alice = self.user_model.objects.create_user(
            username="alice", password=secrets.token_urlsafe(12)
        )
        self.bob = self.user_model.objects.create_user(
            username="bob", password=secrets.token_urlsafe(12)
        )
        self.provider = Provider.objects.create(
            slug="bbc", name="BBC", is_active=True
        )
        self.station = _station(self.provider, "bbc_1xtra", "BBC 1Xtra")

    def test_anonymous_list_returns_401(self) -> None:
        resp = self.client.get(reverse("radio-favorites"))
        self.assertEqual(resp.status_code, 401)

    def test_anonymous_create_returns_401(self) -> None:
        resp = self.client.post(
            reverse("radio-favorites"),
            data={"station_id": self.station.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 401)

    def test_anonymous_delete_returns_401(self) -> None:
        resp = self.client.delete(
            reverse(
                "radio-favorites-delete",
                kwargs={"station_id": self.station.id},
            )
        )
        self.assertEqual(resp.status_code, 401)

    def test_alice_cannot_delete_bobs_favorite(self) -> None:
        """Authorization: a user cannot delete another user's favorite."""
        Favorite.objects.create(user=self.bob, station=self.station)
        self.client.force_authenticate(user=self.alice)
        resp = self.client.delete(
            reverse(
                "radio-favorites-delete",
                kwargs={"station_id": self.station.id},
            )
        )
        self.assertEqual(resp.status_code, 200)
        # Bob's row must still be there.
        self.assertTrue(
            Favorite.objects.filter(
                user=self.bob, station=self.station
            ).exists()
        )


class HistoryPrivacyTests(APITestCase):
    """``/api/v1/radio/history/`` privacy boundary."""

    def setUp(self) -> None:
        self.user_model = get_user_model()
        self.alice = self.user_model.objects.create_user(
            username="alice", password=secrets.token_urlsafe(12)
        )
        self.bob = self.user_model.objects.create_user(
            username="bob", password=secrets.token_urlsafe(12)
        )
        self.provider = Provider.objects.create(
            slug="bbc", name="BBC", is_active=True
        )
        self.station = _station(self.provider, "bbc_1xtra", "BBC 1Xtra")

    def test_anonymous_list_returns_401(self) -> None:
        resp = self.client.get(reverse("radio-history"))
        self.assertEqual(resp.status_code, 401)

    def test_anonymous_recent_returns_401(self) -> None:
        resp = self.client.get(reverse("radio-history-recent"))
        self.assertEqual(resp.status_code, 401)

    def test_alice_does_not_see_bobs_history(self) -> None:
        ListeningHistory.objects.create(user=self.alice, station=self.station)
        ListeningHistory.objects.create(user=self.bob, station=self.station)
        self.client.force_authenticate(user=self.alice)
        resp = self.client.get(reverse("radio-history"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["data"]["count"], 1)
        rows = resp.data["data"]["results"]
        self.assertEqual(len(rows), 1)
        # user_id is intentionally not exposed by the serializer; we
        # assert the count as the privacy boundary contract.
        self.assertEqual(rows[0]["station_id"], self.station.id)

    def test_recent_endpoint_isolates_users(self) -> None:
        ListeningHistory.objects.create(user=self.alice, station=self.station)
        ListeningHistory.objects.create(user=self.bob, station=self.station)
        self.client.force_authenticate(user=self.alice)
        resp = self.client.get(reverse("radio-history-recent"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["data"]["count"], 1)
        rows = resp.data["data"]["results"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["station_id"], self.station.id)
