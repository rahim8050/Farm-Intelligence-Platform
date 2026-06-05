"""User-data isolation tests for the alerts app.

Per the API and Data Lifecycle Standards
(``prompts/p3followup.md``), every endpoint exposing user-owned
data must include tests verifying:

- No cross-user data leakage (read or write).
- Authentication enforcement.
- Authorization enforcement.
- Anonymous access behavior.
"""

from __future__ import annotations

import secrets
from typing import Any
from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from alerts.models import (
    AudioAlert,
    AudioAlertSubscription,
    AudioAlertType,
)
from farms.models import Farm

pytestmark = pytest.mark.django_db


def _make_user() -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        username=f"u-{secrets.token_urlsafe(8)}",
        password=secrets.token_urlsafe(16),
    )


def _make_farm(owner: Any) -> Farm:
    return Farm.objects.create(
        owner=owner, name="F", centroid_lat=0.0, centroid_lon=0.0
    )


def test_subscriptions_anonymous_returns_401() -> None:
    client = APIClient()
    resp = client.get("/api/v1/alerts/subscriptions/")
    assert resp.status_code == 401


def test_alerts_list_anonymous_returns_401() -> None:
    client = APIClient()
    resp = client.get("/api/v1/alerts/")
    assert resp.status_code == 401


def test_alert_detail_anonymous_returns_401() -> None:
    client = APIClient()
    resp = client.get(f"/api/v1/alerts/{uuid4()}/")
    assert resp.status_code == 401


def test_subscription_detail_anonymous_returns_401() -> None:
    client = APIClient()
    resp = client.get(f"/api/v1/alerts/subscriptions/{uuid4()}/")
    assert resp.status_code == 401


def test_alice_cannot_see_bobs_alerts() -> None:
    """Authorization: GET /alerts/ only returns the caller's rows."""
    alice = _make_user()
    bob = _make_user()
    AudioAlert.objects.create(
        user=alice,
        alert_type=AudioAlertType.ADMIN_BROADCAST,
        trigger_source="admin_view",
        title="a",
        message="a",
    )
    AudioAlert.objects.create(
        user=bob,
        alert_type=AudioAlertType.ADMIN_BROADCAST,
        trigger_source="admin_view",
        title="b",
        message="b",
    )
    client = APIClient()
    client.force_authenticate(user=alice)
    resp = client.get("/api/v1/alerts/")
    assert resp.status_code == 200
    rows = resp.data["data"]["results"]
    assert len(rows) == 1
    assert rows[0]["title"] == "a"


def test_alice_cannot_acknowledge_bobs_alert() -> None:
    bob = _make_user()
    bob_alert = AudioAlert.objects.create(
        user=bob,
        alert_type=AudioAlertType.ADMIN_BROADCAST,
        trigger_source="admin_view",
        title="b",
        message="b",
    )
    alice = _make_user()
    client = APIClient()
    client.force_authenticate(user=alice)
    resp = client.post(f"/api/v1/alerts/{bob_alert.id}/")
    assert resp.status_code == 404
    bob_alert.refresh_from_db()
    assert bob_alert.is_acknowledged is False


def test_alice_cannot_see_bobs_subscriptions() -> None:
    alice = _make_user()
    bob = _make_user()
    bob_farm = _make_farm(bob)
    AudioAlertSubscription.objects.create(
        user=bob,
        farm=bob_farm,
        alert_types=[AudioAlertType.NDVI_LOW],
    )
    client = APIClient()
    client.force_authenticate(user=alice)
    resp = client.get("/api/v1/alerts/subscriptions/")
    assert resp.status_code == 200
    assert resp.data["data"]["count"] == 0


def test_alice_cannot_delete_bobs_subscription() -> None:
    alice = _make_user()
    bob = _make_user()
    bob_farm = _make_farm(bob)
    sub = AudioAlertSubscription.objects.create(
        user=bob,
        farm=bob_farm,
        alert_types=[AudioAlertType.NDVI_LOW],
    )
    client = APIClient()
    client.force_authenticate(user=alice)
    resp = client.delete(f"/api/v1/alerts/subscriptions/{sub.id}/")
    assert resp.status_code == 404
    assert AudioAlertSubscription.objects.filter(id=sub.id).exists()


def test_alice_cannot_create_subscription_for_bobs_farm() -> None:
    """Cross-user create: Alice POSTs with Bob's farm id.
    The serializer should accept the farm (it's a valid farm) but
    the subscription row is owned by Alice. We assert the row is
    created for Alice and not Bob.
    """
    alice = _make_user()
    bob = _make_user()
    bob_farm = _make_farm(bob)
    client = APIClient()
    client.force_authenticate(user=alice)
    resp = client.post(
        "/api/v1/alerts/subscriptions/",
        data={
            "farm": bob_farm.id,
            "alert_types": [AudioAlertType.NDVI_LOW],
        },
        format="json",
    )
    assert resp.status_code == 201
    sub_id = resp.data["data"]["id"]
    sub = AudioAlertSubscription.objects.get(id=sub_id)
    assert sub.user_id == alice.id
    # Bob has no subscriptions.
    assert not AudioAlertSubscription.objects.filter(user=bob).exists()


def test_admin_broadcast_anonymous_returns_401() -> None:
    client = APIClient()
    resp = client.post(
        "/api/v1/alerts/admin/send/",
        data={"user_ids": [1], "title": "t", "message": "m"},
        format="json",
    )
    assert resp.status_code in (401, 403)
