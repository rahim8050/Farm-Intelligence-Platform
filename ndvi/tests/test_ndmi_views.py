"""Tests for NDMI view endpoints.

Covers:
- NdmiTimeseriesView
- NdmiLatestView
- NdmiRefreshView
- NdmiRasterPngView
- NdmiRasterQueueView
- NdmiFarmStateView
"""

from __future__ import annotations

import secrets
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.cache import caches
from rest_framework import status
from rest_framework.test import APITestCase

from farms.models import Farm
from ndvi.farm_state_ndmi import STATE_UNKNOWN, compute_ndmi_farm_state
from ndvi.models import NdviObservation
from ndvi.services import LatestParams, TimeseriesParams


class NdmiViewMixin:
    """Shared setup for NDMI view tests."""

    def setUp(self) -> None:
        caches["default"].clear()
        password = secrets.token_urlsafe(16)
        self.user = get_user_model().objects.create_user(
            username="ndmi_user",
            password=password,
        )
        self.other = get_user_model().objects.create_user(
            username="ndmi_other",
            password=password,
        )
        self.farm = Farm.objects.create(
            owner=self.user,
            name="NDMI Test Farm",
            slug="ndmi-test-farm",
            bbox_south=Decimal("0.0"),
            bbox_west=Decimal("0.0"),
            bbox_north=Decimal("0.2"),
            bbox_east=Decimal("0.2"),
            is_active=True,
        )
        self.client.force_authenticate(user=self.user)  # type: ignore[attr-defined]


