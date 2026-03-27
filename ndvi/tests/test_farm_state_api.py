from __future__ import annotations

import secrets
from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from api_keys.auth import generate_plaintext_key, hash_api_key
from api_keys.models import ApiKey, ApiKeyScope
from farms.models import Farm, FarmIntegrationAccess
from integrations.tokens import mint_integration_access_token
from ndvi.models import NdviObservation
from ndvi.services import get_default_ndvi_engine_name


@pytest.fixture(autouse=True)
def disable_coverage_enqueue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ndvi.farm_state._enqueue_coverage_compute",
        lambda **kwargs: None,
    )


class FarmStateApiTests(APITestCase):
    def setUp(self) -> None:
        password = secrets.token_urlsafe(12)
        self.user = get_user_model().objects.create_user(
            username="owner",
            email="owner@example.com",
            password=password,
        )
        self.other = get_user_model().objects.create_user(
            username="other",
            email="other@example.com",
            password=password,
        )
        self.farm = Farm.objects.create(
            owner=self.user,
            name="Farm A",
            slug="farm-a",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        self.url = f"/api/v1/farm-state/{self.farm.id}/"
        self.engine = get_default_ndvi_engine_name()

    def _add_observation(
        self,
        *,
        farm: Farm,
        days_ago: int,
        mean: float,
        max_value: float | None = None,
    ) -> None:
        NdviObservation.objects.create(
            farm=farm,
            engine=self.engine,
            bucket_date=date.today() - timedelta(days=days_ago),
            mean=mean,
            max=max_value,
        )

    def test_farm_state_requires_auth(self) -> None:
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_farm_state_returns_404_for_missing_farm(self) -> None:
        self.client.force_authenticate(user=self.user)
        resp = self.client.get("/api/v1/farm-state/999999/")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_farm_state_owner_only(self) -> None:
        self.client.force_authenticate(user=self.other)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_farm_state_success_response(self) -> None:
        self._add_observation(
            farm=self.farm,
            days_ago=10,
            mean=0.3,
            max_value=0.35,
        )
        self._add_observation(
            farm=self.farm,
            days_ago=2,
            mean=0.32,
            max_value=0.36,
        )
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        data = body.get("data", {})
        self.assertEqual(data.get("farm_id"), self.farm.id)
        self.assertIn("mean_ndvi", data)
        self.assertIn("max_ndvi", data)
        self.assertIn("coverage_pct", data)
        self.assertIn("trend", data)
        self.assertIn("state", data)
        self.assertIn("interpretation", data)
        self.assertIn("action", data)

    def test_farm_state_accepts_api_key(self) -> None:
        self._add_observation(
            farm=self.farm,
            days_ago=5,
            mean=0.3,
            max_value=0.33,
        )
        plaintext = generate_plaintext_key()
        ApiKey.objects.create(
            user=self.user,
            name="Farm State Key",
            key_hash=hash_api_key(plaintext),
            prefix=plaintext[:12],
            last4=plaintext[-4:],
            scope=ApiKeyScope.READ,
        )
        client = APIClient()
        client.credentials(HTTP_X_API_KEY=plaintext)
        resp = client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()["data"]["farm_id"], self.farm.id)

    def test_farm_state_allows_integration_read(self) -> None:
        self._add_observation(
            farm=self.farm,
            days_ago=5,
            mean=0.3,
            max_value=0.33,
        )
        access, _ = mint_integration_access_token(
            user_id="client-1", scope="read"
        )
        FarmIntegrationAccess.objects.create(
            farm=self.farm, client_id="client-1"
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        resp = client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()["data"]["farm_id"], self.farm.id)

    def test_farm_state_classification_edges(self) -> None:
        cases: list[tuple[str, list[tuple[int, float, float]]]] = [
            ("establishment", [(5, 0.2, 0.5)]),
            ("full_canopy", [(4, 0.45, 0.55)]),
            ("decline", [(12, 0.35, 0.36), (2, 0.3, 0.34)]),
            ("growth", [(12, 0.26, 0.3), (2, 0.32, 0.36)]),
        ]

        self.client.force_authenticate(user=self.user)
        for state, observations in cases:
            farm = Farm.objects.create(
                owner=self.user,
                name=f"Farm {state}",
                slug=f"farm-{state}",
                bbox_south=0.0,
                bbox_west=0.0,
                bbox_north=0.2,
                bbox_east=0.2,
                is_active=True,
            )
            for days_ago, mean, max_value in observations:
                self._add_observation(
                    farm=farm,
                    days_ago=days_ago,
                    mean=mean,
                    max_value=max_value,
                )

            resp = self.client.get(f"/api/v1/farm-state/{farm.id}/")
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            data = resp.json().get("data", {})
            self.assertEqual(data.get("state"), state)
