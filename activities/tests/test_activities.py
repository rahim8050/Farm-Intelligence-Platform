import secrets
from datetime import timedelta

import pytest
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from activities.models import Activity

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
