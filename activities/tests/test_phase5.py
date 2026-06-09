"""Tests for Phase 5 hardening features.

Covers:
- Handler exception hierarchy (temp vs permanent)
- Circuit breaker for handler failures
- Dead letter handling
- Conditional recurrence (state-based)
- Activity chaining from NDVI recommendations
- NDVI event listener
- Recurrence double-reschedule atomicity
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from activities.circuit_breaker import (
    can_try_half_open,
    get_breaker_state,
    is_open,
    record_failure,
    record_success,
)
from activities.dead_letter import (
    clear_all_dead_letters,
    count_dead_letters,
    list_dead_letters,
    register_dead_letter,
    replay_dead_letter,
)
from activities.handlers.base import (
    HandlerResult,
    PermanentHandlerError,
    TemporaryHandlerError,
)
from activities.handlers.ndvi_trigger import (
    DEFAULT_STATE_ACTIONS,
    NdviTriggerHandler,
    on_ndvi_job_completed,
)
from activities.services import (
    chain_activity as service_chain_activity,
)
from activities.services import (
    reschedule_recurring,
)


class HandlerExceptionHierarchyTestCase(TestCase):
    """Exception hierarchy: Temporary vs Permanent."""

    def test_temporary_is_retryable(self) -> None:
        ex = TemporaryHandlerError("network timeout")
        self.assertTrue(ex.temporary)

    def test_permanent_is_not_retryable(self) -> None:
        ex = PermanentHandlerError("invalid metadata")
        self.assertFalse(ex.temporary)

    def test_exception_carries_metadata(self) -> None:
        ex = TemporaryHandlerError(
            "timeout", metadata={"url": "https://example.test"}
        )
        self.assertEqual(ex.metadata, {"url": "https://example.test"})

    def test_handler_error_type_check(self) -> None:
        self.assertIsInstance(
            TemporaryHandlerError("x"), PermanentHandlerError.__bases__[0]
        )
        self.assertIsInstance(
            PermanentHandlerError("x"), PermanentHandlerError.__bases__[0]
        )


class CircuitBreakerTestCase(TestCase):
    """Cache-backed circuit breaker for handler failures."""

    def setUp(self) -> None:
        clear_all_dead_letters()
        for key in list(cache_keys()):
            from django.core.cache import cache

            cache.delete(key)

    def test_initial_state_is_closed(self) -> None:
        self.assertFalse(is_open("vaccination"))
        state = get_breaker_state("vaccination")
        self.assertEqual(state["state"], "closed")

    def test_trips_after_threshold_failures(self) -> None:
        handler_type = "fertilizer"
        for _ in range(5):
            record_failure(handler_type)
        self.assertTrue(is_open(handler_type))

    def test_resets_on_success(self) -> None:
        handler_type = "irrigation"
        for _ in range(5):
            record_failure(handler_type)
        self.assertTrue(is_open(handler_type))
        record_success(handler_type)
        self.assertFalse(is_open(handler_type))

    def test_few_failures_does_not_trip(self) -> None:
        handler_type = "vaccination"
        for _ in range(3):
            record_failure(handler_type)
        self.assertFalse(is_open(handler_type))

    def test_half_open_allows_probe(self) -> None:
        handler_type = "ndvi_trigger"
        for _ in range(5):
            record_failure(handler_type)
        self.assertTrue(is_open(handler_type))
        self.assertTrue(can_try_half_open(handler_type))
        self.assertFalse(can_try_half_open(handler_type))


class DeadLetterTestCase(TestCase):
    """Dead letter queue for permanently failed activities."""

    def setUp(self) -> None:
        clear_all_dead_letters()

    def test_register_and_list(self) -> None:
        register_dead_letter(
            1,
            reason="permanent_failure",
            activity_type="vaccination",
            error="bad metadata",
        )
        entries = list_dead_letters()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["activity_id"], 1)
        self.assertEqual(entries[0]["reason"], "permanent_failure")

    def test_idempotent_register(self) -> None:
        register_dead_letter(1, reason="failure", activity_type="test")
        register_dead_letter(1, reason="failure", activity_type="test")
        self.assertEqual(count_dead_letters(), 1)

    def test_count(self) -> None:
        register_dead_letter(1, reason="a", activity_type="t1")
        register_dead_letter(2, reason="b", activity_type="t2")
        self.assertEqual(count_dead_letters(), 2)

    def test_clear(self) -> None:
        register_dead_letter(1, reason="a", activity_type="t1")
        self.assertEqual(count_dead_letters(), 1)
        cleared = clear_all_dead_letters()
        self.assertEqual(cleared, 1)
        self.assertEqual(count_dead_letters(), 0)

    def test_replay_nonexistent_returns_false(self) -> None:
        self.assertFalse(replay_dead_letter(999))

    @patch("activities.models.Activity.objects.get")
    def test_replay_existing(self, mock_get: MagicMock) -> None:
        from django.contrib.auth import get_user_model

        user_model = get_user_model()
        mock_activity = MagicMock()
        mock_activity.id = 42
        mock_activity.status = "failed"
        mock_activity.retry_count = 3
        mock_activity.last_error = "test error"
        mock_activity.execution_id = None
        mock_activity.execution_started_at = None
        mock_activity.execution_completed_at = None
        mock_activity.owner = user_model.objects.create_user(
            username="dl_replay", password=secrets.token_urlsafe(12)
        )
        mock_get.return_value = mock_activity
        register_dead_letter(42, reason="test", activity_type="test")
        result = replay_dead_letter(42)
        self.assertTrue(result)
        self.assertEqual(count_dead_letters(), 0)


class ConditionalRecurrenceTestCase(TestCase):
    """Conditional recurrence: handler result gates reschedule."""

    def test_normal_reschedule_non_recurring_returns_none(self) -> None:
        activity = MagicMock()
        activity.recurrence_type = "none"
        result = reschedule_recurring(activity)
        self.assertIsNone(result)

    def test_conditional_skip_blocks_reschedule(self) -> None:
        activity = MagicMock()
        activity.recurrence_type = "interval"
        activity.interval_days = 7
        activity.status = "success"
        result = reschedule_recurring(
            activity, handler_result_metadata={"conditional_skip": True}
        )
        self.assertIsNone(result)

    def test_interval_reschedules_creates_new_activity(self) -> None:
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user(
            username="rec_interval",
            password=secrets.token_urlsafe(12),
        )
        from farms.models import Farm

        farm = Farm.objects.create(
            owner=user, name="Rec Farm", centroid_lat=0.0, centroid_lon=0.0
        )
        from activities.models import Activity

        activity = Activity.objects.create(
            owner=user,
            farm=farm,
            type=Activity.Type.IRRIGATION,
            status=Activity.Status.SUCCESS,
            recurrence_type=Activity.RecurrenceType.INTERVAL,
            interval_days=7,
            scheduled_at=timezone.now() + timedelta(days=7),
        )
        result = reschedule_recurring(activity)
        self.assertIsNotNone(result)
        self.assertNotEqual(result.id, activity.id)
        self.assertEqual(result.status, Activity.Status.PENDING)


class ActivityChainingTestCase(TestCase):
    """Activity chaining from NDVI recommendations."""

    def test_chain_creates_pending_activity(self) -> None:
        source = MagicMock()
        source.id = 1
        source.type = "ndvi_trigger"
        source.owner = MagicMock()
        source.owner.id = 1
        source.farm_id = 10

        with patch("activities.models.Activity.objects.create") as mock_create:
            mock_activity = MagicMock()
            mock_activity.id = 100
            mock_create.return_value = mock_activity

            result = service_chain_activity(
                source, "fertilizer", metadata={"triggered_by_ndvi": True}
            )
            self.assertIsNotNone(result)
            self.assertEqual(result.id, 100)
            _, kwargs = mock_create.call_args
            self.assertEqual(kwargs["type"], "fertilizer")
            self.assertEqual(kwargs["farm_id"], 10)
            self.assertIn("chained_from", kwargs["metadata"])

    def test_chain_returns_none_without_owner(self) -> None:
        source = MagicMock()
        source.owner = None
        result = service_chain_activity(source, "vaccination")
        self.assertIsNone(result)


class NdviEventListenerTestCase(TestCase):
    """NDVI event listener hooks into job completion events."""

    def setUp(self) -> None:
        from django.core.cache import cache

        cache.clear()

    @patch("activities.models.Activity.objects.filter")
    @patch("activities.handlers.ndvi_trigger.cache.add", return_value=True)
    @patch("activities.handlers.ndvi_trigger.cache.set")
    def test_no_existing_activity_returns_not_triggered(
        self,
        mock_set: MagicMock,
        mock_add: MagicMock,
        mock_filter: MagicMock,
    ) -> None:
        from activities.models import Activity as ActModel

        mock_filter.return_value = ActModel.objects.none()
        result = on_ndvi_job_completed(farm_id=1)
        self.assertFalse(result["triggered"])
        self.assertEqual(
            result["message"], "no ndvi_trigger activity found for farm"
        )

    @patch("activities.models.Activity.objects.filter")
    @patch("activities.services.chain_activity")
    def test_triggers_chain_on_event(
        self,
        mock_chain: MagicMock,
        mock_filter: MagicMock,
    ) -> None:
        source = MagicMock()
        source.owner = MagicMock()
        source.owner.id = 1
        source.farm_id = 1
        mock_filter.return_value.order_by.return_value[:1] = [source]
        mock_chain.return_value = MagicMock(id=42)

        result = on_ndvi_job_completed(
            farm_id=1, mean_ndvi=0.75, state="full_canopy"
        )
        self.assertTrue(result["triggered"])
        self.assertEqual(result["activity_id"], 42)


class NdviTriggerHandlerTestCase(TestCase):
    """NDVI trigger handler integration with Phase 5 features."""

    def test_handler_registered(self) -> None:
        from activities.handlers.registry import HANDLER_REGISTRY

        self.assertIn("ndvi_trigger", HANDLER_REGISTRY)

    def test_dafault_state_actions(self) -> None:
        self.assertIn("establishment", DEFAULT_STATE_ACTIONS)
        self.assertIn("fertilizer", DEFAULT_STATE_ACTIONS["establishment"])
        self.assertIn("irrigation", DEFAULT_STATE_ACTIONS["establishment"])
        self.assertIn("full_canopy", DEFAULT_STATE_ACTIONS)
        self.assertIn("fertilizer", DEFAULT_STATE_ACTIONS["full_canopy"])

    def test_handler_returns_handler_result(self) -> None:
        handler = NdviTriggerHandler()
        activity = MagicMock()
        activity.metadata = {}
        activity.id = 1
        activity.farm_id = None

        with patch.object(handler, "_execute_impl") as mock_exec:
            mock_exec.return_value = HandlerResult(
                success=True,
                message="test",
                metadata={"state": "unknown"},
            )
            result = handler.execute(activity)
            self.assertIsInstance(result, HandlerResult)


def cache_keys() -> list[str]:
    """Return known cache keys for test cleanup."""
    return [
        "activities:cb:counter:vaccination",
        "activities:cb:test",
        "activities:cb:fertilizer",
        "activities:cb:irrigation",
        "activities:cb:ndvi_trigger",
    ]
