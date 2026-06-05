"""Tests for the DB-aware ``alert_types`` filter.

Per ``prompts/p4-staff-engineer-review.md`` #9 the JSON-array
filter on ``AudioAlertSubscription.alert_types`` should be
pushed into SQL on Postgres (where ``JSONField __contains=[...]``
works) and computed in Python on SQLite / MySQL (where the ORM
refuses the array operand).

These tests pin the contract on whichever backend the suite is
running against, and exercise both the helper short-circuit and
the production call sites.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from alerts.models import (
    AudioAlertSubscription,
    AudioAlertType,
)
from alerts.services import (
    _json_contains_supports_array,
    has_subscription,
    subscribed_users_for_farm,
)

User = get_user_model()


def _make_user() -> Any:
    import secrets

    return User.objects.create_user(
        username=f"u-{secrets.token_urlsafe(8)}",
        password=secrets.token_urlsafe(16),
    )


def _make_farm(owner: Any) -> Any:
    import secrets

    from farms.models import Farm

    return Farm.objects.create(
        owner=owner,
        name=f"f-{secrets.token_urlsafe(6)}",
        centroid_lat=0.0,
        centroid_lon=0.0,
    )


def _make_sub(
    *, user: Any, farm: Any, alert_types: list[str]
) -> AudioAlertSubscription:
    return AudioAlertSubscription.objects.create(
        user=user, farm=farm, alert_types=alert_types
    )


# --- _json_contains_supports_array ----------------------------------------


class JsonContainsSupportsArrayTests(TestCase):
    def test_postgres_vendor_returns_true(self) -> None:
        with patch("alerts.services.connection") as mocked_conn:
            mocked_conn.vendor = "postgresql"
            assert _json_contains_supports_array() is True

    def test_sqlite_vendor_returns_false(self) -> None:
        with patch("alerts.services.connection") as mocked_conn:
            mocked_conn.vendor = "sqlite"
            assert _json_contains_supports_array() is False

    def test_mysql_vendor_returns_false(self) -> None:
        with patch("alerts.services.connection") as mocked_conn:
            mocked_conn.vendor = "mysql"
            assert _json_contains_supports_array() is False

    def test_unknown_vendor_returns_false(self) -> None:
        """Be conservative: only Postgres is known-good."""
        with patch("alerts.services.connection") as mocked_conn:
            mocked_conn.vendor = "oracle"
            assert _json_contains_supports_array() is False


# --- has_subscription -----------------------------------------------------


class HasSubscriptionTests(TestCase):
    def test_returns_true_when_alert_type_is_in_subscription(self) -> None:
        user = _make_user()
        farm = _make_farm(user)
        _make_sub(
            user=user,
            farm=farm,
            alert_types=[
                AudioAlertType.NDVI_DECLINE,
                AudioAlertType.NDVI_LOW,
            ],
        )
        assert has_subscription(
            user_id=user.id,
            farm_id=farm.id,
            alert_type=AudioAlertType.NDVI_DECLINE,
        )

    def test_returns_false_when_alert_type_is_not_in_subscription(
        self,
    ) -> None:
        user = _make_user()
        farm = _make_farm(user)
        _make_sub(
            user=user,
            farm=farm,
            alert_types=[AudioAlertType.NDVI_DECLINE],
        )
        assert not has_subscription(
            user_id=user.id,
            farm_id=farm.id,
            alert_type=AudioAlertType.NDVI_LOW,
        )

    def test_returns_false_when_no_subscription_row(self) -> None:
        user = _make_user()
        farm = _make_farm(user)
        assert not has_subscription(
            user_id=user.id,
            farm_id=farm.id,
            alert_type=AudioAlertType.NDVI_DECLINE,
        )


# --- subscribed_users_for_farm --------------------------------------------


class SubscribedUsersForFarmTests(TestCase):
    def test_returns_only_users_with_matching_alert_type(self) -> None:
        owner = _make_user()
        farm = _make_farm(owner)
        u1 = _make_user()
        u2 = _make_user()
        u3 = _make_user()
        _make_sub(
            user=u1,
            farm=farm,
            alert_types=[AudioAlertType.NDVI_DECLINE],
        )
        _make_sub(
            user=u2,
            farm=farm,
            alert_types=[
                AudioAlertType.NDVI_DECLINE,
                AudioAlertType.NDVI_LOW,
            ],
        )
        _make_sub(
            user=u3,
            farm=farm,
            alert_types=[AudioAlertType.NDVI_LOW],
        )
        result = subscribed_users_for_farm(
            farm_id=farm.id, alert_type=AudioAlertType.NDVI_DECLINE
        )
        user_ids = {s.user_id for s in result}
        assert user_ids == {u1.id, u2.id}

    def test_returns_empty_when_no_subscribers(self) -> None:
        owner = _make_user()
        farm = _make_farm(owner)
        _make_user()  # ensure non-empty users table
        result = subscribed_users_for_farm(
            farm_id=farm.id, alert_type=AudioAlertType.NDVI_DECLINE
        )
        assert result == []


# --- end-to-end via the call sites that production code uses ------------


class DispatchTriggerTests(TestCase):
    """Smoke test: the production call sites still produce the
    right number of dispatches on whichever backend the suite
    is running against.
    """

    def test_on_ndvi_decline_fans_out_to_subscribers(self) -> None:
        """The NDVI decline path uses the synchronous
        :func:`dispatch_alert` (not the Celery group used by
        admin broadcasts) and must dispatch to every
        subscriber plus the farm owner.
        """
        from alerts.triggers import on_ndvi_decline

        owner = _make_user()
        farm = _make_farm(owner)
        u1 = _make_user()
        _make_sub(
            user=u1,
            farm=farm,
            alert_types=[AudioAlertType.NDVI_DECLINE],
        )
        with patch("alerts.triggers.dispatch_alert") as mocked:
            n = on_ndvi_decline(
                farm_id=farm.id, owner_id=owner.id, message="x"
            )
        assert n == 2  # u1 + owner
        assert mocked.call_count == 2
        user_ids = {c.kwargs["user_id"] for c in mocked.call_args_list}
        assert user_ids == {u1.id, owner.id}
