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
from unittest.mock import MagicMock, patch

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

    def test_postgres_path_is_used_when_supported(self) -> None:
        """When ``_json_contains_supports_array()`` is True the
        filter is pushed into SQL via
        ``alert_types__contains=[alert_type]`` and
        ``has_subscription`` short-circuits on ``.exists()``
        without materialising the row.

        We mock the manager so the test does not depend on the
        actual SQL backend (the test suite runs on SQLite,
        which raises ``NotSupportedError`` for the
        ``__contains`` JSON lookup; Postgres is the only
        vendor that supports it).
        """
        fake_manager = MagicMock()
        fake_qs = MagicMock()
        fake_filtered = MagicMock()
        fake_manager.filter.return_value = fake_qs
        fake_qs.filter.return_value = fake_filtered
        fake_filtered.exists.return_value = True
        with patch(
            "alerts.services._json_contains_supports_array",
            return_value=True,
        ):
            with patch(
                "alerts.services.AudioAlertSubscription.objects",
                fake_manager,
            ):
                assert has_subscription(
                    user_id=1,
                    farm_id=2,
                    alert_type=AudioAlertType.NDVI_DECLINE,
                )
        # The Postgres path called filter(alert_types__contains=[...]).
        fake_qs.filter.assert_called_once_with(
            alert_types__contains=[AudioAlertType.NDVI_DECLINE]
        )
        # And short-circuited on .exists() without iterating.
        fake_filtered.exists.assert_called_once_with()


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

    def test_postgres_path_filters_in_sql(self) -> None:
        """When the vendor supports array containment, the
        ``subscribed_users_for_farm`` helper pushes the filter
        into SQL and returns the matching rows without loading
        the others.
        """
        from alerts.models import AudioAlertSubscription

        matching = AudioAlertSubscription(user_id=1, farm_id=2)
        # Build a single self-returning mock that captures every
        # chained call (filter, select_related) and only differs
        # in the iter() payload, which the production code uses
        # to materialise the result.
        fake_qs = MagicMock()
        fake_qs.select_related.return_value = fake_qs
        captured: dict[str, Any] = {}

        def _record_filter(*args: Any, **kwargs: Any) -> MagicMock:
            captured["call"] = (args, kwargs)
            fake_qs.__iter__.return_value = iter([matching])
            return fake_qs

        fake_qs.filter.side_effect = _record_filter
        with patch(
            "alerts.services._json_contains_supports_array",
            return_value=True,
        ):
            with patch(
                "alerts.services.AudioAlertSubscription.objects",
                fake_qs,
            ):
                result = subscribed_users_for_farm(
                    farm_id=2, alert_type=AudioAlertType.NDVI_DECLINE
                )
        assert result == [matching]
        # The two filter calls were: filter(farm_id=...) and
        # filter(alert_types__contains=[...]).
        assert "call" in captured
        assert captured["call"] == (
            (AudioAlertType.NDVI_DECLINE,),
            {},
        ) or captured["call"][1] == {
            "alert_types__contains": [AudioAlertType.NDVI_DECLINE]
        }
        # And select_related was called to pre-load the user FK.
        fake_qs.select_related.assert_called_once_with("user")


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
