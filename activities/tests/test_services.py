"""Tests for activity services - state machine and transitions."""

import secrets
import uuid
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

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


class TestStateMachine(TestCase):
    """Test ActivityStateMachine transitions."""

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

    def test_valid_transitions(self) -> None:
        """Test all valid state transitions."""
        valid_pairs = [
            (Activity.Status.PENDING, Activity.Status.DISPATCHED),
            (Activity.Status.DISPATCHED, Activity.Status.RUNNING),
            (Activity.Status.RUNNING, Activity.Status.SUCCESS),
            (Activity.Status.RUNNING, Activity.Status.FAILED),
            (Activity.Status.RUNNING, Activity.Status.RETRY),
            (Activity.Status.RETRY, Activity.Status.PENDING),
            (Activity.Status.FAILED, Activity.Status.PENDING),
        ]

        for current, new in valid_pairs:
            self.assertTrue(
                ActivityStateMachine.can_transition(current, new),
                f"Expected {current} -> {new} to be valid",
            )

    def test_invalid_transitions(self) -> None:
        """Test invalid state transitions are rejected."""
        invalid_pairs = [
            (Activity.Status.PENDING, Activity.Status.RUNNING),
            (Activity.Status.PENDING, Activity.Status.SUCCESS),
            (Activity.Status.PENDING, Activity.Status.FAILED),
            (Activity.Status.SUCCESS, Activity.Status.PENDING),
            (Activity.Status.SUCCESS, Activity.Status.RUNNING),
            (Activity.Status.DISPATCHED, Activity.Status.PENDING),
            (Activity.Status.CREATED, Activity.Status.RUNNING),
        ]

        for current, new in invalid_pairs:
            self.assertFalse(
                ActivityStateMachine.can_transition(current, new),
                f"Expected {current} -> {new} to be invalid",
            )

    def test_transition_method_valid(self) -> None:
        """Test transition method succeeds for valid transition."""
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.PENDING,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        result = ActivityStateMachine.transition(
            activity, Activity.Status.DISPATCHED
        )

        self.assertEqual(result.status, Activity.Status.DISPATCHED)

    def test_transition_method_invalid_raises(self) -> None:
        """Test transition method raises for invalid transition."""
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.PENDING,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        with self.assertRaises(InvalidTransitionError):
            ActivityStateMachine.transition(activity, Activity.Status.RUNNING)

    def test_string_status_transitions(self) -> None:
        """Test state machine handles string status values."""
        self.assertTrue(
            ActivityStateMachine.can_transition("pending", "dispatched")
        )
        self.assertFalse(
            ActivityStateMachine.can_transition("pending", "running")
        )


class TestClaimActivity(TestCase):
    """Test claim_activity atomic claim."""

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

    def test_claim_sets_execution_id(self) -> None:
        """Test claim_activity sets execution_id."""
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.PENDING,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        claimed, exec_id = claim_activity(activity.id)

        self.assertIsNotNone(claimed)
        self.assertIsNotNone(exec_id)
        self.assertEqual(claimed.status, Activity.Status.DISPATCHED)
        self.assertIsNotNone(claimed.execution_id)
        self.assertIsNotNone(claimed.execution_started_at)

    def test_claim_contention_returns_none(self) -> None:
        """Test claim_activity returns None when already claimed."""
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.DISPATCHED,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        claimed, exec_id = claim_activity(activity.id)

        self.assertIsNone(claimed)
        self.assertIsNone(exec_id)

    def test_claim_already_success(self) -> None:
        """Test claim fails for SUCCESS status."""
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.SUCCESS,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        claimed, exec_id = claim_activity(activity.id)

        self.assertIsNone(claimed)
        self.assertIsNone(exec_id)


class TestValidateExecution(TestCase):
    """Test validate_execution execution_id validation."""

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

    def test_validate_valid_execution_id(self) -> None:
        """Test validate_execution succeeds with valid execution_id."""
        exec_id = uuid.uuid4()
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.DISPATCHED,
            execution_id=exec_id,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        validated = validate_execution(activity.id, str(exec_id))

        self.assertEqual(validated.id, activity.id)

    def test_validate_stale_execution_id(self) -> None:
        """Test validate_execution raises on stale execution_id."""
        exec_id = uuid.uuid4()
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.DISPATCHED,
            execution_id=exec_id,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        with self.assertRaises(StaleExecutionError):
            validate_execution(activity.id, "wrong-id")

    def test_validate_none_execution_id(self) -> None:
        """Test validate_execution raises when execution_id is None."""
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

    def test_validate_terminal_status(self) -> None:
        """Test validate_execution rejects terminal statuses."""
        exec_id = uuid.uuid4()
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.SUCCESS,
            execution_id=exec_id,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        with self.assertRaises(InvalidTransitionError):
            validate_execution(activity.id, str(exec_id))

    def test_validate_not_found(self) -> None:
        """Test validate_execution raises for non-existent activity."""
        with self.assertRaises(StaleExecutionError):
            validate_execution(99999, "any-id")


