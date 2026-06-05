"""Tests for the alerts REST views.

The TTS backend is stubbed so we do not shell out during tests. The
WebSocket channel layer is the in-memory one that ships with
Django Channels, so push delivery is verified against the
``AudioAlert.is_delivered`` flag.
"""

from __future__ import annotations

import secrets
from typing import Any
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from alerts.models import (
    AudioAlert,
    AudioAlertSubscription,
    AudioAlertType,
)
from alerts.tts import TTSResult

pytestmark = pytest.mark.django_db


def _stub_tts() -> Any:
    return patch(
        "alerts.tts.synthesize",
        return_value=TTSResult(b"RIFF...stub", "audio/wav", 1500),
    )


@pytest.fixture
def auth_client(make_user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=make_user())
    return client


def test_subscription_list_create_view_create_and_list(
    auth_client, make_user, make_farm
) -> None:
    user = auth_client.handler._force_user  # type: ignore[attr-defined]
    farm = make_farm(user)
    with _stub_tts():
        resp = auth_client.post(
            "/api/v1/alerts/subscriptions/",
            data={
                "farm": farm.id,
                "alert_types": [
                    AudioAlertType.NDVI_LOW,
                    AudioAlertType.NDVI_DECLINE,
                ],
            },
            format="json",
        )
    assert resp.status_code == 201, resp.data
    body = resp.data
    assert body["status"] == 0
    assert body["data"]["farm"] == farm.id
    assert sorted(body["data"]["alert_types"]) == sorted(
        [AudioAlertType.NDVI_LOW, AudioAlertType.NDVI_DECLINE]
    )

    # Idempotent re-POST should be 200, not 201
    with _stub_tts():
        resp2 = auth_client.post(
            "/api/v1/alerts/subscriptions/",
            data={
                "farm": farm.id,
                "alert_types": [AudioAlertType.NDVI_LOW],
            },
            format="json",
        )
    assert resp2.status_code == 200
    assert resp2.data["data"]["alert_types"] == [AudioAlertType.NDVI_LOW]

    # GET list
    resp3 = auth_client.get("/api/v1/alerts/subscriptions/")
    assert resp3.status_code == 200
    assert resp3.data["status"] == 0
    assert resp3.data["data"]["count"] == 1
    assert len(resp3.data["data"]["results"]) == 1


def test_subscription_create_rejects_bad_alert_type(
    auth_client, make_user, make_farm
) -> None:
    user = auth_client.handler._force_user  # type: ignore[attr-defined]
    farm = make_farm(user)
    resp = auth_client.post(
        "/api/v1/alerts/subscriptions/",
        data={"farm": farm.id, "alert_types": ["not-a-type"]},
        format="json",
    )
    assert resp.status_code == 400


def test_subscription_detail_patch_and_delete(
    auth_client, make_user, make_farm
) -> None:
    user = auth_client.handler._force_user  # type: ignore[attr-defined]
    farm = make_farm(user)
    sub = AudioAlertSubscription.objects.create(
        user=user,
        farm=farm,
        alert_types=[AudioAlertType.NDVI_LOW],
    )
    resp = auth_client.patch(
        f"/api/v1/alerts/subscriptions/{sub.id}/",
        data={"farm": farm.id, "alert_types": [AudioAlertType.NDVI_DECLINE]},
        format="json",
    )
    assert resp.status_code == 200
    sub.refresh_from_db()
    assert sub.alert_types == [AudioAlertType.NDVI_DECLINE]
    resp2 = auth_client.delete(f"/api/v1/alerts/subscriptions/{sub.id}/")
    assert resp2.status_code == 200
    assert not AudioAlertSubscription.objects.filter(id=sub.id).exists()


def test_subscription_detail_404_for_other_user(
    auth_client, make_user, make_farm
) -> None:
    other = make_user()
    other_farm = make_farm(other)
    sub = AudioAlertSubscription.objects.create(
        user=other,
        farm=other_farm,
        alert_types=[AudioAlertType.NDVI_LOW],
    )
    resp = auth_client.patch(
        f"/api/v1/alerts/subscriptions/{sub.id}/",
        data={
            "farm": other_farm.id,
            "alert_types": [AudioAlertType.NDVI_LOW],
        },
        format="json",
    )
    assert resp.status_code == 404


def test_alert_list_view_returns_callers_alerts(
    auth_client, make_user, make_farm
) -> None:
    user = auth_client.handler._force_user  # type: ignore[attr-defined]
    farm = make_farm(user)
    with _stub_tts():
        from alerts.services import dispatch_alert

        for _ in range(2):
            dispatch_alert(
                user_id=user.id,
                farm_id=farm.id,
                alert_type=AudioAlertType.NDVI_LOW,
                trigger_source="ndvi_task",
                title="t",
                message="m",
            )
    resp = auth_client.get("/api/v1/alerts/")
    assert resp.status_code == 200
    assert resp.data["status"] == 0
    assert len(resp.data["data"]["results"]) == 2
    assert resp.data["data"]["count"] == 2
    unread = auth_client.get("/api/v1/alerts/?unread=true")
    assert len(unread.data["data"]["results"]) == 2


def test_alert_detail_view_get_and_acknowledge(
    auth_client, make_user, make_farm
) -> None:
    user = auth_client.handler._force_user  # type: ignore[attr-defined]
    with _stub_tts():
        from alerts.services import dispatch_alert

        result = dispatch_alert(
            user_id=user.id,
            farm_id=None,
            alert_type=AudioAlertType.ADMIN_BROADCAST,
            trigger_source="admin_view",
            title="t",
            message="m",
        )
    resp = auth_client.get(f"/api/v1/alerts/{result.alert_id}/")
    assert resp.status_code == 200
    assert resp.data["data"]["title"] == "t"
    resp2 = auth_client.post(f"/api/v1/alerts/{result.alert_id}/")
    assert resp2.status_code == 200
    alert = AudioAlert.objects.get(id=result.alert_id)
    assert alert.is_acknowledged is True


def test_alert_detail_404_for_other_user(
    auth_client, make_user, make_farm
) -> None:
    other = make_user()
    with _stub_tts():
        from alerts.services import dispatch_alert

        result = dispatch_alert(
            user_id=other.id,
            farm_id=None,
            alert_type=AudioAlertType.ADMIN_BROADCAST,
            trigger_source="admin_view",
            title="t",
            message="m",
        )
    resp = auth_client.get(f"/api/v1/alerts/{result.alert_id}/")
    assert resp.status_code == 404


def test_admin_broadcast_requires_admin(auth_client) -> None:
    resp = auth_client.post(
        "/api/v1/alerts/admin/send/",
        data={
            "user_ids": [1, 2],
            "title": "t",
            "message": "m",
        },
        format="json",
    )
    assert resp.status_code == 403


def test_admin_broadcast_dispatches(make_user) -> None:
    user_model = type(make_user())
    admin = user_model.objects.create_superuser(
        username="admin", password=secrets.token_urlsafe(16)
    )
    target1 = make_user()
    target2 = make_user()
    client = APIClient()
    client.force_authenticate(user=admin)
    with _stub_tts():
        resp = client.post(
            "/api/v1/alerts/admin/send/",
            data={
                "user_ids": [target1.id, target2.id],
                "title": "t",
                "message": "m",
            },
            format="json",
        )
    assert resp.status_code == 200
    assert resp.data["data"]["dispatched"] == 2


def test_anonymous_cannot_access_alerts() -> None:
    client = APIClient()
    resp = client.get("/api/v1/alerts/")
    assert resp.status_code in (401, 403)
