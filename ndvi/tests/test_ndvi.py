from __future__ import annotations

import secrets
from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.cache import caches
from rest_framework import status
from rest_framework.test import APITestCase

from farms.models import Farm
from ndvi.engines.base import NdviPoint
from ndvi.engines.sentinelhub import SentinelHubEngine
from ndvi.models import NdviJob, NdviObservation
from ndvi.services import (
    TimeseriesParams,
    get_default_ndvi_engine_name,
    hash_request,
)
from ndvi.tasks import run_ndvi_job


class NdviApiTests(APITestCase):
    def setUp(self) -> None:
        caches["default"].clear()
        password = secrets.token_urlsafe(16)
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
        self.timeseries_url = f"/api/v1/farms/{self.farm.id}/ndvi/timeseries/"
        self.latest_url = f"/api/v1/farms/{self.farm.id}/ndvi/latest/"
        self.refresh_url = f"/api/v1/farms/{self.farm.id}/ndvi/refresh/"
        self.job_status_base = "/api/v1/ndvi/jobs/"
        self.default_engine = get_default_ndvi_engine_name()

    def test_owner_isolation(self) -> None:
        """Users cannot read NDVI for farms they do not own."""

        self.client.force_authenticate(user=self.other)
        resp = self.client.get(
            self.timeseries_url,
            {"start": "2024-01-01", "end": "2024-01-10"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_bbox_required(self) -> None:
        """Missing bounding box returns 400."""

        farm = Farm.objects.create(
            owner=self.user,
            name="No bbox",
            slug="nobbox",
            is_active=True,
        )
        self.client.force_authenticate(user=self.user)
        url = f"/api/v1/farms/{farm.id}/ndvi/timeseries/"
        resp = self.client.get(
            url, {"start": "2024-01-01", "end": "2024-01-02"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("ndvi.views.dispatch_ndvi_job")
    def test_timeseries_accepts_mmddyyyy(self, mock_delay: MagicMock) -> None:
        """MM/DD/YYYY dates are normalized and accepted."""

        self.client.force_authenticate(user=self.user)
        resp = self.client.get(
            self.timeseries_url,
            {"start": "01/02/2024", "end": "01/10/2024"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        data = body.get("data", {})
        self.assertEqual(data.get("start"), "2024-01-02")
        self.assertEqual(data.get("end"), "2024-01-10")
        mock_delay.assert_called_once()

    @patch("ndvi.views.dispatch_ndvi_job")
    def test_timeseries_engine_override(self, mock_delay: MagicMock) -> None:
        """Query param engine overrides the default engine."""

        self.client.force_authenticate(user=self.user)
        resp = self.client.get(
            self.timeseries_url,
            {"start": "2024-01-01", "end": "2024-01-10", "engine": "stac"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json().get("data", {})
        self.assertEqual(data.get("engine"), "stac")
        job = NdviJob.objects.filter(job_type=NdviJob.JobType.GAP_FILL).first()
        self.assertIsNotNone(job)
        if job:
            self.assertEqual(job.engine, "stac")
        mock_delay.assert_called_once()

    @patch("ndvi.views.dispatch_ndvi_job")
    def test_latest_response_contract_shape(
        self, mock_delay: MagicMock
    ) -> None:
        """Latest endpoint returns the standard response envelope."""

        self.client.force_authenticate(user=self.user)
        resp = self.client.get(self.latest_url, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertIn("status", body)
        self.assertIn("message", body)
        self.assertIn("data", body)
        self.assertIn("errors", body)
        mock_delay.assert_called_once()

    @patch("ndvi.views.dispatch_ndvi_job")
    def test_gap_detection_enqueues_job(self, mock_delay: MagicMock) -> None:
        """Gap detection schedules a gap-fill job without blocking."""

        NdviObservation.objects.create(
            farm=self.farm,
            engine=self.default_engine,
            bucket_date=date(2024, 1, 1),
            mean=0.1,
        )
        self.client.force_authenticate(user=self.user)
        payload = {
            "start": "2024-01-01",
            "end": "2024-01-15",
            "step_days": "7",
        }
        resp = self.client.get(self.timeseries_url, payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        data = body.get("data", {})
        self.assertTrue(data.get("is_partial"))
        self.assertEqual(data.get("missing_buckets_count"), 2)
        self.assertEqual(
            NdviJob.objects.filter(job_type=NdviJob.JobType.GAP_FILL).count(),
            1,
        )
        mock_delay.assert_called_once()

    @patch("ndvi.views.dispatch_ndvi_job")
    def test_idempotent_job_creation(self, mock_delay: MagicMock) -> None:
        """Same params create a single queued job."""

        self.client.force_authenticate(user=self.user)
        payload = {
            "start": "2024-02-01",
            "end": "2024-02-15",
            "step_days": "7",
        }
        first = self.client.get(self.timeseries_url, payload, format="json")
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        second = self.client.get(self.timeseries_url, payload, format="json")
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(
            NdviJob.objects.filter(job_type=NdviJob.JobType.GAP_FILL).count(),
            1,
        )
        mock_delay.assert_called_once()

    def test_lock_prevents_duplicate_upstream_calls(self) -> None:
        """Distributed lock ensures engine invoked once."""

        params = TimeseriesParams(
            start=date(2024, 1, 1),
            end=date(2024, 1, 7),
            step_days=7,
            max_cloud=30,
        )
        request_hash = hash_request(
            engine=self.default_engine,
            owner_id=self.user.id,
            farm_id=self.farm.id,
            params={
                "start": params.start,
                "end": params.end,
                "step_days": params.step_days,
                "max_cloud": params.max_cloud,
            },
        )
        job = NdviJob.objects.create(
            owner=self.user,
            farm=self.farm,
            engine=self.default_engine,
            job_type=NdviJob.JobType.GAP_FILL,
            start=params.start,
            end=params.end,
            step_days=params.step_days,
            max_cloud=params.max_cloud,
            request_hash=request_hash,
        )

        class DummyEngine:
            def __init__(self) -> None:
                self.calls = 0

            def get_timeseries(self, **_: Any) -> list[NdviPoint]:
                self.calls += 1
                return [NdviPoint(date=date(2024, 1, 1), mean=0.2)]

            def get_latest(
                self, **_: Any
            ) -> NdviPoint | None:  # pragma: no cover - not used
                return None

        dummy = DummyEngine()
        with patch("ndvi.tasks.get_engine", return_value=dummy):
            caches["default"].clear()
            result1 = run_ndvi_job.apply(args=[job.id]).get()
            result2 = run_ndvi_job.apply(args=[job.id]).get()

        self.assertEqual(dummy.calls, 1)
        self.assertEqual(result1, "ok")
        self.assertEqual(result2, "locked")

    def test_token_caching_reuses_oauth_response(self) -> None:
        """OAuth token is cached and reused."""

        caches["default"].clear()
        engine = SentinelHubEngine(
            client_id="cid", client_secret=secrets.token_urlsafe(8)
        )

        call_count = 0

        class FakeResponse:
            def json(self) -> dict[str, object]:
                return {"access_token": "token-123", "expires_in": 3600}

            def raise_for_status(self) -> None:
                return None

        def fake_request(*_: Any, **__: Any) -> FakeResponse:
            nonlocal call_count
            call_count += 1
            return FakeResponse()

        with patch.object(
            engine, "_request_with_retry", side_effect=fake_request
        ):
            token1 = engine._get_access_token()
            token2 = engine._get_access_token()

        self.assertEqual(token1, "token-123")
        self.assertEqual(token1, token2)
        self.assertEqual(call_count, 1)

    @patch("ndvi.views.enqueue_job")
    def test_cached_response_skips_enqueue(
        self, mock_enqueue: MagicMock
    ) -> None:
        """Cached API response is returned without scheduling."""

        self.client.force_authenticate(user=self.user)
        payload = {
            "start": "2024-03-01",
            "end": "2024-03-03",
            "step_days": "1",
        }

        with patch("ndvi.views.dispatch_ndvi_job"):
            first = self.client.get(
                self.timeseries_url, payload, format="json"
            )
            self.assertEqual(first.status_code, status.HTTP_200_OK)

        mock_enqueue.reset_mock()
        second = self.client.get(self.timeseries_url, payload, format="json")
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        mock_enqueue.assert_not_called()

    @patch("ndvi.views.dispatch_ndvi_job")
    def test_timeseries_complete_does_not_enqueue(
        self, mock_delay: MagicMock
    ) -> None:
        self.client.force_authenticate(user=self.user)
        NdviObservation.objects.create(
            farm=self.farm,
            engine=self.default_engine,
            bucket_date=date(2024, 1, 1),
            mean=0.1,
        )
        NdviObservation.objects.create(
            farm=self.farm,
            engine=self.default_engine,
            bucket_date=date(2024, 1, 8),
            mean=0.2,
        )
        NdviObservation.objects.create(
            farm=self.farm,
            engine=self.default_engine,
            bucket_date=date(2024, 1, 15),
            mean=0.3,
        )
        payload = {
            "start": "2024-01-01",
            "end": "2024-01-15",
            "step_days": "7",
        }
        resp = self.client.get(self.timeseries_url, payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()["data"]
        self.assertFalse(data["is_partial"])
        self.assertEqual(data["missing_buckets_count"], 0)
        mock_delay.assert_not_called()

    @patch("ndvi.views.dispatch_ndvi_job")
    def test_latest_view_stale_enqueues_refresh(
        self, mock_delay: MagicMock
    ) -> None:
        NdviObservation.objects.create(
            farm=self.farm,
            engine=self.default_engine,
            bucket_date=date(2020, 1, 1),
            mean=0.1,
        )
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(self.latest_url, {"lookback_days": "7"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()["data"]
        self.assertTrue(data["stale"])
        self.assertEqual(
            NdviJob.objects.filter(
                job_type=NdviJob.JobType.REFRESH_LATEST
            ).count(),
            1,
        )
        mock_delay.assert_called_once()

    @patch("ndvi.views.enqueue_job")
    def test_latest_view_fresh_no_enqueue(
        self, mock_enqueue: MagicMock
    ) -> None:
        NdviObservation.objects.create(
            farm=self.farm,
            engine=self.default_engine,
            bucket_date=date.today(),
            mean=0.1,
        )
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(self.latest_url, {"lookback_days": "7"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()["data"]
        self.assertFalse(data["stale"])
        mock_enqueue.assert_not_called()

    @patch("ndvi.views.enqueue_job")
    def test_latest_view_cached_response(
        self, mock_enqueue: MagicMock
    ) -> None:
        self.client.force_authenticate(user=self.user)
        cached_payload = {
            "observation": None,
            "engine": self.default_engine,
            "lookback_days": 7,
            "max_cloud": 30,
            "stale": True,
        }
        caches["default"].set(
            f"ndvi:cache:latest:{self.user.id}:{self.farm.id}:{self.default_engine}:7:30",
            cached_payload,
        )
        resp = self.client.get(self.latest_url, {"lookback_days": "7"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()["data"], cached_payload)
        mock_enqueue.assert_not_called()

    @patch("ndvi.views.dispatch_ndvi_job")
    def test_refresh_view_throttle_and_success(
        self, mock_delay: MagicMock
    ) -> None:
        self.client.force_authenticate(user=self.user)
        first = self.client.post(self.refresh_url, format="json")
        self.assertEqual(first.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(first.json()["status"], 0)
        self.assertEqual(
            NdviJob.objects.filter(
                job_type=NdviJob.JobType.REFRESH_LATEST
            ).count(),
            1,
        )
        mock_delay.assert_called_once()

        second = self.client.post(self.refresh_url, format="json")
        self.assertEqual(second.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_job_status_view_returns_job(self) -> None:
        job = NdviJob.objects.create(
            owner=self.user,
            farm=self.farm,
            engine=self.default_engine,
            job_type=NdviJob.JobType.GAP_FILL,
            request_hash="status-hash",
        )
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(f"{self.job_status_base}{job.id}/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()["data"]["id"], job.id)
