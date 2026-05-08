import secrets
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from activities.models import Activity
from activities.services import (
    ActivityStateMachine,
    InvalidTransitionError,
    StaleExecutionError,
    claim_activity,
    recover_stale_activity,
    transition_to_failed,
    transition_to_retry,
    transition_to_running,
    transition_to_success,
    validate_execution,
)

TEST_PASSWORD = secrets.token_urlsafe(16)


@pytest.mark.django_db
class TestActivityAPI(TestCase):
    def setUp(self) -> None:
        self.client = APIClient()
        from django.contrib.auth import get_user_model

        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="testuser",
            email="test@example.com",
            password=TEST_PASSWORD,
        )
        self.client.force_authenticate(user=self.user)

        from farms.models import Farm

        self.farm = Farm.objects.create(
            name="Test Farm",
            slug="test-farm",
            owner=self.user,
            centroid_lat=0.0,
            centroid_lon=0.0,
        )

    def test_create_activity_success(self) -> None:
        data = {
            "type": "vaccination",
            "scheduled_at": (timezone.now() + timedelta(days=1)).isoformat(),
            "farm": self.farm.id,
            "metadata": {"cattle_id": 123},
        }
        response = self.client.post("/api/v1/activities/", data, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["status"], 0)
        self.assertEqual(response.data["data"]["type"], "vaccination")

    def test_create_activity_with_recurrence(self) -> None:
        data = {
            "type": "fertilizer",
            "scheduled_at": (timezone.now() + timedelta(days=1)).isoformat(),
            "recurrence_type": "interval",
            "interval_days": 30,
            "farm": self.farm.id,
            "metadata": {"amount_kg": 50},
        }
        response = self.client.post("/api/v1/activities/", data, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["data"]["recurrence_type"], "interval")
        self.assertEqual(response.data["data"]["interval_days"], 30)

    def test_create_activity_invalid_type(self) -> None:
        data = {
            "type": "invalid_type",
            "scheduled_at": (timezone.now() + timedelta(days=1)).isoformat(),
        }
        response = self.client.post("/api/v1/activities/", data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_activity_missing_interval_days(self) -> None:
        data = {
            "type": "fertilizer",
            "scheduled_at": (timezone.now() + timedelta(days=1)).isoformat(),
            "recurrence_type": "interval",
        }
        response = self.client.post("/api/v1/activities/", data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_list_activities(self) -> None:
        Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            scheduled_at=timezone.now() + timedelta(days=1),
        )
        response = self.client.get("/api/v1/activities/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)

    def test_list_activities_user_isolation(self) -> None:
        Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        from django.contrib.auth import get_user_model

        user_model = get_user_model()
        other_user = user_model.objects.create_user(
            username="other",
            email="other@example.com",
            password=TEST_PASSWORD,
        )
        self.client.force_authenticate(user=other_user)

        response = self.client.get("/api/v1/activities/")
        self.assertEqual(len(response.data["data"]), 0)

    def test_retrieve_activity(self) -> None:
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            scheduled_at=timezone.now() + timedelta(days=1),
        )
        response = self.client.get(f"/api/v1/activities/{activity.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["type"], "vaccination")

    def test_update_activity(self) -> None:
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            scheduled_at=timezone.now() + timedelta(days=1),
        )
        response = self.client.patch(
            f"/api/v1/activities/{activity.id}/",
            {"metadata": {"cattle_id": 456}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["metadata"]["cattle_id"], 456)

    def test_delete_activity(self) -> None:
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            scheduled_at=timezone.now() + timedelta(days=1),
        )
        response = self.client.delete(f"/api/v1/activities/{activity.id}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Activity.objects.filter(id=activity.id).exists())

    def test_activity_unauthorized(self) -> None:
        self.client.logout()
        response = self.client.get("/api/v1/activities/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@pytest.mark.django_db
class TestActivityServices(TestCase):
    def setUp(self) -> None:
        from django.contrib.auth import get_user_model

        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="testuser",
            email="test@example.com",
            password=TEST_PASSWORD,
        )

        from farms.models import Farm

        self.farm = Farm.objects.create(
            name="Test Farm",
            slug="test-farm",
            owner=self.user,
            centroid_lat=0.0,
            centroid_lon=0.0,
        )

    def test_claim_activity_success(self) -> None:
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.PENDING,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        claimed, execution_id = claim_activity(activity.id)

        self.assertIsNotNone(claimed)
        self.assertIsNotNone(execution_id)
        self.assertEqual(claimed.status, Activity.Status.DISPATCHED)
        self.assertIsNotNone(claimed.execution_id)

    def test_claim_activity_contention(self) -> None:
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.DISPATCHED,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        claimed, execution_id = claim_activity(activity.id)

        self.assertIsNone(claimed)
        self.assertIsNone(execution_id)

    def test_validate_execution_success(self) -> None:
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.PENDING,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        claimed, execution_id = claim_activity(activity.id)
        validated = validate_execution(claimed.id, execution_id)

        self.assertEqual(validated.id, activity.id)

    def test_validate_execution_stale(self) -> None:
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.PENDING,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        claimed, _ = claim_activity(activity.id)

        with self.assertRaises(StaleExecutionError):
            validate_execution(claimed.id, "invalid-execution-id")

    def test_transition_to_running(self) -> None:
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.DISPATCHED,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        transitioned = transition_to_running(activity)

        self.assertEqual(transitioned.status, Activity.Status.RUNNING)

    def test_transition_to_success(self) -> None:
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.RUNNING,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        transitioned = transition_to_success(activity)

        self.assertEqual(transitioned.status, Activity.Status.SUCCESS)
        self.assertIsNotNone(transitioned.execution_completed_at)

    def test_transition_to_failed(self) -> None:
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.RUNNING,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        transitioned = transition_to_failed(activity, "Test error")

        self.assertEqual(transitioned.status, Activity.Status.FAILED)
        self.assertEqual(transitioned.last_error, "Test error")

    def test_transition_to_retry(self) -> None:
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.RUNNING,
            retry_count=0,
            max_retries=3,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        next_due = timezone.now() + timedelta(hours=1)
        transitioned = transition_to_retry(activity, next_due)

        self.assertEqual(transitioned.status, Activity.Status.RETRY)
        self.assertEqual(transitioned.retry_count, 1)

    def test_recover_stale_activity(self) -> None:
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.RUNNING,
            retry_count=0,
            max_retries=3,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        recovered = recover_stale_activity(activity)

        self.assertEqual(recovered.status, Activity.Status.RETRY)
        self.assertEqual(recovered.retry_count, 1)

    def test_state_machine_valid_transition(self) -> None:
        self.assertTrue(
            ActivityStateMachine.can_transition(
                Activity.Status.PENDING, Activity.Status.DISPATCHED
            )
        )
        self.assertTrue(
            ActivityStateMachine.can_transition(
                Activity.Status.DISPATCHED, Activity.Status.RUNNING
            )
        )

    def test_state_machine_invalid_transition(self) -> None:
        self.assertFalse(
            ActivityStateMachine.can_transition(
                Activity.Status.PENDING, Activity.Status.RUNNING
            )
        )
        self.assertFalse(
            ActivityStateMachine.can_transition(
                Activity.Status.SUCCESS, Activity.Status.PENDING
            )
        )

    def test_state_machine_transition_enforcement(self) -> None:
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.PENDING,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        with self.assertRaises(InvalidTransitionError):
            ActivityStateMachine.transition(activity, Activity.Status.RUNNING)


@pytest.mark.django_db
class TestActivityHandlers(TestCase):
    """Phase 2 tests for handler registry."""

    def setUp(self) -> None:
        from django.contrib.auth import get_user_model

        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="testuser",
            email="test@example.com",
            password=TEST_PASSWORD,
        )

        from farms.models import Farm

        self.farm = Farm.objects.create(
            name="Test Farm",
            slug="test-farm",
            owner=self.user,
            centroid_lat=0.0,
            centroid_lon=0.0,
        )

    def test_handler_registry_get_handler(self) -> None:
        """Test get_handler returns handler for type."""
        from activities.handlers import DefaultHandler, get_handler

        handler = get_handler("vaccination")
        self.assertIsInstance(handler, DefaultHandler)

    def test_handler_execute(self) -> None:
        """Test handler execution."""
        from activities.handlers import get_handler

        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.PENDING,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        handler = get_handler("vaccination")
        result = handler.execute(activity)

        self.assertIn("vaccination", result)

    def test_default_handler(self) -> None:
        """Test default handler for unknown type."""
        from activities.handlers import DefaultHandler

        handler = DefaultHandler("unknown_type")
        self.assertEqual(handler.type, "unknown_type")

        result = handler.execute(None)
        self.assertIn("unknown_type", result)

    def test_register_handler_decorator(self) -> None:
        """Test handler registration decorator."""
        from activities.handlers import (  # noqa: I001
            ActivityHandler,
            HANDLER_REGISTRY,
            register_handler,
        )

        class TestHandler(ActivityHandler):
            type = "test_type"

            def execute(self, activity: Any) -> str:  # type: ignore[override]
                return "test_result"

        registered = register_handler(TestHandler)
        self.assertEqual(registered, TestHandler)
        self.assertIn("test_type", HANDLER_REGISTRY)


@pytest.mark.django_db
class TestActivityTasks(TestCase):
    """Phase 2 tests for Celery tasks."""

    def setUp(self) -> None:
        from django.contrib.auth import get_user_model

        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="testuser",
            email="test@example.com",
            password=TEST_PASSWORD,
        )

        from farms.models import Farm

        self.farm = Farm.objects.create(
            name="Test Farm",
            slug="test-farm",
            owner=self.user,
            centroid_lat=0.0,
            centroid_lon=0.0,
        )

    def test_poll_activities_returns_dict(self) -> None:
        """Test poll_activities returns proper dict structure."""
        from activities.tasks import poll_activities

        result = poll_activities()
        self.assertIn("dispatched", result)
        self.assertIn("scanned", result)

    def test_claim_and_dispatch_returns_tuple(self) -> None:
        """Test _claim_and_dispatch returns tuple."""
        from activities.tasks import _claim_and_dispatch

        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.PENDING,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        with patch("activities.tasks.execute_activity.delay") as mock_delay:
            activity, execution_id = _claim_and_dispatch(activity.id)

        mock_delay.assert_called_once_with(activity.id, execution_id)
        self.assertIsNotNone(activity)
        self.assertIsNotNone(execution_id)

    def test_validate_and_execute_calls_handlers(self) -> None:
        """Test execute_activity runs handler."""
        from activities.tasks import _claim_and_dispatch, execute_activity

        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.PENDING,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        with patch("activities.tasks.execute_activity.delay") as mock_delay:
            activity, execution_id = _claim_and_dispatch(activity.id)

        mock_delay.assert_called_once_with(activity.id, execution_id)

        result = execute_activity(activity.id, execution_id)

        self.assertIn(
            result["status"],
            [
                "success",
            ],
        )
        activity.refresh_from_db()
        self.assertEqual(activity.status, Activity.Status.SUCCESS)

    def test_recover_stale_activities(self) -> None:
        """Test recover_stale_activities function."""
        from activities.tasks import recover_stale_activities

        result = recover_stale_activities()
        self.assertIn("recovered", result)

    def test_get_handler_function(self) -> None:
        """Test _get_handler returns handler."""
        from activities.tasks import _get_handler

        handler = _get_handler("vaccination")
        self.assertIsNotNone(handler)
        self.assertTrue(hasattr(handler, "execute"))

    def test_handler_instantiation_from_registry(self) -> None:
        """Test handler is instantiated when retrieved."""
        from activities.handlers import (  # noqa: I001
            ActivityHandler,
            register_handler,
        )

        class CustomHandler(ActivityHandler):
            type = "custom"

            def execute(self, activity: Any) -> str:  # type: ignore[override]
                return "custom_result"

        register_handler(CustomHandler)

        from activities.handlers import get_handler

        handler = get_handler("custom")
        self.assertIsInstance(handler, CustomHandler)
        result = handler.execute(None)
        self.assertEqual(result, "custom_result")

    def test_activity_handler_base_execute(self) -> None:
        """Test base ActivityHandler execute method."""
        from activities.handlers import ActivityHandler

        handler = ActivityHandler()
        self.assertEqual(handler.type, "base")
        result = handler.execute(None)
        self.assertEqual(result, "Executed base")


