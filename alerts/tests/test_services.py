"""Tests for the alerts service layer.

These tests run with the in-memory channel layer and a stubbed TTS
backend so the WebSocket and audio paths are exercised end-to-end.
Celery runs in eager mode (``CELERY_TASK_ALWAYS_EAGER`` is True in
``IS_TESTING``), so :func:`alerts.services.dispatch_alert` actually
runs :func:`alerts.tasks.render_alert_audio` synchronously.
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
    dispatch_alert_fast,
    group_name_for_user,
    has_subscription,
    list_alerts_for_user,
    subscribed_users_for_farm,
)

pytestmark = pytest.mark.django_db


def _stub_render_task() -> Any:
    """Patch :func:`alerts.tasks.render_alert_audio` to a noop stub.

    The real task runs in eager mode, which would call the real TTS
    pipeline. Tests that only care about the dispatch path can swap
    it out for a stub that records the call.
    """
    from unittest.mock import MagicMock

    from alerts import tasks

    return patch.object(tasks.render_alert_audio, "delay", MagicMock())


def _stub_channel_layer() -> Any:
    """Stub the Channels layer so the sync test loop can call into it."""
    from unittest.mock import AsyncMock, MagicMock

    layer = MagicMock()
    layer.group_send = AsyncMock(return_value=None)
    return patch("alerts.services.get_channel_layer", return_value=layer)


def test_group_name_for_user_uses_settings_prefix() -> None:
    with override_settings(ALERTS_WEBHOOK_GROUP_PREFIX="user_"):
        assert group_name_for_user(42) == "user_42"


def test_dispatch_alert_fast_creates_row_and_pushes(
    make_user: Callable[..., Any], make_farm: Callable[..., Any]
) -> None:
    user = make_user()
    farm = make_farm(user)
    with _stub_render_task(), _stub_channel_layer():
        result = dispatch_alert_fast(
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
    # No TTS run yet (the render task is stubbed), so the audio file
    # is empty until the celery task completes.
    assert not alert.audio_file


def test_dispatch_alert_enqueues_render(
    make_user: Callable[..., Any], make_farm: Callable[..., Any]
) -> None:
    user = make_user()
    farm = make_farm(user)
    from unittest.mock import MagicMock

    from alerts import tasks

    delay_mock = MagicMock()
    with (
        patch.object(tasks.render_alert_audio, "delay", delay_mock),
        _stub_channel_layer(),
    ):
        result = dispatch_alert(
            user_id=user.id,
            farm_id=farm.id,
            alert_type=AudioAlertType.NDVI_LOW,
            trigger_source="ndvi_task",
            title="Low NDVI",
            message="Mean is 0.18",
        )
    delay_mock.assert_called_once_with(str(result.alert_id))


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
    with _stub_render_task(), _stub_channel_layer():
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
    with _stub_render_task(), _stub_channel_layer():
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
