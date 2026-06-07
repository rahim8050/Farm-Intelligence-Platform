"""Tests for the radio emergency-broadcast endpoints and service helpers.

Covers Phase 5 (P5) emergency broadcasts as described in
``docs/architecture/radio/08_future_expansion.md``.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from radio.models import EmergencyBroadcast, EmergencyPriority
from radio.services import (
    create_emergency_broadcast,
    delete_emergency_broadcast,
    get_current_emergency,
    list_emergency_history,
    update_emergency_broadcast,
)

User = get_user_model()


def _make(
    *,
    title: str = "Severe storm",
    message: str = "Take shelter.",
    priority: str = EmergencyPriority.HIGH,
    starts_at=None,
    ends_at=None,
    is_active: bool = True,
) -> EmergencyBroadcast:
    now = timezone.now()
    return create_emergency_broadcast(
        title=title,
        message=message,
        priority=priority,
        starts_at=starts_at or now - timedelta(minutes=5),
        ends_at=ends_at or now + timedelta(hours=1),
        is_active=is_active,
    )


class EmergencyServiceTestCase(TestCase):
    """Service-layer coverage for ``radio.services`` emergency helpers."""

    def test_create_and_get_current(self) -> None:
        broadcast = _make(title="Frost warning")
        current = get_current_emergency()
        self.assertIsNotNone(current)
        self.assertEqual(current.id, broadcast.id)

    def test_get_current_returns_none_when_none_active(self) -> None:
        _make(is_active=False)
        self.assertIsNone(get_current_emergency())

    def test_get_current_chooses_higher_priority(self) -> None:
        low = _make(
            title="Low priority",
            priority=EmergencyPriority.LOW,
        )
        critical = _make(
            title="Critical",
            priority=EmergencyPriority.CRITICAL,
        )
        current = get_current_emergency()
        self.assertIsNotNone(current)
        self.assertEqual(current.id, critical.id)
        self.assertNotEqual(current.id, low.id)

    def test_get_current_excludes_inactive(self) -> None:
        _make(title="Inactive", is_active=False)
        active = _make(title="Active")
        current = get_current_emergency()
        self.assertIsNotNone(current)
        self.assertEqual(current.id, active.id)

    def test_list_emergency_history_orders_newest_first(self) -> None:
        old = _make(title="Old")
        new = _make(title="New")
        rows = list_emergency_history(limit=10, offset=0)
        self.assertEqual([r.id for r in rows], [new.id, old.id])

    def test_update_and_delete(self) -> None:
        broadcast = _make(title="Initial")
        update_emergency_broadcast(
            broadcast, fields={"title": "Updated", "is_active": False}
        )
        broadcast.refresh_from_db()
        self.assertEqual(broadcast.title, "Updated")
        self.assertFalse(broadcast.is_active)
        delete_emergency_broadcast(broadcast)
        self.assertFalse(
            EmergencyBroadcast.objects.filter(pk=broadcast.pk).exists()
        )


class EmergencyEndpointTestCase(APITestCase):
    """Endpoint coverage for the ``/api/v1/radio/emergency/`` routes."""

    def setUp(self) -> None:
        self.client_api = APIClient()
        self.user = User.objects.create_user(
            username="bob", password=secrets.token_urlsafe(12)
        )
        self.admin = User.objects.create_superuser(
            username="admin",
            email="admin@example.test",
            password=secrets.token_urlsafe(12),
        )

    def test_current_returns_null_when_none_active(self) -> None:
        response = self.client_api.get(reverse("radio-emergency-current"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], 0)
        self.assertIsNone(body["data"])

    def test_current_returns_active(self) -> None:
        broadcast = _make(title="Active alert")
        response = self.client_api.get(reverse("radio-emergency-current"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], 0)
        self.assertEqual(body["data"]["id"], broadcast.id)
        self.assertEqual(body["data"]["title"], "Active alert")

    def test_history_returns_list(self) -> None:
        _make(title="one")
        _make(title="two")
        response = self.client_api.get(reverse("radio-emergency-history"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], 0)
        self.assertEqual(len(body["data"]), 2)

    def test_history_limit_and_offset(self) -> None:
        _make(title="a")
        b = _make(title="b")
        _make(title="c")
        response = self.client_api.get(
            reverse("radio-emergency-history"),
            {"limit": 1, "offset": 1},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["data"]), 1)
        self.assertEqual(body["data"][0]["id"], b.id)

    def test_history_invalid_limit(self) -> None:
        response = self.client_api.get(
            reverse("radio-emergency-history"),
            {"limit": "abc"},
        )
        self.assertEqual(response.status_code, 400)

    def test_history_limit_out_of_range(self) -> None:
        response = self.client_api.get(
            reverse("radio-emergency-history"),
            {"limit": 999},
        )
        self.assertEqual(response.status_code, 400)

    def test_create_requires_admin(self) -> None:
        self.client_api.force_authenticate(self.user)
        now = timezone.now()
        response = self.client_api.post(
            reverse("radio-emergency-create"),
            data={
                "title": "Test",
                "message": "Body",
                "priority": EmergencyPriority.HIGH,
                "starts_at": (now - timedelta(minutes=1)).isoformat(),
                "ends_at": (now + timedelta(hours=1)).isoformat(),
            },
            format="json",
        )
        self.assertIn(response.status_code, (401, 403))

    def test_create_admin_success(self) -> None:
        self.client_api.force_authenticate(self.admin)
        now = timezone.now()
        response = self.client_api.post(
            reverse("radio-emergency-create"),
            data={
                "title": "Test",
                "message": "Body",
                "priority": EmergencyPriority.HIGH,
                "starts_at": (now - timedelta(minutes=1)).isoformat(),
                "ends_at": (now + timedelta(hours=1)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["data"]["title"], "Test")
        self.assertEqual(EmergencyBroadcast.objects.count(), 1)

    def test_create_rejects_ends_before_starts(self) -> None:
        self.client_api.force_authenticate(self.admin)
        now = timezone.now()
        response = self.client_api.post(
            reverse("radio-emergency-create"),
            data={
                "title": "Bad",
                "message": "Body",
                "priority": EmergencyPriority.HIGH,
                "starts_at": (now + timedelta(hours=1)).isoformat(),
                "ends_at": (now - timedelta(hours=1)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_patch_updates_broadcast(self) -> None:
        broadcast = _make(title="Old")
        self.client_api.force_authenticate(self.admin)
        response = self.client_api.patch(
            reverse("radio-emergency-detail", args=[broadcast.pk]),
            data={"title": "New"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        broadcast.refresh_from_db()
        self.assertEqual(broadcast.title, "New")

    def test_delete_is_idempotent(self) -> None:
        self.client_api.force_authenticate(self.admin)
        broadcast = _make()
        url = reverse("radio-emergency-detail", args=[broadcast.pk])
        first = self.client_api.delete(url)
        second = self.client_api.delete(url)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(
            EmergencyBroadcast.objects.filter(pk=broadcast.pk).exists()
        )