@pytest.mark.django_db
class TestValidationGuards(TestCase):
    """Test strengthened validation guards."""

    def setUp(self) -> None:
        from django.contrib.auth import get_user_model

        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="testuser",
            email="test@example.com",
            password=TEST_PASSWORD,
        )

        from farms.models import Farm

        self.farm = Farm.objects.create(
            name="Test Farm",
            slug="test-farm",
            owner=self.user,
            centroid_lat=0.0,
            centroid_lon=0.0,
        )

    def test_validate_rejects_terminal_status(self) -> None:
        """Test validate_execution rejects terminal states."""
        import uuid as uuid_module

        from activities.services import (
            InvalidTransitionError,
            validate_execution,
        )

        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.SUCCESS,
            execution_id=uuid_module.uuid4(),
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        with self.assertRaises(InvalidTransitionError):
            validate_execution(activity.id, str(activity.execution_id))

    def test_validate_rejects_none_execution_id(self) -> None:
        """Test validate_execution rejects None execution_id."""
        from activities.services import (
            StaleExecutionError,
            validate_execution,
        )

        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.DISPATCHED,
            execution_id=None,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        with self.assertRaises(StaleExecutionError):
            validate_execution(activity.id, "any-id")

    def test_recovery_clears_execution_id(self) -> None:
        """Test recover_stale_activity clears execution_id."""
        import uuid as uuid_module

        from activities.services import recover_stale_activity

        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.RUNNING,
            execution_id=uuid_module.uuid4(),
            retry_count=0,
            max_retries=3,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        recovered = recover_stale_activity(activity)

        self.assertIsNone(recovered.execution_id)
        self.assertEqual(recovered.status, Activity.Status.RETRY)


