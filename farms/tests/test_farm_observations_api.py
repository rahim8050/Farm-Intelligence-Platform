from __future__ import annotations

import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from farms.models import Farm, FarmIntegrationAccess, FarmObservation
from integrations.tokens import mint_integration_access_token

User = get_user_model()


class FarmObservationApiTests(APITestCase):
    def setUp(self) -> None:
        pw = secrets.token_urlsafe(12)
        self.user1 = User.objects.create_user(username="u1", password=pw)
        self.user2 = User.objects.create_user(username="u2", password=pw)
        self.farm = Farm.objects.create(owner=self.user1, name="Demo Farm")

    def test_owner_can_create_and_list_observations(self) -> None:
        self.client.force_authenticate(user=self.user1)
        payload = {
            "observed_at": timezone.now().isoformat(),
            "event_type": "irrigation",
            "note": "Checked drip line",
            "metadata": {"duration_min": 45},
        }
        created = self.client.post(
            f"/api/v1/farms/{self.farm.id}/observations/",
            payload,
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        body = created.json()
        self.assertEqual(body["status"], 0)
        self.assertEqual(body["data"]["event_type"], "irrigation")
        self.assertEqual(body["data"]["farm_id"], self.farm.id)

        listed = self.client.get(f"/api/v1/farms/{self.farm.id}/observations/")
        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        list_body = listed.json()
        self.assertEqual(list_body["status"], 0)
        self.assertEqual(len(list_body["data"]), 1)

    def test_list_filters_and_limit(self) -> None:
        self.client.force_authenticate(user=self.user1)
        now = timezone.now()
        FarmObservation.objects.create(
            farm=self.farm,
            observed_at=now,
            event_type="inspection",
            note="Field walk",
        )
        FarmObservation.objects.create(
            farm=self.farm,
            observed_at=now - timedelta(days=1),
            event_type="irrigation",
            note="Drip line check",
        )

        resp = self.client.get(
            f"/api/v1/farms/{self.farm.id}/observations/",
            {"event_type": "inspection", "limit": "1"},
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()["data"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["event_type"], "inspection")

    def test_other_user_cannot_access_observations(self) -> None:
        self.client.force_authenticate(user=self.user2)
        res = self.client.get(f"/api/v1/farms/{self.farm.id}/observations/")
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_integration_can_create_observation(self) -> None:
        access, _ = mint_integration_access_token(
            user_id="client-1", scope="write"
        )
        FarmIntegrationAccess.objects.create(
            farm=self.farm, client_id="client-1"
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        payload = {
            "observed_at": timezone.now().isoformat(),
            "event_type": "inspection",
            "note": "Pest scouting",
        }
        created = client.post(
            f"/api/v1/farms/{self.farm.id}/observations/",
            payload,
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        body = created.json()
        self.assertEqual(body["status"], 0)
        self.assertEqual(body["data"]["created_by_id"], None)
        self.assertEqual(body["data"]["created_by_client_id"], "client-1")

    def test_integration_requires_allowlist(self) -> None:
        access, _ = mint_integration_access_token(
            user_id="client-2", scope="write"
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        payload = {
            "observed_at": timezone.now().isoformat(),
            "event_type": "inspection",
        }
        created = client.post(
            f"/api/v1/farms/{self.farm.id}/observations/",
            payload,
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_404_NOT_FOUND)

    def test_integration_scope_blocks_write(self) -> None:
        access, _ = mint_integration_access_token(
            user_id="client-3", scope="read"
        )
        FarmIntegrationAccess.objects.create(
            farm=self.farm, client_id="client-3"
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        payload = {
            "observed_at": timezone.now().isoformat(),
            "event_type": "inspection",
        }
        created = client.post(
            f"/api/v1/farms/{self.farm.id}/observations/",
            payload,
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_403_FORBIDDEN)
