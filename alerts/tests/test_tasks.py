"""Tests for the alerts Celery tasks (NDVI scans)."""

from __future__ import annotations

import secrets
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model

from alerts.models import AudioAlert, AudioAlertType
from alerts.tasks import scan_low_ndvi_observations, scan_ndvi_declines
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