class TestTransitionFunctions(TestCase):
    """Test individual transition functions."""

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

    def test_transition_to_running(self) -> None:
        """Test transition_to_running from DISPATCHED."""
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.DISPATCHED,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        result = transition_to_running(activity)

        self.assertEqual(result.status, Activity.Status.RUNNING)

    def test_transition_to_success(self) -> None:
        """Test transition_to_success sets completion time."""
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.RUNNING,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        result = transition_to_success(activity)

        self.assertEqual(result.status, Activity.Status.SUCCESS)
        self.assertIsNotNone(result.execution_completed_at)

    def test_transition_to_failed(self) -> None:
        """Test transition_to_failed sets error and completion time."""
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.RUNNING,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        result = transition_to_failed(activity, "Test error message")

        self.assertEqual(result.status, Activity.Status.FAILED)
        self.assertEqual(result.last_error, "Test error message")
        self.assertIsNotNone(result.execution_completed_at)

    def test_transition_to_retry_increments_count(self) -> None:
        """Test transition_to_retry increments retry_count."""
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
        result = transition_to_retry(activity, next_due)

        self.assertEqual(result.status, Activity.Status.RETRY)
        self.assertEqual(result.retry_count, 1)

    def test_transition_to_retry_max_retries_exceeded(self) -> None:
        """Test transition_to_retry fails when max retries exceeded."""
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.RUNNING,
            retry_count=3,
            max_retries=3,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        next_due = timezone.now() + timedelta(hours=1)
        result = transition_to_retry(activity, next_due)

        self.assertEqual(result.status, Activity.Status.FAILED)
        self.assertIn("Max retries", result.last_error)


class TestRecoverStaleActivity(TestCase):
    """Test recover_stale_activity recovery logic."""

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

    def test_recover_stale_from_running(self) -> None:
        """Test recovery from RUNNING status."""
        exec_id = uuid.uuid4()
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.RUNNING,
            execution_id=exec_id,
            retry_count=0,
            max_retries=3,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        result = recover_stale_activity(activity)

        self.assertEqual(result.status, Activity.Status.RETRY)
        self.assertEqual(result.retry_count, 1)
        self.assertIsNone(result.execution_id)

    def test_recover_stale_from_dispatched(self) -> None:
        """Test recovery from DISPATCHED status."""
        exec_id = uuid.uuid4()
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.DISPATCHED,
            execution_id=exec_id,
            retry_count=0,
            max_retries=3,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        result = recover_stale_activity(activity)

        self.assertEqual(result.status, Activity.Status.RETRY)
        self.assertIsNone(result.execution_id)

    def test_recover_skips_pending(self) -> None:
        """Test recovery skips PENDING activities."""
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.PENDING,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        result = recover_stale_activity(activity)

        self.assertEqual(result.status, Activity.Status.PENDING)

    def test_recover_skips_success(self) -> None:
        """Test recovery skips SUCCESS activities."""
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.SUCCESS,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        result = recover_stale_activity(activity)

        self.assertEqual(result.status, Activity.Status.SUCCESS)

    def test_recover_clears_execution_id(self) -> None:
        """Test recovery clears execution_id to prevent reuse."""
        exec_id = uuid.uuid4()
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.RUNNING,
            execution_id=exec_id,
            retry_count=0,
            max_retries=3,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        result = recover_stale_activity(activity)

        self.assertIsNone(result.execution_id)
        self.assertIsNone(result.execution_started_at)


class TestIdempotencyHardening(TestCase):
    """Test idempotency guarantees."""

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

    def test_claim_then_reclaim_after_recovery(self) -> None:
        """Test activity can be claimed again after recovery."""
        exec_id = uuid.uuid4()
        activity = Activity.objects.create(
            owner=self.user,
            farm=self.farm,
            type=Activity.Type.VACCINATION,
            status=Activity.Status.DISPATCHED,
            execution_id=exec_id,
            retry_count=0,
            max_retries=3,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        recovered = recover_stale_activity(activity)
        self.assertEqual(recovered.status, Activity.Status.RETRY)

        recovered.status = Activity.Status.PENDING
        recovered.save()

        claimed, new_exec_id = claim_activity(activity.id)

        self.assertIsNotNone(claimed)
        self.assertIsNotNone(new_exec_id)
        self.assertNotEqual(str(exec_id), new_exec_id)
