from __future__ import annotations

import secrets
from datetime import date
from uuid import UUID, uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import caches
from django.core.files.base import ContentFile
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from farms.models import Farm, FarmIntegrationAccess
from integrations.tokens import mint_integration_access_token
from ndvi.models import NdviRasterArtifact

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc``\x00\x00\x00\x02\x00\x01"
    b"\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
)

User = get_user_model()


class FarmSyncApiTests(APITestCase):
    def setUp(self) -> None:
        caches["default"].clear()
        password = secrets.token_urlsafe(12)
        self.service_user = User.objects.create_user(
            username="nextcloud-integration",
            password=password,
        )

    def _auth_client(self, *, client_id: str, scope: str) -> APIClient:
        access, _ = mint_integration_access_token(
            user_id=client_id, scope=scope
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return client

    def _payload(self, *, external_farm_id: UUID) -> dict[str, object]:
        return {
            "external_farm_id": str(external_farm_id),
            "external_user_id": "nextcloud-user",
            "name": "north-field",
            "bbox": {
                "south": -1.234,
                "west": 36.812,
                "north": -1.22,
                "east": 36.83,
            },
            "centroid": {"lat": -1.227, "lon": 36.82},
        }

    def test_sync_creates_farm(self) -> None:
        external_farm_id = uuid4()
        client = self._auth_client(client_id="client-1", scope="write")
        resp = client.post(
            "/api/v1/farms/sync",
            self._payload(external_farm_id=external_farm_id),
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(body["status"], 0)
        self.assertEqual(
            body["data"]["external_farm_id"], str(external_farm_id)
        )

        farm = Farm.objects.get(external_farm_id=external_farm_id)
        self.assertEqual(farm.owner_id, self.service_user.id)
        self.assertEqual(farm.external_user_id, "nextcloud-user")
        self.assertEqual(farm.name, "north-field")
        self.assertIsNotNone(farm.slug)
        self.assertEqual(str(farm.bbox_south), body["data"]["bbox"]["south"])
        self.assertTrue(
            FarmIntegrationAccess.objects.filter(
                farm=farm, client_id="client-1", is_active=True
            ).exists()
        )

    def test_sync_updates_existing_farm(self) -> None:
        external_farm_id = uuid4()
        farm = Farm.objects.create(
            owner=self.service_user,
            external_farm_id=external_farm_id,
            external_user_id="nextcloud-user",
            name="Old Name",
            slug="old-name",
            bbox_south=-1.25,
            bbox_west=36.8,
            bbox_north=-1.21,
            bbox_east=36.85,
            centroid_lat=-1.23,
            centroid_lon=36.81,
        )
        client = self._auth_client(client_id="client-2", scope="write")
        payload = self._payload(external_farm_id=external_farm_id)
        payload["name"] = "Updated Name"
        resp = client.post("/api/v1/farms/sync", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        farm.refresh_from_db()
        self.assertEqual(farm.name, "Updated Name")
        self.assertEqual(farm.slug, "old-name")

    def test_sync_requires_token(self) -> None:
        resp = self.client.post(
            "/api/v1/farms/sync",
            self._payload(external_farm_id=uuid4()),
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_sync_preserves_bbox_validation(self) -> None:
        external_farm_id = uuid4()
        client = self._auth_client(client_id="client-3", scope="write")
        payload = self._payload(external_farm_id=external_farm_id)
        payload["bbox"] = {
            "south": 1.0,
            "west": 36.8,
            "north": 0.5,
            "east": 36.85,
        }
        resp = client.post("/api/v1/farms/sync", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.json()["status"], 1)

    def test_raster_resolves_external_farm_id_after_sync(self) -> None:
        external_farm_id = uuid4()
        client = self._auth_client(client_id="client-4", scope="write")
        sync_resp = client.post(
            "/api/v1/farms/sync",
            self._payload(external_farm_id=external_farm_id),
            format="json",
        )
        self.assertEqual(sync_resp.status_code, status.HTTP_200_OK)
        farm = Farm.objects.get(external_farm_id=external_farm_id)

        artifact = NdviRasterArtifact.objects.create(
            farm=farm,
            owner_id=farm.owner_id,
            engine=getattr(settings, "NDVI_RASTER_ENGINE_NAME", "sentinelhub"),
            date=date(2024, 2, 1),
            size=512,
            max_cloud=30,
            content_hash="hash",
        )
        artifact.image.save("raster.png", ContentFile(PNG_BYTES), save=True)

        resp = client.get(
            "/api/v1/farms/99999/ndvi/raster.png",
            {
                "date": "2024-02-01",
                "external_farm_id": str(external_farm_id),
            },
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp["Content-Type"], "image/png")
