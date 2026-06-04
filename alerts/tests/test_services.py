"""Tests for the alerts service layer.

These tests run with the in-memory channel layer and a stubbed TTS
backend so the WebSocket and audio paths are exercised end-to-end.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest
from django.test import override_settings

from alerts.models import (
    AudioAlert,
    AudioAlertSubscription,
    AudioAlertType,
)
from alerts.services import (
    acknowledge_alert,
    dispatch_alert,
    group_name_for_user,
    has_subscription,
    list_alerts_for_user,
    subscribed_users_for_farm,
)

pytestmark = pytest.mark.django_db


def _stub_tts() -> Any:
    """Replace the real TTS call with a deterministic stub."""
    from alerts.tts import TTSResult

    return patch(
        "alerts.services.synthesize",
        return_value=TTSResult(b"RIFF...stub", "audio/wav", 1234),
    )


def _stub_channel_layer() -> Any:
    """Stub the Channels layer so the sync test loop can call into it."""
    from unittest.mock import AsyncMock, MagicMock

    layer = MagicMock()
    layer.group_send = AsyncMock(return_value=None)
    return patch("alerts.services.get_channel_layer", return_value=layer)


def test_group_name_for_user_uses_settings_prefix() -> None:
    with override_settings(ALERTS_WEBHOOK_GROUP_PREFIX="user_"):
        assert group_name_for_user(42) == "user_42"


def test_dispatch_alert_creates_row_and_synthesises_audio(
    make_user: Callable[..., Any], make_farm: Callable[..., Any]
) -> None:
    user = make_user()
    farm = make_farm(user)
    with _stub_tts(), _stub_channel_layer():
        result = dispatch_alert(
            user_id=user.id,
            farm_id=farm.id,
            alert_type=AudioAlertType.NDVI_LOW,
            trigger_source="ndvi_task",
            title="Low NDVI",
            message="Mean is 0.18",
        )
    assert result.delivered is True
    alert = AudioAlert.objects.get(id=result.alert_id)
    assert alert.user_id == user.id
    assert alert.farm_id == farm.id
    assert alert.title == "Low NDVI"
    assert alert.duration_ms == 1234
    assert alert.mime_type == "audio/wav"
    assert alert.is_delivered is True


def test_dispatch_alert_rejects_unknown_alert_type() -> None:
    with pytest.raises(ValueError):
        dispatch_alert(
            user_id=1,
            farm_id=None,
            alert_type="not-a-type",
            trigger_source="ndvi_task",
            title="x",
            message="x",
        )


def test_dispatch_alert_rejects_unknown_trigger_source() -> None:
    with pytest.raises(ValueError):
        dispatch_alert(
            user_id=1,
            farm_id=None,
            alert_type=AudioAlertType.ADMIN_BROADCAST,
            trigger_source="not-a-source",
            title="x",
            message="x",
        )


def test_acknowledge_alert_is_idempotent_and_scoped(
    make_user: Callable[..., Any],
) -> None:
    user = make_user()
    other = make_user()
    with _stub_tts(), _stub_channel_layer():
        result = dispatch_alert(
            user_id=user.id,
            farm_id=None,
            alert_type=AudioAlertType.ADMIN_BROADCAST,
            trigger_source="admin_view",
            title="hi",
            message="hi",
        )
    assert acknowledge_alert(user_id=user.id, alert_id=result.alert_id)
    assert (
        acknowledge_alert(user_id=user.id, alert_id=result.alert_id) is False
    )
    assert (
        acknowledge_alert(user_id=other.id, alert_id=result.alert_id) is False
    )


def test_list_alerts_for_user_filters_unread(
    make_user: Callable[..., Any],
) -> None:
    user = make_user()
    with _stub_tts(), _stub_channel_layer():
        a = dispatch_alert(
            user_id=user.id,
            farm_id=None,
            alert_type=AudioAlertType.ADMIN_BROADCAST,
            trigger_source="admin_view",
            title="t",
            message="m",
        )
        b = dispatch_alert(
            user_id=user.id,
            farm_id=None,
            alert_type=AudioAlertType.ADMIN_BROADCAST,
            trigger_source="admin_view",
            title="t",
            message="m",
        )
    acknowledge_alert(user_id=user.id, alert_id=a.alert_id)
    all_rows = list_alerts_for_user(user_id=user.id)
    unread = list_alerts_for_user(user_id=user.id, only_unacknowledged=True)
    assert {r.id for r in all_rows} == {a.alert_id, b.alert_id}
    assert {r.id for r in unread} == {b.alert_id}


def test_has_subscription_checks_alert_types_list(
    make_user: Callable[..., Any], make_farm: Callable[..., Any]
) -> None:
    user = make_user()
    farm = make_farm(user)
    AudioAlertSubscription.objects.create(
        user=user, farm=farm, alert_types=[AudioAlertType.NDVI_LOW]
    )
    assert has_subscription(
        user_id=user.id,
        farm_id=farm.id,
        alert_type=AudioAlertType.NDVI_LOW,
    )
    assert not has_subscription(
        user_id=user.id,
        farm_id=farm.id,
        alert_type=AudioAlertType.NDVI_DECLINE,
    )


def test_subscribed_users_for_farm_returns_matching_subs(
    make_user: Callable[..., Any], make_farm: Callable[..., Any]
) -> None:
    owner = make_user()
    other = make_user()
    farm = make_farm(owner)
    AudioAlertSubscription.objects.create(
        user=owner,
        farm=farm,
        alert_types=[AudioAlertType.NDVI_LOW],
    )
    AudioAlertSubscription.objects.create(
        user=other,
        farm=farm,
        alert_types=[AudioAlertType.NDVI_DECLINE],
    )
    rows = list(
        subscribed_users_for_farm(
            farm_id=farm.id, alert_type=AudioAlertType.NDVI_LOW
        )
    )
    assert {r.user_id for r in rows} == {owner.id}


def test_dispatch_alert_swallows_tts_failure(
    make_user: Callable[..., Any],
) -> None:
    user = make_user()
    with (
        patch(
            "alerts.services.synthesize",
            side_effect=RuntimeError("tts down"),
        ),
        _stub_channel_layer(),
    ):
        result = dispatch_alert(
            user_id=user.id,
            farm_id=None,
            alert_type=AudioAlertType.ADMIN_BROADCAST,
            trigger_source="admin_view",
            title="t",
            message="m",
        )
    assert result.delivered is True
    alert = AudioAlert.objects.get(id=result.alert_id)
    assert alert.audio_file == ""
    assert alert.duration_ms == 0
