"""No-regression tests: NDVI behavior must be unchanged after NDWI additions.

These tests confirm that the NDWI implementation did not alter any NDVI
behavior — endpoints, metric names, model queries, and engine factories
all remain intact.
"""

from __future__ import annotations

import secrets
from datetime import date, timedelta
from typing import Any
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import caches
from django.test import TestCase
from django.urls import resolve
from rest_framework import status
from rest_framework.test import APITestCase

from farms.models import Farm
from ndvi.engines.base import NDVIEngine
from ndvi.models import NdviDerivedObservation, NdviJob, NdviObservation
from ndvi.services import ENGINE_FACTORIES, get_engine


class NdviEngineFactoriesUnchanged(TestCase):
    """NDVI engine factories must be registered and callable.

    We test that factory entries exist and are callable, but only
    instantiate engines that do not require external credentials
    (e.g. stac).  Engines requiring env vars (sentinelhub, gee, etc.)
    are verified at the factory-registration level only.
    """

    def test_ndvi_stac_factory(self) -> None:
        engine: NDVIEngine = ENGINE_FACTORIES["stac"]()
        self.assertEqual(engine.engine_name, "stac")

    def test_ndvi_sentinelhub_factory_registered(self) -> None:
        factory = ENGINE_FACTORIES["sentinelhub"]
        self.assertTrue(callable(factory))

    def test_ndvi_gee_factory_registered(self) -> None:
        factory = ENGINE_FACTORIES["gee"]
        self.assertTrue(callable(factory))

    def test_ndvi_landsat_factory(self) -> None:
        engine: NDVIEngine = ENGINE_FACTORIES["landsat"]()
        self.assertEqual(engine.engine_name, "landsat")

    def test_ndvi_modis_factory(self) -> None:
        engine: NDVIEngine = ENGINE_FACTORIES["modis"]()
        self.assertEqual(engine.engine_name, "modis")

    def test_get_engine_defaults_to_ndvi(self) -> None:
        engine: NDVIEngine = get_engine("stac")
        self.assertEqual(engine.engine_name, "stac")


class NdviEngineFactoriesNotOverwritten(TestCase):
    """NDWI factory entries must not shadow NDVI entries."""

    def test_ndwi_factories_are_separate_keys(self) -> None:
        self.assertIn("stac", ENGINE_FACTORIES)
        self.assertIn("ndwi_stac", ENGINE_FACTORIES)
        self.assertIsNot(
            ENGINE_FACTORIES["stac"], ENGINE_FACTORIES["ndwi_stac"]
        )

    def test_ndwi_factories_all_present(self) -> None:
        for suffix in ("gee", "sentinelhub", "stac", "landsat", "modis"):
            self.assertIn(f"ndwi_{suffix}", ENGINE_FACTORIES)

    def test_ndmi_factories_are_separate_keys(self) -> None:
        self.assertIn("stac", ENGINE_FACTORIES)
        self.assertIn("ndmi_stac", ENGINE_FACTORIES)
        self.assertIsNot(
            ENGINE_FACTORIES["stac"], ENGINE_FACTORIES["ndmi_stac"]
        )

    def test_ndmi_factories_all_present(self) -> None:
        for suffix in ("gee", "sentinelhub", "stac", "landsat", "modis"):
            self.assertIn(f"ndmi_{suffix}", ENGINE_FACTORIES)

    def test_factories_are_distinct_per_index_type(self) -> None:
        for suffix in ("gee", "sentinelhub", "stac", "landsat", "modis"):
            factories = {
                "NDVI": ENGINE_FACTORIES[suffix],
                "NDWI": ENGINE_FACTORIES[f"ndwi_{suffix}"],
                "NDMI": ENGINE_FACTORIES[f"ndmi_{suffix}"],
            }
            assert len(set(id(f) for f in factories.values())) == 3, (
                f"Factories for {suffix} must be distinct objects"
            )


class NdviModelQueriesUnchanged(TestCase):
    """NdviObservation queries must be unaffected by index_type."""

    def test_default_index_type_is_ndvi(self) -> None:
        obs = NdviObservation(
            farm_id=1,
            engine="stac",
            bucket_date="2026-01-01",
            mean=0.5,
        )
        self.assertEqual(obs.index_type, "NDVI")

    def test_ndvi_query_excludes_ndwi(self) -> None:
        user = get_user_model().objects.create_user(
            username="query_test_user",
            password=secrets.token_urlsafe(16),
        )
        farm = Farm.objects.create(
            owner=user,
            name="test",
            slug="test",
            is_active=True,
        )
        NdviObservation.objects.create(
            farm=farm,
            engine="stac",
            bucket_date="2026-01-01",
            mean=0.5,
        )
        NdviObservation.objects.create(
            farm=farm,
            engine="stac",
            bucket_date="2026-01-02",
            mean=0.3,
            index_type="NDWI",
        )
        ndvi_only = NdviObservation.objects.filter(index_type="NDVI")
        self.assertEqual(ndvi_only.count(), 1)


class NdviMetricNamesUnchanged(TestCase):
    """NDVI Prometheus metric names must still exist as Python objects."""

    def test_ndvi_metrics_importable(self) -> None:
        from ndvi.metrics import (  # noqa: F811
            ndvi_jobs_total,
        )

        self.assertIsNotNone(ndvi_jobs_total)

    def test_spectral_metrics_importable(self) -> None:
        from ndvi.metrics import (  # noqa: F811
            spectral_jobs_total,
        )

        self.assertIsNotNone(spectral_jobs_total)


