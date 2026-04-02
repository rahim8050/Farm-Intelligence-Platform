from __future__ import annotations

import json
import secrets
from datetime import date
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import caches
from django.core.files.base import ContentFile
from django.test import override_settings
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from farms.models import Farm
from ndvi.models import NdviJob, NdviRasterArtifact
from ndvi.services import hash_request
from ndvi.tasks import run_ndvi_job
from ndvi.views import _extract_raster_not_found_reason

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc``\x00\x00\x00\x02\x00\x01"
    b"\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
)


@override_settings(
    NDVI_RASTER_ENGINE_PATH="ndvi.tests.fakes.FakeRasterEngine",
    NDVI_RASTER_DEFAULT_SIZE=512,
    NDVI_RASTER_MAX_SIZE=1024,
)
class NdviRasterApiTests(APITestCase):
    def setUp(self) -> None:
        caches["default"].clear()
        password = secrets.token_urlsafe(12)
        self.user = get_user_model().objects.create_user(
            username="owner",
            password=password,
            email="owner@example.com",
        )
        self.other = get_user_model().objects.create_user(
            username="other",
            password=password,
            email="other@example.com",
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
        self.queue_url = f"/api/v1/farms/{self.farm.id}/ndvi/raster/queue"
        self.raster_url = f"/api/v1/farms/{self.farm.id}/ndvi/raster.png"

    def _create_artifact(
        self,
        *,
        day: date,
        content: bytes = PNG_BYTES,
        content_hash: str = "hash",
    ) -> NdviRasterArtifact:
        artifact = NdviRasterArtifact.objects.create(
            farm=self.farm,
            owner_id=self.user.id,
            engine=getattr(settings, "NDVI_RASTER_ENGINE_NAME", "sentinelhub"),
            date=day,
            size=512,
            max_cloud=30,
            content_hash=content_hash,
        )
        artifact.image.save("raster.png", ContentFile(content), save=True)
        return artifact

    def _create_failed_raster_job(
        self,
        *,
        day: date,
        size: int,
        max_cloud: int,
        reason: str,
        engine: str = "stac",
    ) -> None:
        request_hash = hash_request(
            engine=engine,
            owner_id=self.user.id,
            farm_id=self.farm.id,
            params={
                "start": day,
                "end": day,
                "step_days": size,
                "max_cloud": max_cloud,
            },
        )
        error = ValidationError(
            {
                "detail": "Raster not found",
                "code": "raster_not_found",
                "reason": reason,
            }
        )
        NdviJob.objects.create(
            owner=self.user,
            farm=self.farm,
            engine=engine,
            job_type=NdviJob.JobType.RASTER_PNG,
            status=NdviJob.JobStatus.FAILED,
            start=day,
            end=day,
            step_days=size,
            max_cloud=max_cloud,
            request_hash=request_hash,
            last_error=str(error),
        )

    def test_queue_success_and_cooldown(self) -> None:
        self.client.force_authenticate(user=self.user)
        payload = {"date": "2024-02-01", "size": 256, "max_cloud": 20}
        with patch("ndvi.views.dispatch_ndvi_job") as mock_delay:
            resp = self.client.post(self.queue_url, payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(
            NdviJob.objects.filter(
                job_type=NdviJob.JobType.RASTER_PNG
            ).count(),
            1,
        )
        mock_delay.assert_called_once()

        # Cooldown blocks a second request
        second = self.client.post(self.queue_url, payload, format="json")
        self.assertEqual(second.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_raster_get_with_etag_and_owner_isolation(self) -> None:
        self._create_artifact(
            day=date(2024, 2, 2),
            content_hash="hash123",
        )

        self.client.force_authenticate(user=self.user)
        resp = self.client.get(
            self.raster_url,
            {"date": "2024-02-02", "size": "512", "max_cloud": "30"},
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp["Content-Type"], "image/png")
        self.assertIn("ETag", resp)
        etag = resp["ETag"]

        not_modified = self.client.get(
            self.raster_url,
            {"date": "2024-02-02", "size": "512", "max_cloud": "30"},
            HTTP_IF_NONE_MATCH=etag,
        )
        self.assertEqual(
            not_modified.status_code, status.HTTP_304_NOT_MODIFIED
        )

        self.client.force_authenticate(user=self.other)
        forbidden = self.client.get(
            self.raster_url,
            {"date": "2024-02-02", "size": "512", "max_cloud": "30"},
        )
        self.assertEqual(forbidden.status_code, status.HTTP_404_NOT_FOUND)

    def test_raster_get_accepts_mmddyyyy(self) -> None:
        self._create_artifact(
            day=date(2024, 2, 2),
            content_hash="hash456",
        )

        self.client.force_authenticate(user=self.user)
        resp = self.client.get(
            self.raster_url,
            {"date": "02/02/2024", "size": "512", "max_cloud": "30"},
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp["Content-Type"], "image/png")

    def test_raster_get_missing_returns_404(self) -> None:
        self._create_failed_raster_job(
            day=date(2024, 2, 10),
            size=512,
            max_cloud=30,
            reason="no_items",
        )
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(
            self.raster_url,
            {
                "date": "2024-02-10",
                "size": "512",
                "max_cloud": "30",
                "engine": "stac",
            },
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        body = resp.json()
        self.assertEqual(body["status"], 1)
        self.assertEqual(body["message"], "Raster not found")
        self.assertEqual(body["errors"]["detail"], "Raster not found")
        self.assertEqual(body["errors"]["code"], "raster_not_found")
        self.assertEqual(body["errors"]["reason"], "no_items")

    def test_extract_raster_not_found_reason_json(self) -> None:
        payload = json.dumps(
            {
                "detail": "Raster not found",
                "code": "raster_not_found",
                "reason": "no_items",
            }
        )
        reason = _extract_raster_not_found_reason(payload)
        self.assertEqual(reason, "no_items")

    def test_raster_get_empty_content_returns_404(self) -> None:
        self._create_artifact(
            day=date(2024, 2, 3),
            content=b"",
            content_hash="hash-empty",
        )
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(
            self.raster_url,
            {"date": "2024-02-03", "size": "512", "max_cloud": "30"},
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_raster_accept_header_png_returns_png(self) -> None:
        self._create_artifact(day=date(2024, 2, 5), content_hash="hash-png")
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(
            self.raster_url,
            {"date": "2024-02-05", "size": "512", "max_cloud": "30"},
            HTTP_ACCEPT="image/png",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp["Content-Type"], "image/png")
        self.assertTrue(resp.content.startswith(b"\x89PNG"))

    def test_raster_accept_header_json_returns_png(self) -> None:
        self._create_artifact(day=date(2024, 2, 6), content_hash="hash-json")
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(
            self.raster_url,
            {"date": "2024-02-06", "size": "512", "max_cloud": "30"},
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp["Content-Type"], "image/png")
        self.assertTrue(resp.content.startswith(b"\x89PNG"))

    def test_raster_error_returns_json_even_with_png_accept(self) -> None:
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(
            self.raster_url,
            {"date": "2024-02-11", "size": "512", "max_cloud": "30"},
            HTTP_ACCEPT="image/png",
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(resp["Content-Type"], "application/json")
        self.assertEqual(resp.json()["status"], 1)

    def test_raster_lookup_caches_artifact_id(self) -> None:
        caches["default"].clear()
        artifact = self._create_artifact(
            day=date(2024, 2, 4),
            content_hash="hash-cache",
        )
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(
            self.raster_url,
            {"date": "2024-02-04", "size": "512", "max_cloud": "30"},
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        engine_name = getattr(
            settings, "NDVI_RASTER_ENGINE_NAME", "sentinelhub"
        )
        cache_key = (
            f"ndvi:raster:ptr:{self.farm.id}:{engine_name}:2024-02-04:512:30"
        )
        cached = caches["default"].get(cache_key)
        self.assertEqual(cached, artifact.id)

    def test_raster_job_execution_saves_artifact(self) -> None:
        self.client.force_authenticate(user=self.user)
        params = {"start": date(2024, 3, 3), "end": date(2024, 3, 3)}
        job = NdviJob.objects.create(
            owner=self.user,
            farm=self.farm,
            engine=getattr(settings, "NDVI_RASTER_ENGINE_NAME", "sentinelhub"),
            job_type=NdviJob.JobType.RASTER_PNG,
            start=params["start"],
            end=params["end"],
            step_days=256,
            max_cloud=25,
            request_hash="hash-raster",
        )
        with patch("ndvi.tasks.acquire_lock", return_value=True):
            result = run_ndvi_job.apply(args=[job.id]).get()

        self.assertEqual(result, "ok")
        artifacts = NdviRasterArtifact.objects.filter(farm=self.farm)
        self.assertEqual(artifacts.count(), 1)
        artifact = artifacts.first()
        self.assertIsNotNone(artifact)
        if artifact:
            self.assertEqual(artifact.size, 256)
            artifact.image.open("rb")
            content = artifact.image.read()
            artifact.image.close()
            self.assertTrue(content.startswith(b"\x89PNG"))
