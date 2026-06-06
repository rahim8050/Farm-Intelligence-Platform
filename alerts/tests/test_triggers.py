"""Tests for the alerts trigger functions."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from alerts.models import AudioAlert, AudioAlertType
from alerts.triggers import (
    on_activity_completed,
    on_admin_broadcast,
    on_ndvi_decline,
    on_ndvi_low,
)

pytestmark = pytest.mark.django_db


def _stub_dispatch() -> Any:
    from uuid import UUID

    from alerts.services import AlertDispatchResult

    return patch(
        "alerts.triggers.dispatch_alert",
        return_value=AlertDispatchResult(
            alert_id=UUID("00000000-0000-0000-0000-000000000000"),
            delivered=True,
        ),
    )


def test_on_activity_completed_skips_non_terminal(make_user) -> None:
    user = make_user()
    with _stub_dispatch() as mocked:
        on_activity_completed(
            user_id=user.id,
            farm_id=None,
            activity_id=1,
            activity_type="irrigation",
            status="running",
            message="x",
        )
    mocked.assert_not_called()


def test_on_activity_completed_skips_when_farm_missing(make_user) -> None:
    user = make_user()
    with _stub_dispatch() as mocked:
        on_activity_completed(
            user_id=user.id,
            farm_id=None,
            activity_id=1,
            activity_type="irrigation",
            status="success",
            message="x",
        )
    mocked.assert_not_called()


def test_on_activity_completed_dispatches_to_subscribers(
    make_user, make_farm
) -> None:
    owner = make_user()
    other = make_user()
    farm = make_farm(owner)
    from alerts.models import AudioAlertSubscription

    AudioAlertSubscription.objects.create(
        user=other,
        farm=farm,
        alert_types=[AudioAlertType.ACTIVITY_COMPLETED],
    )
    with patch("alerts.triggers.dispatch_alert") as mocked:
        on_activity_completed(
            user_id=owner.id,
            farm_id=farm.id,
            activity_id=42,
            activity_type="irrigation",
            status="success",
            message="done",
        )
    assert mocked.call_count == 1
    kwargs = mocked.call_args.kwargs
    assert kwargs["farm_id"] == farm.id
    assert kwargs["alert_type"] == AudioAlertType.ACTIVITY_COMPLETED
    assert kwargs["user_id"] == other.id
    assert kwargs["message"] == "done"


def test_on_ndvi_decline_dispatches_to_owner_and_subscribers(
    make_user, make_farm
) -> None:
    owner = make_user()
    other = make_user()
    farm = make_farm(owner)
    from alerts.models import AudioAlertSubscription

    AudioAlertSubscription.objects.create(
        user=other,
        farm=farm,
        alert_types=[AudioAlertType.NDVI_DECLINE],
    )
    with patch("alerts.triggers.dispatch_alert") as mocked:
        n = on_ndvi_decline(
            farm_id=farm.id, owner_id=owner.id, message="decline!"
        )
    assert n == 2
    assert mocked.call_count == 2


def test_on_ndvi_low_returns_count(make_user, make_farm) -> None:
    owner = make_user()
    farm = make_farm(owner)
    with patch("alerts.triggers.dispatch_alert") as mocked:
        n = on_ndvi_low(farm_id=farm.id, owner_id=owner.id, message="low")
    assert n == 1
    assert mocked.call_args.kwargs["alert_type"] == (AudioAlertType.NDVI_LOW)


def test_on_admin_broadcast_dispatches_to_each_recipient(make_user) -> None:
    """The broadcast fans out one Celery task per recipient via a
    ``celery.group``; the inline ``dispatch_alert`` call is no
    longer reached on the happy path.
    """
    a = make_user()
    b = make_user()
    with patch("alerts.triggers.group") as mocked_group:
        mocked_group.return_value.apply_async.return_value = None
        n = on_admin_broadcast(recipients=[a.id, b.id], title="t", message="m")
    assert n == 2
    # The group was constructed once with both recipients.
    assert mocked_group.call_count == 1
    header = mocked_group.call_args.args[0]
    assert len(header) == 2
    # apply_async was called exactly once on the group.
    assert mocked_group.return_value.apply_async.call_count == 1


def test_on_admin_broadcast_fans_out_via_celery_group(make_user) -> None:
    """Per ``prompts/p4-staff-engineer-review.md`` #1 the broadcast
    enqueues one ``dispatch_one_alert`` task per recipient via a
    Celery ``group`` so a single slow TTS render does not block
    the others.
    """
    a = make_user()
    b = make_user()
    c = make_user()
    with patch("alerts.triggers.group") as mocked_group:
        mocked_group.return_value.apply_async.return_value = None
        n = on_admin_broadcast(
            recipients=[a.id, b.id, c.id], title="t", message="m"
        )
    assert n == 3
    # The group was constructed once with all three recipients.
    assert mocked_group.call_count == 1
    header = mocked_group.call_args.args[0]
    assert len(header) == 3
    # apply_async was called exactly once on the group.
    assert mocked_group.return_value.apply_async.call_count == 1
    # And no real alert rows were created in the DB (the task body
    # never executed; the group was stubbed).
    assert AudioAlert.objects.count() == 0


def test_on_admin_broadcast_falls_back_when_broker_down(make_user) -> None:
    """When the Celery broker is unreachable the function falls
    back to a synchronous in-process fan-out via
    :func:`alerts.services.dispatch_alert`. Each successful
    inline dispatch is counted; per-recipient failures are
    swallowed.
    """
    a = make_user()
    b = make_user()
    with patch(
        "alerts.triggers.group",
        side_effect=RuntimeError("broker down"),
    ):
        with patch("alerts.triggers.dispatch_alert") as mocked:
            n = on_admin_broadcast(
                recipients=[a.id, b.id], title="t", message="m"
            )
    assert n == 2
    assert mocked.call_count == 2


def test_on_admin_broadcast_fallback_partial_failure(make_user) -> None:
    """If the inline fallback fails for one user, the other
    users are still dispatched and the failure is logged but
    not propagated.
    """
    a = make_user()
    b = make_user()
    with patch(
        "alerts.triggers.group",
        side_effect=RuntimeError("broker down"),
    ):
        with patch(
            "alerts.triggers.dispatch_alert",
            side_effect=[None, RuntimeError("dispatch down")],
        ):
            n = on_admin_broadcast(
                recipients=[a.id, b.id], title="t", message="m"
            )
    # Only one of the two inline dispatches succeeded.
    assert n == 1
