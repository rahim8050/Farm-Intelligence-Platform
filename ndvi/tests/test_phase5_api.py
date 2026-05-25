"""Tests for Phase 5 API Evolution: ?representation=v2 support."""

from __future__ import annotations

import secrets
from datetime import date, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.cache import caches
from rest_framework import status
from rest_framework.test import APITestCase

from farms.models import Farm
from ndvi.models import NdviDerivedObservation, NdviObservation
from ndvi.services import get_default_ndvi_engine_name


class Phase5RepresentationTests(APITestCase):
    """V2 representation payload on timeseries, latest, farm-state."""

    def setUp(self) -> None:
        caches["default"].clear()
        password = secrets.token_urlsafe(16)
        self.user = get_user_model().objects.create_user(
            username="owner",
            password=password,
            email="owner@example.com",
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
        self.engine = get_default_ndvi_engine_name()
        self.timeseries_url = f"/api/v1/farms/{self.farm.id}/ndvi/timeseries/"
        self.latest_url = f"/api/v1/farms/{self.farm.id}/ndvi/latest/"
        self.farm_state_url = f"/api/v1/farm-state/{self.farm.id}/"
        self.client.force_authenticate(user=self.user)

        self._seed_observations()

    def _seed_observations(self) -> None:
        today = date.today()
        self.dates = [today - timedelta(days=i) for i in range(5)]
        self.v1_obs = []
        for i, d in enumerate(self.dates):
            obs = NdviObservation.objects.create(
                farm=self.farm,
                engine=self.engine,
                bucket_date=d,
                mean=0.5 + i * 0.05,
                min=0.4 + i * 0.05,
                max=0.6 + i * 0.05,
                sample_count=100,
                cloud_fraction=0.05,
            )
            self.v1_obs.append(obs)

        self.v2_obs = []
        for i, obs in enumerate(self.v1_obs[:3]):
            v2 = NdviDerivedObservation.objects.create(
                farm=self.farm,
                v1_observation=obs,
                engine=self.engine,
                bucket_date=obs.bucket_date,
                source="sentinel-2",
                selected_ndvi=obs.mean,
                smoothed_ndvi=obs.mean + 0.02,
                confidence=0.85 + i * 0.03,
                quality_flags={"cloud_heavy": False, "low_confidence": False},
                is_null=False,
            )
            self.v2_obs.append(v2)

    # -- timeseries -------------------------------------------------------

    @patch("ndvi.views.dispatch_ndvi_job")
    def test_timeseries_v1_default_no_v2_fields(
        self, mock_dispatch: MagicMock
    ) -> None:
        """Default (no representation param) returns V1-only payload."""
        resp = self.client.get(
            self.timeseries_url,
            {"start": self.dates[-1].isoformat(), "end": self.dates[0].isoformat()},
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data: dict[str, Any] = resp.json().get("data", {})
        assert "v2_observations" not in data
        assert "representation" not in data

        for obs in data.get("observations", []):
            assert "smoothed_ndvi" not in obs

    @patch("ndvi.views.dispatch_ndvi_job")
    def test_timeseries_v2_adds_v2_fields(
        self, mock_dispatch: MagicMock
    ) -> None:
        """representation=v2 adds v2_observations and inline V2 fields."""
        resp = self.client.get(
            self.timeseries_url,
            {
                "start": self.dates[-1].isoformat(),
                "end": self.dates[0].isoformat(),
                "representation": "v2",
            },
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data: dict[str, Any] = resp.json().get("data", {})
        assert data.get("representation") == "v2"
        assert "v2_observations" in data

    @patch("ndvi.views.dispatch_ndvi_job")
    def test_timeseries_v2_inline_fields_populated(
        self, mock_dispatch: MagicMock
    ) -> None:
        """Each observation gets inline V2 fields when representation=v2."""
        resp = self.client.get(
            self.timeseries_url,
            {
                "start": self.dates[-1].isoformat(),
                "end": self.dates[0].isoformat(),
                "representation": "v2",
            },
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data: dict[str, Any] = resp.json().get("data", {})
        observations = data.get("observations", [])

        for obs_data in observations:
            assert "smoothed_ndvi" in obs_data
            assert "confidence" in obs_data
            assert "source" in obs_data
            assert "quality_flags" in obs_data

    @patch("ndvi.views.dispatch_ndvi_job")
    def test_timeseries_v2_values_match_derived(
        self, mock_dispatch: MagicMock
    ) -> None:
        """V2 inline fields match NdviDerivedObservation values."""
        resp = self.client.get(
            self.timeseries_url,
            {
                "start": self.dates[-1].isoformat(),
                "end": self.dates[0].isoformat(),
                "representation": "v2",
            },
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data: dict[str, Any] = resp.json().get("data", {})

        for obs_data in data.get("observations", []):
            iso = obs_data["bucket_date"]
            parsed = date.fromisoformat(iso) if isinstance(iso, str) else iso
            v2 = NdviDerivedObservation.objects.filter(
                farm=self.farm, engine=self.engine, bucket_date=parsed
            ).first()
            if v2:
                assert obs_data["smoothed_ndvi"] == v2.smoothed_ndvi
                assert obs_data["confidence"] == v2.confidence
                assert obs_data["source"] == v2.source
                assert obs_data["quality_flags"] == v2.quality_flags
            else:
                assert obs_data["smoothed_ndvi"] is None
                assert obs_data["confidence"] is None
                assert obs_data["source"] is None
                assert obs_data["quality_flags"] is None

    @patch("ndvi.views.dispatch_ndvi_job")
    def test_timeseries_v2_observations_list_match(
        self, mock_dispatch: MagicMock
    ) -> None:
        """v2_observations list corresponds to each observation."""
        resp = self.client.get(
            self.timeseries_url,
            {
                "start": self.dates[-1].isoformat(),
                "end": self.dates[0].isoformat(),
                "representation": "v2",
            },
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data: dict[str, Any] = resp.json().get("data", {})
        v2_list: list[dict[str, Any]] = data.get("v2_observations", [])
        observations: list[dict[str, Any]] = data.get("observations", [])
        assert len(v2_list) == len(observations)

        for obs_data, v2_entry in zip(observations, v2_list, strict=True):
            assert obs_data["smoothed_ndvi"] == v2_entry.get("smoothed_ndvi")
            assert obs_data["confidence"] == v2_entry.get("confidence")
            assert obs_data["source"] == v2_entry.get("source")
            assert obs_data["quality_flags"] == v2_entry.get("quality_flags")

    # -- latest -----------------------------------------------------------

    @patch("ndvi.views.dispatch_ndvi_job")
    def test_latest_v1_default_no_v2_fields(
        self, mock_dispatch: MagicMock
    ) -> None:
        """Default latest response has no V2 fields."""
        self.v1_obs[0].is_latest = True  # type: ignore[attr-defined]
        self.v1_obs[0].save()

        resp = self.client.get(self.latest_url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data: dict[str, Any] = resp.json().get("data", {})
        assert "v2_observation" not in data
        assert "representation" not in data
        obs_data = data.get("observation", {})
        if obs_data:
            assert "smoothed_ndvi" not in obs_data

    @patch("ndvi.views.dispatch_ndvi_job")
    def test_latest_v2_adds_v2_observation(
        self, mock_dispatch: MagicMock
    ) -> None:
        """representation=v2 adds v2_observation to latest payload."""
        self.v1_obs[0].is_latest = True  # type: ignore[attr-defined]
        self.v1_obs[0].save()

        resp = self.client.get(self.latest_url, {"representation": "v2"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data: dict[str, Any] = resp.json().get("data", {})
        assert data.get("representation") == "v2"
        assert "v2_observation" in data

    @patch("ndvi.views.dispatch_ndvi_job")
    def test_latest_v2_populated_values(
        self, mock_dispatch: MagicMock
    ) -> None:
        """V2 fields contain correct NdviDerivedObservation data."""
        self.v1_obs[0].is_latest = True  # type: ignore[attr-defined]
        self.v1_obs[0].save()
        v2 = self.v2_obs[0]

        resp = self.client.get(self.latest_url, {"representation": "v2"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data: dict[str, Any] = resp.json().get("data", {})
        v2_data: dict[str, Any] = data.get("v2_observation", {})
        assert v2_data["smoothed_ndvi"] == v2.smoothed_ndvi
        assert v2_data["confidence"] == v2.confidence
        assert v2_data["source"] == v2.source
        assert v2_data["quality_flags"] == v2.quality_flags

    @patch("ndvi.views.dispatch_ndvi_job")
    def test_latest_v2_null_when_no_derived(
        self, mock_dispatch: MagicMock
    ) -> None:
        """V2 fields are null when no NdviDerivedObservation exists."""
        NdviObservation.objects.create(
            farm=self.farm,
            engine=self.engine,
            bucket_date=date.today() + timedelta(days=1),
            mean=0.8,
            min=0.7,
            max=0.9,
        )

        resp = self.client.get(self.latest_url, {"representation": "v2"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data: dict[str, Any] = resp.json().get("data", {})
        v2_data: dict[str, Any] = data.get("v2_observation", {})
        assert v2_data["smoothed_ndvi"] is None
        assert v2_data["confidence"] is None

    # -- farm-state -------------------------------------------------------

    @patch("ndvi.services.enforce_quota")
    def test_farm_state_v1_default_no_v2_fields(
        self, mock_quota: MagicMock
    ) -> None:
        """Default farm-state response has no V2 fields."""
        resp = self.client.get(self.farm_state_url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data: dict[str, Any] = resp.json().get("data", {})
        assert "v2_observation" not in data
        assert "representation" not in data

    @patch("ndvi.services.enforce_quota")
    def test_farm_state_v2_adds_v2_observation(
        self, mock_quota: MagicMock
    ) -> None:
        """representation=v2 adds v2_observation to farm-state."""
        resp = self.client.get(self.farm_state_url, {"representation": "v2"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data: dict[str, Any] = resp.json().get("data", {})
        assert data.get("representation") == "v2"
        assert "v2_observation" in data

    @patch("ndvi.services.enforce_quota")
    def test_farm_state_v2_values_populated(
        self, mock_quota: MagicMock
    ) -> None:
        """Farm-state V2 observation has correct values."""
        resp = self.client.get(self.farm_state_url, {"representation": "v2"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data: dict[str, Any] = resp.json().get("data", {})
        v2_data: dict[str, Any] = data.get("v2_observation", {})

        latest_v2 = (
            NdviDerivedObservation.objects.filter(farm=self.farm)
            .order_by("-bucket_date")
            .first()
        )
        assert latest_v2 is not None
        assert v2_data["smoothed_ndvi"] == latest_v2.smoothed_ndvi
        assert v2_data["confidence"] == latest_v2.confidence