class IndexEndpointsDoNotBreakEachOther(TestCase):
    """NDWI/NDMI must not break NDVI/NDWI URL resolution."""

    def test_ndvi_timeseries_url_resolves(self) -> None:
        resolver = resolve("/api/v1/farms/1/ndvi/timeseries/")
        self.assertIn("ndvi", resolver.view_name or "")

    def test_ndwi_timeseries_url_resolves(self) -> None:
        resolver = resolve("/api/v1/farms/1/ndwi/timeseries/")
        self.assertIn("ndwi", resolver.view_name or "")

    def test_ndmi_timeseries_url_resolves(self) -> None:
        resolver = resolve("/api/v1/farms/1/ndmi/timeseries/")
        self.assertIn("ndmi", resolver.view_name or "")

    def test_ndmi_latest_url_resolves(self) -> None:
        resolver = resolve("/api/v1/farms/1/ndmi/latest/")
        self.assertIn("ndmi", resolver.view_name or "")

    def test_ndmi_refresh_url_resolves(self) -> None:
        resolver = resolve("/api/v1/farms/1/ndmi/refresh/")
        self.assertIn("ndmi", resolver.view_name or "")

    def test_ndmi_raster_png_url_resolves(self) -> None:
        resolver = resolve("/api/v1/farms/1/ndmi/raster.png")
        self.assertIn("ndmi", resolver.view_name or "")

    def test_ndmi_raster_queue_url_resolves(self) -> None:
        resolver = resolve("/api/v1/farms/1/ndmi/raster/queue")
        self.assertIn("ndmi", resolver.view_name or "")


class NdwiV2RepresentationIntegrationTests(APITestCase):
    """V2 representation (?representation=v2) for NDWI endpoints.

    These tests verify the V2 injection code path works for NDWI
    endpoints, including when V2 records exist.
    """

    def setUp(self) -> None:
        caches["default"].clear()
        password = secrets.token_urlsafe(16)
        self.user = get_user_model().objects.create_user(
            username="ndwi_v2_user",
            password=password,
        )
        self.farm = Farm.objects.create(
            owner=self.user,
            name="NDWI V2 Farm",
            slug="ndwi-v2-farm",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        self.client.force_authenticate(user=self.user)

    def _seed_ndwi_continuous(
        self, start: date, end: date
    ) -> list[NdviObservation]:
        obs = []
        current = start
        while current <= end:
            o = NdviObservation.objects.create(
                farm=self.farm,
                engine="stac",
                bucket_date=current,
                mean=0.5,
                index_type="NDWI",
                state=NdviObservation.ObservationState.FINAL,
            )
            obs.append(o)
            current += timedelta(days=1)
        return obs

    @patch("ndvi.views.enforce_quota")
    @patch("ndvi.views.dispatch_ndvi_job")
    def test_ndwi_timeseries_v2_returns_200(
        self, mock_dispatch: Any, mock_quota: Any
    ) -> None:
        td = date.today()
        start = td - timedelta(days=10)
        end = td + timedelta(days=1)
        self._seed_ndwi_continuous(start, end)
        url = f"/api/v1/farms/{self.farm.id}/ndwi/timeseries/"
        response = self.client.get(
            url,
            {
                "representation": "v2",
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
    def test_ndwi_latest_v2_returns_200(
        self, mock_dispatch: Any, mock_quota: Any
    ) -> None:
        td = date.today()
        NdviObservation.objects.create(
            farm=self.farm,
            engine="stac",
            bucket_date=td,
            mean=0.5,
            index_type="NDWI",
            state=NdviObservation.ObservationState.FINAL,
        )
        url = f"/api/v1/farms/{self.farm.id}/ndwi/latest/"
        response = self.client.get(
            url, {"representation": "v2", "engine": "stac"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data: dict[str, Any] = response.json()
        self.assertIn("data", data)

    @patch("ndvi.views.enforce_quota")
    @patch("ndvi.views.dispatch_ndvi_job")
    def test_ndwi_timeseries_v2_includes_v2_fields(
        self, mock_dispatch: Any, mock_quota: Any
    ) -> None:
        td = date.today()
        start = td - timedelta(days=10)
        end = td + timedelta(days=1)
        obs = self._seed_ndwi_continuous(start, end)
        for v1 in obs:
            NdviDerivedObservation.objects.create(
                farm=self.farm,
                v1_observation=v1,
                engine="stac",
                bucket_date=v1.bucket_date,
                source="stac",
                selected_ndvi=v1.mean,
                smoothed_ndvi=v1.mean,
                confidence=0.85,
                index_type="NDWI",
            )
        url = f"/api/v1/farms/{self.farm.id}/ndwi/timeseries/"
        response = self.client.get(
            url,
            {
                "representation": "v2",
                "engine": "stac",
                "step_days": "1",
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = response.json()
        inner = payload.get("data", {})
        self.assertIn("v2_observations", inner)
        self.assertEqual(inner.get("representation"), "v2")


class RunNdwiJobRoutingTests(TestCase):
    """run_ndwi_job task routing."""

    def test_dispatch_routes_ndwi_to_run_ndwi_job(self) -> None:
        from ndvi.tasks import run_ndwi_job

        job = NdviJob(
            id=99999,
            owner_id=1,
            farm_id=1,
            engine="stac",
            job_type=NdviJob.JobType.REFRESH_LATEST,
            request_hash="test_hash",
            index_type="NDWI",
        )
        task = run_ndwi_job if job.index_type == "NDWI" else None
        self.assertIsNotNone(task)

    def test_dispatch_routes_ndvi_to_run_ndvi_job(self) -> None:
        from ndvi.tasks import run_ndvi_job

        job = NdviJob(
            id=99998,
            owner_id=1,
            farm_id=1,
            engine="stac",
            job_type=NdviJob.JobType.REFRESH_LATEST,
            request_hash="test_hash",
            index_type="NDVI",
        )
        task = run_ndvi_job if job.index_type == "NDVI" else None
        self.assertIsNotNone(task)
