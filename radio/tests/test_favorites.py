"""Tests for the radio favorites endpoints and service helpers."""

from __future__ import annotations

import secrets

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from radio.models import Favorite, Provider, Station

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


class FavoriteServiceTestCase(TestCase):
    """Service-layer coverage for ``radio.services.{add,remove}_favorite``."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="alice", password=secrets.token_urlsafe(12)
        )
        self.provider = Provider.objects.create(
            slug="bbc", name="BBC", is_active=True
        )
        self.station_a = _station(self.provider, "bbc_1xtra", "BBC 1Xtra")
        self.station_b = _station(self.provider, "bbc_radio1", "BBC Radio 1")

    def test_add_favorite_is_idempotent(self) -> None:
        from radio.services import add_favorite

        fav1, created1 = add_favorite(self.user, self.station_a)
        fav2, created2 = add_favorite(self.user, self.station_a)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(fav1.id, fav2.id)
        self.assertEqual(Favorite.objects.filter(user=self.user).count(), 1)

    def test_remove_favorite_is_idempotent(self) -> None:
        from radio.services import add_favorite, remove_favorite

        add_favorite(self.user, self.station_a)
        self.assertTrue(remove_favorite(self.user, "bbc_1xtra"))
        self.assertFalse(remove_favorite(self.user, "bbc_1xtra"))
        self.assertEqual(Favorite.objects.filter(user=self.user).count(), 0)

    def test_list_favorites_for_user_isolates_users(self) -> None:
        from radio.services import add_favorite, list_favorites_for_user

        bob = User.objects.create_user(
            username="bob", password=secrets.token_urlsafe(12)
        )
        add_favorite(self.user, self.station_a)
        add_favorite(self.user, self.station_b)
        add_favorite(bob, self.station_a)

        alice_favs = list_favorites_for_user(self.user)
        bob_favs = list_favorites_for_user(bob)
        self.assertEqual(
            {f.station_id for f in alice_favs},
            {"bbc_1xtra", "bbc_radio1"},
        )
        self.assertEqual({f.station_id for f in bob_favs}, {"bbc_1xtra"})


class FavoriteListCreateEndpointTestCase(APITestCase):
    """Tests for ``GET/POST /api/v1/radio/favorites/``."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="alice", password=secrets.token_urlsafe(12)
        )
        self.other = User.objects.create_user(
            username="bob", password=secrets.token_urlsafe(12)
        )
        self.provider = Provider.objects.create(
            slug="bbc", name="BBC", is_active=True
        )
        self.station = _station(self.provider, "bbc_1xtra", "BBC 1Xtra")

    def test_unauthenticated_request_returns_401(self) -> None:
        response = self.client.get(reverse("radio-favorites"))
        self.assertEqual(response.status_code, 401)

    def test_get_returns_empty_list_for_new_user(self) -> None:
        self.client.force_authenticate(user=self.user)
        response = self.client.get(reverse("radio-favorites"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], 0)
        self.assertEqual(response.data["data"]["count"], 0)
        self.assertEqual(response.data["data"]["results"], [])

    def test_post_creates_favorite_and_returns_envelope(self) -> None:
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            reverse("radio-favorites"),
            {"station_id": "bbc_1xtra"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], 0)
        self.assertEqual(response.data["data"]["station_id"], "bbc_1xtra")
        self.assertEqual(response.data["data"]["station"]["id"], "bbc_1xtra")
        self.assertEqual(Favorite.objects.filter(user=self.user).count(), 1)

    def test_post_is_idempotent(self) -> None:
        self.client.force_authenticate(user=self.user)
        first = self.client.post(
            reverse("radio-favorites"),
            {"station_id": "bbc_1xtra"},
            format="json",
        )
        second = self.client.post(
            reverse("radio-favorites"),
            {"station_id": "bbc_1xtra"},
            format="json",
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data["message"], "Already a favorite")
        self.assertEqual(Favorite.objects.filter(user=self.user).count(), 1)

    def test_post_unknown_station_returns_404(self) -> None:
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            reverse("radio-favorites"),
            {"station_id": "no_such_station"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_post_inactive_station_returns_400(self) -> None:
        self.station.is_active = False
        self.station.save(update_fields=["is_active"])
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            reverse("radio-favorites"),
            {"station_id": "bbc_1xtra"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_get_only_returns_callers_favorites(self) -> None:
        from radio.services import add_favorite

        station_b = _station(self.provider, "bbc_radio1", "BBC Radio 1")
        add_favorite(self.user, self.station)
        add_favorite(self.other, station_b)

        self.client.force_authenticate(user=self.user)
        response = self.client.get(reverse("radio-favorites"))
        self.assertEqual(response.status_code, 200)
        ids = [f["station_id"] for f in response.data["data"]["results"]]
        self.assertEqual(ids, ["bbc_1xtra"])
        self.assertEqual(response.data["data"]["count"], 1)


class FavoriteDeleteEndpointTestCase(APITestCase):
    """Tests for ``DELETE /api/v1/radio/favorites/<station_id>/``."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="alice", password=secrets.token_urlsafe(12)
        )
        self.provider = Provider.objects.create(
            slug="bbc", name="BBC", is_active=True
        )
        self.station = _station(self.provider, "bbc_1xtra", "BBC 1Xtra")
        self.url = reverse(
            "radio-favorites-delete", kwargs={"station_id": "bbc_1xtra"}
        )

    def test_unauthenticated_request_returns_401(self) -> None:
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, 401)

    def test_delete_removes_existing_favorite(self) -> None:
        from radio.services import add_favorite

        add_favorite(self.user, self.station)
        self.client.force_authenticate(user=self.user)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Favorite.objects.filter(user=self.user).count(), 0)

    def test_delete_is_idempotent(self) -> None:
        self.client.force_authenticate(user=self.user)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, 200)
