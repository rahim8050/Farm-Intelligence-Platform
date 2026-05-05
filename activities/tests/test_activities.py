import secrets
from datetime import timedelta

import pytest
from django.test import TestCase
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
