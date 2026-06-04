"""Tests for the alerts ORM models."""

from __future__ import annotations

import secrets
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError

from alerts.models import (
    AudioAlert,
    AudioAlertSubscription,
    AudioAlertTriggerSource,
    AudioAlertType,
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


def test_subscription_unique_per_user_farm() -> None:
    user = _make_user()
    farm = _make_farm(user)
    AudioAlertSubscription.objects.create(
        user=user, farm=farm, alert_types=[AudioAlertType.NDVI_LOW]
    )
    with pytest.raises(IntegrityError):
        AudioAlertSubscription.objects.create(
            user=user, farm=farm, alert_types=[]
        )


def test_alert_defaults() -> None:
    user = _make_user()
    alert = AudioAlert.objects.create(
        user=user,
        alert_type=AudioAlertType.ADMIN_BROADCAST,
        trigger_source=AudioAlertTriggerSource.ADMIN_VIEW,
        title="t",
        message="m",
    )
    assert alert.is_delivered is False
    assert alert.is_acknowledged is False
    assert alert.duration_ms == 0
    assert alert.mime_type == ""


def test_alert_indexes_present() -> None:
    """The model declares the indexes from the design doc."""
    fields = [tuple(idx.fields) for idx in AudioAlert._meta.indexes]
    assert ("user", "-created_at") in fields
    assert ("user", "is_acknowledged") in fields
    assert ("alert_type", "-created_at") in fields
