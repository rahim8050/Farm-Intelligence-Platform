"""Tests for the alerts Celery tasks (NDVI scans + dispatch)."""

from __future__ import annotations

import secrets
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model

from alerts.models import (
    AudioAlert,
    AudioAlertTriggerSource,
    AudioAlertType,
)
from alerts.tasks import (
    dispatch_one_alert,
    scan_low_ndvi_observations,
    scan_ndvi_declines,
)
from farms.models import Farm

pytestmark = pytest.mark.django_db


def _make_user() -> Any:
    user_model = get_user_model()
    return user_model.objects.create_user(
        username=f"u-{secrets.token_urlsafe(8)}",
        password=secrets.token_urlsafe(16),
    )


def _make_farm(owner: Any) -> Farm:
    return Farm.objects.create(
        owner=owner, name="F", centroid_lat=0.0, centroid_lon=0.0
    )


def test_scan_ndvi_declines_fires_for_decline_state() -> None:
    owner = _make_user()
    farm = _make_farm(owner)
    decline_result = MagicMock(state="decline")
    with patch(
        "ndvi.farm_state.build_farm_state", return_value=decline_result
    ):
        with patch("alerts.triggers.on_ndvi_decline") as mocked:
            res = scan_ndvi_declines()
    assert res["farms_scanned"] == 1
    assert res["dispatched"] == 1
    mocked.assert_called_once()
    assert mocked.call_args.kwargs["farm_id"] == farm.id


def test_scan_ndvi_declines_skips_non_decline() -> None:
    owner = _make_user()
    _make_farm(owner)
    growth_result = MagicMock(state="growth")
    with patch("ndvi.farm_state.build_farm_state", return_value=growth_result):
        with patch("alerts.triggers.on_ndvi_decline") as mocked:
            res = scan_ndvi_declines()
    assert res["dispatched"] == 0
    mocked.assert_not_called()


def test_scan_ndvi_declines_dedupes_within_24h() -> None:
    owner = _make_user()
    farm = _make_farm(owner)
    AudioAlert.objects.create(
        user=owner,
        farm=farm,
        alert_type=AudioAlertType.NDVI_DECLINE,
        trigger_source="ndvi_task",
        title="old",
        message="old",
    )
    decline_result = MagicMock(state="decline")
    with patch(
        "ndvi.farm_state.build_farm_state", return_value=decline_result
    ):
        with patch("alerts.triggers.on_ndvi_decline") as mocked:
            res = scan_ndvi_declines()
    assert res["dispatched"] == 0
    mocked.assert_not_called()


def test_scan_low_ndvi_fires_when_below_threshold() -> None:
    owner = _make_user()
    _make_farm(owner)
    fake_obs = MagicMock(id=99, mean=0.15)
    with patch("ndvi.models.NdviObservation") as obs_cls:
        manager = MagicMock()
        first_mock = manager.valid.return_value.filter.return_value
        first_mock.order_by.return_value.first.return_value = fake_obs
        obs_cls.objects = manager
        with patch("alerts.triggers.on_ndvi_low") as mocked:
            res = scan_low_ndvi_observations()
    assert res["dispatched"] == 1
    assert mocked.call_args.kwargs["source_object_id"] == "99"


def test_scan_low_ndvi_skips_when_above_threshold() -> None:
    owner = _make_user()
    _make_farm(owner)
    fake_obs = MagicMock(id=99, mean=0.45)
    with patch("ndvi.models.NdviObservation") as obs_cls:
        manager = MagicMock()
        first_mock = manager.valid.return_value.filter.return_value
        first_mock.order_by.return_value.first.return_value = fake_obs
        obs_cls.objects = manager
        with patch("alerts.triggers.on_ndvi_low") as mocked:
            res = scan_low_ndvi_observations()
    assert res["dispatched"] == 0
    mocked.assert_not_called()


# --- dispatch_one_alert --------------------------------------------------
# Per prompts/p4-staff-engineer-review.md #1 the admin broadcast fans
# out one Celery task per recipient so a slow TTS render does not
# block the others.


def test_dispatch_one_alert_delivers_to_user() -> None:
    user = _make_user()
    res = dispatch_one_alert.run(
        user_id=user.id,
        farm_id=None,
        alert_type=AudioAlertType.ADMIN_BROADCAST,
        trigger_source=AudioAlertTriggerSource.ADMIN_VIEW,
        title="hi",
        message="hello",
    )
    assert res["user_id"] == user.id
    assert res["alert_id"]
    assert AudioAlert.objects.filter(user=user).count() == 1


def test_dispatch_one_alert_invalid_alert_type_does_not_retry() -> None:
    """``ValueError`` from ``dispatch_alert`` is a programmer error
    (unknown enum) and must NOT be retried; it surfaces immediately.
    """
    user = _make_user()
    with pytest.raises(ValueError):
        dispatch_one_alert.run(
            user_id=user.id,
            farm_id=None,
            alert_type="bogus_type",
            trigger_source=AudioAlertTriggerSource.ADMIN_VIEW,
            title="hi",
            message="hello",
        )
    # No row was created because the input is invalid.
    assert AudioAlert.objects.filter(user=user).count() == 0


def test_dispatch_one_alert_invalid_trigger_source_does_not_retry() -> None:
    """Same as above for ``trigger_source``."""
    user = _make_user()
    with pytest.raises(ValueError):
        dispatch_one_alert.run(
            user_id=user.id,
            farm_id=None,
            alert_type=AudioAlertType.ADMIN_BROADCAST,
            trigger_source="bogus_source",
            title="hi",
            message="hello",
        )
    assert AudioAlert.objects.filter(user=user).count() == 0


def test_dispatch_one_alert_is_configured_with_retry() -> None:
    """The task has autoretry_for=(OperationalError, ConnectionError,
    TimeoutError) with backoff + jitter so transient infrastructure
    failures self-heal, and dont_autoretry_for=(ValueError,) so
    programmer errors surface immediately.
    """
    assert dispatch_one_alert.max_retries == 2
    assert dispatch_one_alert.acks_late is True
    assert ValueError in dispatch_one_alert.dont_autoretry_for