class NdmiApiTests(NdmiViewMixin, APITestCase):
    """NDMI view endpoints integration tests."""

    def setUp(self) -> None:
        super().setUp()
        self.timeseries_url = f"/api/v1/farms/{self.farm.id}/ndmi/timeseries/"
        self.latest_url = f"/api/v1/farms/{self.farm.id}/ndmi/latest/"
        self.refresh_url = f"/api/v1/farms/{self.farm.id}/ndmi/refresh/"

    @patch("ndvi.views.enforce_quota")
    @patch("ndvi.views.dispatch_ndvi_job")
    def test_ndmi_timeseries_returns_200(
        self, mock_dispatch: MagicMock, mock_quota: MagicMock
    ) -> None:
        """GET /ndmi/timeseries/ returns envelope with data."""
        td = date.today()
        start = td - timedelta(days=10)
        end = td + timedelta(days=1)
        NdviObservation.objects.create(
            farm=self.farm,
            engine="stac",
            bucket_date=td,
            mean=0.4,
            index_type="NDMI",
            state=NdviObservation.ObservationState.FINAL,
        )
        response = self.client.get(
            self.timeseries_url,
            {
                "engine": "stac",
                "step_days": "1",
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data: dict[str, Any] = response.json()
        self.assertIn("data", data)

    @patch("ndvi.views.enforce_quota")
    @patch("ndvi.views.dispatch_ndvi_job")
    def test_ndmi_timeseries_returns_cached_response(
        self, mock_dispatch: MagicMock, mock_quota: MagicMock
    ) -> None:
        """GET /ndmi/timeseries/ returns envelope when cached."""
        td = date.today()
        start = td - timedelta(days=5)
        end = td
        from ndvi.services import cache_ndmi_timeseries_response

        params = TimeseriesParams(
            start=start, end=end, step_days=1, max_cloud=30
        )
        cache_ndmi_timeseries_response(
            owner_id=self.user.id,
            farm_id=self.farm.id,
            engine="stac",
            params=params,
            payload={"observations": []},
        )
        response = self.client.get(
            self.timeseries_url,
            {
                "engine": "stac",
                "step_days": "1",
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @patch("ndvi.views.enforce_quota")
    @patch("ndvi.views.dispatch_ndvi_job")
    def test_ndmi_timeseries_v2_cached(
        self, mock_dispatch: MagicMock, mock_quota: MagicMock
    ) -> None:
        """GET /ndmi/timeseries/ with representation=v2 returns envelope."""
        td = date.today()
        start = td - timedelta(days=3)
        end = td
        from ndvi.services import cache_ndmi_timeseries_response

        params = TimeseriesParams(
            start=start, end=end, step_days=1, max_cloud=30
        )
        cache_ndmi_timeseries_response(
            owner_id=self.user.id,
            farm_id=self.farm.id,
            engine="stac",
            params=params,
            payload={"observations": []},
        )
        response = self.client.get(
            self.timeseries_url,
            {
                "engine": "stac",
                "step_days": "1",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "representation": "v2",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("data", response.json())

    @patch("ndvi.views.enforce_quota")
    @patch("ndvi.views.dispatch_ndvi_job")
    def test_ndmi_timeseries_gap_fill(
        self, mock_dispatch: MagicMock, mock_quota: MagicMock
    ) -> None:
        """GET /ndmi/timeseries/ triggers gap fill for missing dates."""
        td = date.today()
        start = td - timedelta(days=10)
        end = td + timedelta(days=1)
        NdviObservation.objects.create(
            farm=self.farm,
            engine="stac",
            bucket_date=td,
            mean=0.4,
            index_type="NDMI",
            state=NdviObservation.ObservationState.FINAL,
        )
        response = self.client.get(
            self.timeseries_url,
            {
                "engine": "stac",
                "step_days": "1",
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_dispatch.assert_called()

    @patch("ndvi.views.enforce_quota")
    @patch("ndvi.views.dispatch_ndvi_job")
    def test_ndmi_latest_returns_200(
        self, mock_dispatch: MagicMock, mock_quota: MagicMock
    ) -> None:
        """GET /ndmi/latest/ returns envelope with data."""
        td = date.today()
        NdviObservation.objects.create(
            farm=self.farm,
            engine="stac",
            bucket_date=td,
            mean=0.4,
            index_type="NDMI",
            state=NdviObservation.ObservationState.FINAL,
        )
        response = self.client.get(
            self.latest_url,
            {"engine": "stac"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data: dict[str, Any] = response.json()
        self.assertIn("data", data)

    @patch("ndvi.views.enforce_quota")
    @patch("ndvi.views.dispatch_ndvi_job")
    def test_ndmi_latest_v2_cached(
        self, mock_dispatch: MagicMock, mock_quota: MagicMock
    ) -> None:
        """GET /ndmi/latest/ with representation=v2 returns envelope."""
        from ndvi.services import cache_ndmi_latest_response

        params = LatestParams(lookback_days=14, max_cloud=30)
        cache_ndmi_latest_response(
            owner_id=self.user.id,
            farm_id=self.farm.id,
            engine="stac",
            params=params,
            payload={"observation": None, "engine": "stac"},
        )
        response = self.client.get(
            self.latest_url,
            {"engine": "stac", "representation": "v2"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data: dict[str, Any] = response.json()
        self.assertIn("data", data)

    @patch("ndvi.views.enforce_quota")
    @patch("ndvi.views.dispatch_ndvi_job")
    def test_ndmi_refresh_returns_202(
        self, mock_dispatch: MagicMock, mock_quota: MagicMock
    ) -> None:
        """POST /ndmi/refresh/ returns 202 Accepted."""
        response = self.client.post(self.refresh_url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        data: dict[str, Any] = response.json()
        self.assertIn("data", data)

    @patch("ndvi.views.enforce_quota")
    @patch("ndvi.views.dispatch_ndvi_job")
    def test_ndmi_refresh_returns_429_on_throttle(
        self, mock_dispatch: MagicMock, mock_quota: MagicMock
    ) -> None:
        """POST /ndmi/refresh/ returns 429 when throttled."""
        self.client.post(self.refresh_url)
        response = self.client.post(self.refresh_url)
        self.assertEqual(
            response.status_code, status.HTTP_429_TOO_MANY_REQUESTS
        )

    def test_ndmi_owner_isolation(self) -> None:
        """Other users cannot read NDMI data for a farm they don't own."""
        self.client.force_authenticate(user=self.other)
        td = date.today()
        response = self.client.get(
            self.timeseries_url,
            {
                "engine": "stac",
                "step_days": "1",
                "start": (td - timedelta(days=5)).isoformat(),
                "end": td.isoformat(),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_ndmi_timeseries_bad_engine_returns_400(self) -> None:
        """Unknown engine name returns 400 validation error."""
        td = date.today()
        start = td - timedelta(days=5)
        end = td
        response = self.client.get(
            self.timeseries_url,
            {
                "engine": "nonexistent_engine",
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class NdmiRasterQueueTests(NdmiViewMixin, APITestCase):
    """NDMI raster queue endpoint tests."""

    def setUp(self) -> None:
        super().setUp()
        self.queue_url = f"/api/v1/farms/{self.farm.id}/ndmi/raster/queue"

    @patch("ndvi.views.enforce_quota")
    @patch("ndvi.views.dispatch_ndvi_job")
    def test_ndmi_raster_queue_returns_202(
        self, mock_dispatch: MagicMock, mock_quota: MagicMock
    ) -> None:
        td = date.today().isoformat()
        response = self.client.post(
            self.queue_url,
            {"engine": "stac", "date": td},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        data: dict[str, Any] = response.json()
        self.assertIn("data", data)

    @patch("ndvi.views.enforce_quota")
    @patch("ndvi.views.dispatch_ndvi_job")
    def test_ndmi_raster_queue_returns_429_on_throttle(
        self, mock_dispatch: MagicMock, mock_quota: MagicMock
    ) -> None:
        td = date.today().isoformat()
        self.client.post(
            self.queue_url,
            {"engine": "stac", "date": td},
            format="json",
        )
        response = self.client.post(
            self.queue_url,
            {"engine": "stac", "date": td},
            format="json",
        )
        self.assertEqual(
            response.status_code, status.HTTP_429_TOO_MANY_REQUESTS
        )


class NdmiRasterPngTests(NdmiViewMixin, APITestCase):
    """NDMI raster PNG endpoint tests."""

    def setUp(self) -> None:
        super().setUp()
        self.png_url = f"/api/v1/farms/{self.farm.id}/ndmi/raster.png"

    @patch("ndvi.views.enforce_quota")
    @patch("ndvi.views.dispatch_ndvi_job")
    def test_ndmi_raster_png_404_when_no_artifact(
        self, mock_dispatch: MagicMock, mock_quota: MagicMock
    ) -> None:
        """Raster PNG returns 404 when no cached artifact exists."""
        response = self.client.get(
            self.png_url,
            {
                "engine": "stac",
                "date": date.today().isoformat(),
                "size": "256",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class NdmiUrlResolutionTests(NdmiViewMixin, APITestCase):
    """NDMI URL patterns resolve correctly."""

    def test_ndmi_timeseries_url_resolves(self) -> None:
        from django.urls import resolve

        resolver = resolve("/api/v1/farms/1/ndmi/timeseries/")
        self.assertIn("ndmi", resolver.view_name or "")

    def test_ndmi_latest_url_resolves(self) -> None:
        from django.urls import resolve

        resolver = resolve("/api/v1/farms/1/ndmi/latest/")
        self.assertIn("ndmi", resolver.view_name or "")

    def test_ndmi_refresh_url_resolves(self) -> None:
        from django.urls import resolve

        resolver = resolve("/api/v1/farms/1/ndmi/refresh/")
        self.assertIn("ndmi", resolver.view_name or "")

    def test_ndmi_raster_png_url_resolves(self) -> None:
        from django.urls import resolve

        resolver = resolve("/api/v1/farms/1/ndmi/raster.png")
        self.assertIn("ndmi", resolver.view_name or "")

    def test_ndmi_raster_queue_url_resolves(self) -> None:
        from django.urls import resolve

        resolver = resolve("/api/v1/farms/1/ndmi/raster/queue")
        self.assertIn("ndmi", resolver.view_name or "")

    def test_ndmi_farm_state_url_resolves(self) -> None:
        from django.urls import resolve

        resolver = resolve("/api/v1/farms/1/ndmi/farm-state/")
        self.assertIn("ndmi", resolver.view_name or "")


class NdmiFarmStateApiTests(NdmiViewMixin, APITestCase):
    """NDMI farm state API endpoint tests."""

    def setUp(self) -> None:
        super().setUp()
        self.url = f"/api/v1/farms/{self.farm.id}/ndmi/farm-state/"
        self.client.force_authenticate(user=self.user)

    def _seed_observations(
        self, mean: float = 0.5, count: int = 5
    ) -> list[NdviObservation]:
        today = date.today()
        obs = []
        for i in range(count):
            o = NdviObservation.objects.create(
                farm=self.farm,
                engine="stac",
                bucket_date=today - timedelta(days=i),
                mean=mean,
                min=mean - 0.1,
                max=mean + 0.1,
                sample_count=100,
                cloud_fraction=0.05,
                index_type="NDMI",
                state=NdviObservation.ObservationState.FINAL,
            )
            obs.append(o)
        return obs

    def test_farm_state_returns_200(self) -> None:
        self._seed_observations(mean=0.5)
        response = self.client.get(self.url)
        assert response.status_code == status.HTTP_200_OK

    def test_farm_state_has_envelope(self) -> None:
        self._seed_observations(mean=-0.3)
        response = self.client.get(self.url)
        data: dict[str, Any] = response.json()
        assert "data" in data
        assert "message" in data
        assert "errors" in data or "success" in data

    def test_farm_state_moisture_classification(self) -> None:
        self._seed_observations(mean=0.5)
        response = self.client.get(self.url)
        data = response.json()
        payload = data.get("data", {})
        assert payload.get("mean_ndmi") == 0.5
        assert payload.get("state") is not None
        assert payload.get("interpretation") is not None
        assert payload.get("action") is not None
        assert payload.get("farm_id") == self.farm.id

    def test_farm_state_dry_moisture(self) -> None:
        self._seed_observations(mean=-0.3)
        response = self.client.get(self.url)
        data = response.json()
        assert data["data"]["state"] == "dry"

    def test_farm_state_returns_404_for_other_user(self) -> None:
        other = get_user_model().objects.create_user(
            username="ndmi_other2",
            password=secrets.token_urlsafe(16),
            email="other2@example.com",
        )
        self.client.force_authenticate(user=other)
        response = self.client.get(self.url)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_farm_state_no_observations(self) -> None:
        response = self.client.get(self.url)
        data = response.json()
        assert data["data"]["state"] == STATE_UNKNOWN

    def test_farm_state_returns_min_ndmi(self) -> None:
        self._seed_observations(mean=0.3)
        response = self.client.get(self.url)
        data = response.json()
        payload = data["data"]
        assert payload.get("min_ndmi") is not None


class ComputeNdmiFarmStateTests(NdmiViewMixin, APITestCase):
    """Unit tests for compute_ndmi_farm_state()."""

    def test_returns_result_for_empty_farm(self) -> None:
        result = compute_ndmi_farm_state(farm=self.farm)
        assert result.farm_id == self.farm.id
        assert result.mean_ndmi is None

    def test_uses_only_ndmi_observations(self) -> None:
        NdviObservation.objects.create(
            farm=self.farm,
            engine="stac",
            bucket_date=date.today(),
            mean=0.8,
            min=0.7,
            max=0.9,
            sample_count=100,
            cloud_fraction=0.05,
            index_type="NDVI",
            state=NdviObservation.ObservationState.FINAL,
        )
        result = compute_ndmi_farm_state(farm=self.farm)
        assert result.mean_ndmi is None

    def test_dry_threshold(self) -> None:
        """mean_ndmi < -0.2 should be classified as dry."""
        NdviObservation.objects.create(
            farm=self.farm,
            engine="stac",
            bucket_date=date.today(),
            mean=-0.3,
            index_type="NDMI",
            state=NdviObservation.ObservationState.FINAL,
        )
        result = compute_ndmi_farm_state(farm=self.farm)
        assert result.state == "dry"

    def test_moist_state(self) -> None:
        """mean_ndmi between -0.2 and 0.2 with no declining trend is moist."""
        NdviObservation.objects.create(
            farm=self.farm,
            engine="stac",
            bucket_date=date.today(),
            mean=0.0,
            index_type="NDMI",
            state=NdviObservation.ObservationState.FINAL,
        )
        result = compute_ndmi_farm_state(farm=self.farm)
        assert result.state == "moist"

    def test_declining_trend(self) -> None:
        """Multiple observations with decreasing mean should be declining."""
        today = date.today()
        NdviObservation.objects.create(
            farm=self.farm,
            engine="stac",
            bucket_date=today - timedelta(days=2),
            mean=0.1,
            index_type="NDMI",
            state=NdviObservation.ObservationState.FINAL,
        )
        NdviObservation.objects.create(
            farm=self.farm,
            engine="stac",
            bucket_date=today - timedelta(days=1),
            mean=0.05,
            index_type="NDMI",
            state=NdviObservation.ObservationState.FINAL,
        )
        NdviObservation.objects.create(
            farm=self.farm,
            engine="stac",
            bucket_date=today,
            mean=0.0,
            index_type="NDMI",
            state=NdviObservation.ObservationState.FINAL,
        )
        result = compute_ndmi_farm_state(farm=self.farm)
        assert result.state == "declining"
        assert result.trend is not None
        assert result.trend < 0

    def test_saturated_threshold(self) -> None:
        """mean_ndmi > 0.2 should be classified as saturated."""
        NdviObservation.objects.create(
            farm=self.farm,
            engine="stac",
            bucket_date=date.today(),
            mean=0.25,
            index_type="NDMI",
            state=NdviObservation.ObservationState.FINAL,
        )
        result = compute_ndmi_farm_state(farm=self.farm)
        assert result.state == "saturated"

    def test_water_threshold(self) -> None:
        """mean_ndmi > 0.3 should be classified as water."""
        NdviObservation.objects.create(
            farm=self.farm,
            engine="stac",
            bucket_date=date.today(),
            mean=0.35,
            index_type="NDMI",
            state=NdviObservation.ObservationState.FINAL,
        )
        result = compute_ndmi_farm_state(farm=self.farm)
        assert result.state == "water"