class TestConcurrency(TransactionTestCase):
    """Test concurrent claim_activity calls.

    Note: This test uses real concurrency.
    SQLite may fail due to table locks.
    PostgreSQL is required for proper testing.
    """

    def setUp(self) -> None:
        from django.contrib.auth import get_user_model

        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="testuser",
            email="test@example.com",
            password=TEST_PASSWORD,
        )

        from farms.models import Farm

        self.farm = Farm.objects.create(
            name="Test Farm",
            slug="test-farm",
            owner=self.user,
            centroid_lat=0.0,
            centroid_lon=0.0,
        )

    def test_concurrent_claim_single_success(self) -> None:
        """Test exactly one claim succeeds under concurrent access.

        Uses ThreadPoolExecutor to simulate parallel workers.
        Validates that the integration works correctly.

        Note: SQLite may fail with "database table is locked".
        This test requires PostgreSQL for proper validation.
        """
        from concurrent.futures import ThreadPoolExecutor

        from activities.services import claim_activity

        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.PENDING,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        def attempt_claim() -> tuple:
            try:
                return claim_activity(activity.id)
            except Exception:  # noqa: BLE001
                return (None, None)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(attempt_claim) for _ in range(2)]
            results = [f.result() for f in futures]

        successes = [r for r in results if r[0] is not None]

        self.assertEqual(len(successes), 1)
        self.assertIsNotNone(successes[0][1])

