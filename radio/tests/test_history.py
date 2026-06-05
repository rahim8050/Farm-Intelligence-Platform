"""Tests for the radio listening-history endpoints and service helpers."""

from __future__ import annotations

import secrets

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from radio.models import ListeningHistory, Provider, Station
from radio.services import (
    list_history_for_user,
    record_listening_session,
)

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


class RecordListeningSessionTestCase(TestCase):
    """Service-layer coverage for ``record_listening_session``."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="alice", password=secrets.token_urlsafe(12)
        )
        self.provider = Provider.objects.create(
            slug="bbc", name="BBC", is_active=True
        )
        self.station = _station(self.provider, "bbc_1xtra", "BBC 1Xtra")

    def test_anonymous_request_does_not_record(self) -> None:
        from django.contrib.auth.models import AnonymousUser

        row = record_listening_session(AnonymousUser(), self.station)
        self.assertIsNone(row)
        self.assertEqual(ListeningHistory.objects.count(), 0)

    def test_authenticated_request_records_a_row(self) -> None:
        row = record_listening_session(self.user, self.station)
        self.assertIsNotNone(row)
        self.assertEqual(row.user_id, self.user.id)
        self.assertEqual(row.station_id, self.station.id)
        self.assertIsNone(row.ended_at)

    def test_history_is_per_user(self) -> None:
        bob = User.objects.create_user(
            username="bob", password=secrets.token_urlsafe(12)
        )
        record_listening_session(self.user, self.station)
        record_listening_session(bob, self.station)
        self.assertEqual(len(list_history_for_user(self.user)), 1)
        self.assertEqual(len(list_history_for_user(bob)), 1)
        self.assertNotEqual(
            list_history_for_user(self.user)[0].id,
            list_history_for_user(bob)[0].id,
        )


class StationStreamRecordsHistoryTestCase(APITestCase):
    """``StationStreamView`` should record a history row for auth'd calls."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="alice", password=secrets.token_urlsafe(12)
        )
        self.provider = Provider.objects.create(
            slug="bbc", name="BBC", is_active=True
        )
        self.station = _station(self.provider, "bbc_1xtra", "BBC 1Xtra")
        self.station.is_available = True
        self.station.save(update_fields=["is_available"])

    def test_anonymous_stream_call_does_not_record(self) -> None:
        response = self.client.get("/api/v1/radio/stations/bbc_1xtra/stream/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ListeningHistory.objects.count(), 0)

    def test_authenticated_stream_call_records_a_row(self) -> None:
        self.client.force_authenticate(user=self.user)
        response = self.client.get(
            "/api/v1/radio/stations/bbc_1xtra/stream/",
            HTTP_USER_AGENT="test-agent/1.0",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ListeningHistory.objects.count(), 1)
        row = ListeningHistory.objects.get()
        self.assertEqual(row.user_id, self.user.id)
        self.assertEqual(row.station_id, "bbc_1xtra")
        self.assertEqual(row.user_agent, "test-agent/1.0")

    def test_unavailable_station_does_not_record(self) -> None:
        self.station.is_available = False
        self.station.save(update_fields=["is_available"])
        self.client.force_authenticate(user=self.user)
        response = self.client.get("/api/v1/radio/stations/bbc_1xtra/stream/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(ListeningHistory.objects.count(), 0)


class ListeningHistoryEndpointsTestCase(APITestCase):
    """Tests for ``GET /api/v1/radio/history/`` and ``.../recent/``."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="alice", password=secrets.token_urlsafe(12)
        )
        self.provider = Provider.objects.create(
            slug="bbc", name="BBC", is_active=True
        )
        self.station_a = _station(self.provider, "bbc_1xtra", "BBC 1Xtra")
        self.station_b = _station(self.provider, "bbc_radio1", "BBC Radio 1")
        record_listening_session(self.user, self.station_a)
        record_listening_session(self.user, self.station_b)

    def test_unauthenticated_request_returns_401(self) -> None:
        response = self.client.get(reverse("radio-history"))
        self.assertEqual(response.status_code, 401)

    def test_list_returns_users_rows(self) -> None:
        self.client.force_authenticate(user=self.user)
        response = self.client.get(reverse("radio-history"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], 0)
        self.assertEqual(len(response.data["data"]["results"]), 2)
        self.assertEqual(response.data["data"]["count"], 2)

    def test_list_does_not_leak_other_users(self) -> None:
        bob = User.objects.create_user(
            username="bob", password=secrets.token_urlsafe(12)
        )
        record_listening_session(bob, self.station_a)
        self.client.force_authenticate(user=self.user)
        response = self.client.get(reverse("radio-history"))
        ids = [row["station_id"] for row in response.data["data"]["results"]]
        self.assertEqual(len(ids), 2)
        self.assertNotIn(bob.username, ids)

    def test_recent_endpoint_with_default_limit(self) -> None:
        self.client.force_authenticate(user=self.user)
        response = self.client.get(reverse("radio-history-recent"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], 0)
        self.assertEqual(len(response.data["data"]["results"]), 2)

    def test_recent_endpoint_with_explicit_limit(self) -> None:
        self.client.force_authenticate(user=self.user)
        response = self.client.get(
            reverse("radio-history-recent"), {"limit": "1"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["data"]["results"]), 1)

    def test_recent_endpoint_rejects_invalid_limit(self) -> None:
        self.client.force_authenticate(user=self.user)
        response = self.client.get(
            reverse("radio-history-recent"), {"limit": "abc"}
        )
        self.assertEqual(response.status_code, 400)

    def test_recent_endpoint_rejects_out_of_range_limit(self) -> None:
        self.client.force_authenticate(user=self.user)
        response = self.client.get(
            reverse("radio-history-recent"), {"limit": "999"}
        )
        self.assertEqual(response.status_code, 400)