# Phase 3 Tests
@pytest.mark.django_db
class TestPhase3Handlers(TestCase):
    """Test Phase 3 activity handlers."""

    def setUp(self) -> None:
        """Set up test data."""
        from django.contrib.auth import get_user_model

        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="testuser",
            email="test@example.com",
            password=TEST_PASSWORD,
        )

        from farms.models import Farm

        self.farm = Farm.objects.create(
            name="Test Farm",
            slug="test-farm",
            owner=self.user,
            centroid_lat=0.0,
            centroid_lon=0.0,
        )

    def test_vaccination_handler_registered(self) -> None:
        """Test vaccination handler is registered."""
        from activities.handlers import get_handler

        handler = get_handler("vaccination")
        self.assertIsNotNone(handler)
        self.assertEqual(handler.type, "vaccination")

    def test_fertilizer_handler_registered(self) -> None:
        """Test fertilizer handler is registered."""
        from activities.handlers import get_handler

        handler = get_handler("fertilizer")
        self.assertIsNotNone(handler)
        self.assertEqual(handler.type, "fertilizer")

    def test_irrigation_handler_registered(self) -> None:
        """Test irrigation handler is registered."""
        from activities.handlers import get_handler

        handler = get_handler("irrigation")
        self.assertIsNotNone(handler)
        self.assertEqual(handler.type, "irrigation")

    def test_vaccination_handler_execute(self) -> None:
        """Test vaccination handler execution."""
        from activities.handlers import get_handler
        from activities.models import Activity

        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type="vaccination",
            status=Activity.Status.PENDING,
            scheduled_at=timezone.now() + timedelta(days=1),
            metadata={"cattle_id": "C123"},
        )

        handler = get_handler("vaccination")
        result = handler.execute(activity)

        from activities.handlers import HandlerResult

        self.assertIsInstance(result, HandlerResult)
        self.assertTrue(result.success)
        self.assertEqual(result.message, "Vaccination completed")
        self.assertEqual(result.metadata["cattle_id"], "C123")

    def test_fertilizer_handler_execute(self) -> None:
        """Test fertilizer handler execution."""
        from activities.handlers import get_handler
        from activities.models import Activity

        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type="fertilizer",
            status=Activity.Status.PENDING,
            scheduled_at=timezone.now() + timedelta(days=1),
            metadata={"amount_kg": 50, "fertilizer_type": "urea"},
        )

        handler = get_handler("fertilizer")
        result = handler.execute(activity)

        from activities.handlers import HandlerResult

        self.assertIsInstance(result, HandlerResult)
        self.assertTrue(result.success)
        self.assertEqual(result.message, "Fertilizer applied")
        self.assertEqual(result.metadata["amount_kg"], 50)

    def test_irrigation_handler_execute(self) -> None:
        """Test irrigation handler execution."""
        from activities.handlers import get_handler
        from activities.models import Activity

        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type="irrigation",
            status=Activity.Status.PENDING,
            scheduled_at=timezone.now() + timedelta(days=1),
            metadata={"duration_min": 20},
        )

        handler = get_handler("irrigation")
        result = handler.execute(activity)

        from activities.handlers import HandlerResult

        self.assertIsInstance(result, HandlerResult)
        self.assertTrue(result.success)
        self.assertEqual(result.message, "Irrigation completed")
