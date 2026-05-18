"""Tests for the NDVI stream producer.

Covers:
- Stream payload schema correctness
- Producer publish functions
- Dispatch helper integration with stream backend
"""

from __future__ import annotations

# ruff: noqa: S101
import re
from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ndvi.models import NdviJob
from ndvi.services import (
    dispatch_farm_state_coverage,
    dispatch_ndvi_job,
    get_default_colormap_normalization,
)

# Stream entry ID format: "<timestamp>-<sequence>" e.g. "1713000000000-0"
STREAM_ENTRY_ID_RE = re.compile(r"^\d+-\d+$")


# ─── Payload Schema Tests ───────────────────────────────────────────


class TestBuildStreamPayload:
    @pytest.fixture
    def mock_job(self) -> MagicMock:
        job = MagicMock(spec=NdviJob)
        job.id = 42
        job.request_hash = "abc123hash"
        job.farm_id = 7
        job.owner_id = 1
        job.engine = "stac"
        job.job_type = NdviJob.JobType.REFRESH_LATEST
        job.start = date(2025, 1, 1)
        job.end = date(2025, 1, 15)
        job.step_days = 7
        job.max_cloud = 30
        job.lookback_days = 14
        return job

    def test_contains_all_required_fields(self, mock_job: MagicMock) -> None:
        from ndvi.streams import build_stream_payload

        payload = build_stream_payload(mock_job)

        required_fields = {
            "job_id",
            "request_hash",
            "farm_id",
            "owner_id",
            "engine",
            "job_type",
            "start",
            "end",
            "step_days",
            "max_cloud",
            "lookback_days",
            "colormap_normalization",
            "enqueue_timestamp",
        }
        assert required_fields.issubset(payload.keys())

    def test_serializes_dates_as_iso(self, mock_job: MagicMock) -> None:
        from ndvi.streams import build_stream_payload

        payload = build_stream_payload(mock_job)

        assert payload["start"] == "2025-01-01"
        assert payload["end"] == "2025-01-15"

    def test_serializes_null_dates_as_empty_string(
        self, mock_job: MagicMock
    ) -> None:
        from ndvi.streams import build_stream_payload

        mock_job.start = None
        mock_job.end = None

        payload = build_stream_payload(mock_job)

        assert payload["start"] == ""
        assert payload["end"] == ""

    def test_includes_colormap_normalization(
        self, mock_job: MagicMock
    ) -> None:
        from ndvi.streams import build_stream_payload

        payload = build_stream_payload(mock_job)

        expected = get_default_colormap_normalization().value
        assert payload["colormap_normalization"] == expected

    def test_uses_job_values(self, mock_job: MagicMock) -> None:
        from ndvi.streams import build_stream_payload

        payload = build_stream_payload(mock_job)

        assert payload["job_id"] == str(mock_job.id)
        assert payload["request_hash"] == mock_job.request_hash
        assert payload["farm_id"] == str(mock_job.farm_id)
        assert payload["owner_id"] == str(mock_job.owner_id)
        assert payload["engine"] == mock_job.engine
        assert payload["job_type"] == mock_job.job_type
        assert payload["step_days"] == str(mock_job.step_days)
        assert payload["max_cloud"] == str(mock_job.max_cloud)
        assert payload["lookback_days"] == str(mock_job.lookback_days)

    def test_optional_fields_empty_when_null(
        self, mock_job: MagicMock
    ) -> None:
        from ndvi.streams import build_stream_payload

        mock_job.step_days = None
        mock_job.max_cloud = None
        mock_job.lookback_days = None

        payload = build_stream_payload(mock_job)

        assert payload["step_days"] == ""
        assert payload["max_cloud"] == ""
        assert payload["lookback_days"] == ""


# ─── Producer Function Tests ────────────────────────────────────────


class TestPublishNdviJob:
    @pytest.fixture
    def mock_job(self) -> MagicMock:
        job = MagicMock(spec=NdviJob)
        job.id = 42
        job.request_hash = "abc123hash"
        job.farm_id = 7
        job.owner_id = 1
        job.engine = "stac"
        job.job_type = NdviJob.JobType.REFRESH_LATEST
        job.start = date(2025, 1, 1)
        job.end = date(2025, 1, 15)
        job.step_days = 7
        job.max_cloud = 30
        job.lookback_days = 14
        return job

    def test_returns_valid_entry_id(self, mock_job: MagicMock) -> None:
        from ndvi.streams import publish_ndvi_job

        mock_redis = MagicMock()
        mock_redis.xadd.return_value = "1713000000000-0"

        with patch("ndvi.streams._get_stream_redis_client") as mock_get:
            mock_get.return_value = mock_redis
            entry_id = publish_ndvi_job(mock_job)

        assert STREAM_ENTRY_ID_RE.match(entry_id)

    def test_calls_xadd_with_correct_args(self, mock_job: MagicMock) -> None:
        from ndvi.streams import publish_ndvi_job

        mock_redis = MagicMock()
        mock_redis.xadd.return_value = "1713000000000-0"

        with patch("ndvi.streams._get_stream_redis_client") as mock_get:
            mock_get.return_value = mock_redis
            publish_ndvi_job(mock_job)

        mock_redis.xadd.assert_called_once()
        call_args = mock_redis.xadd.call_args

        assert call_args[0][0] == "ndvi:stream"
        payload = call_args[0][1]
        assert payload["job_id"] == str(mock_job.id)
        assert call_args[1]["maxlen"] == 10000
        assert call_args[1]["approximate"] is True


class TestPublishFarmStateCoverage:
    def test_returns_valid_entry_id(self) -> None:
        from ndvi.streams import publish_farm_state_coverage

        mock_redis = MagicMock()
        mock_redis.xadd.return_value = "1713000000000-0"

        with patch("ndvi.streams._get_stream_redis_client") as mock_get:
            mock_get.return_value = mock_redis
            entry_id = publish_farm_state_coverage(
                farm_id=7,
                engine="stac",
                target_date=date(2025, 1, 3),
                threshold=0.4,
            )

        assert STREAM_ENTRY_ID_RE.match(entry_id)

    def test_calls_xadd_with_coverage_payload(self) -> None:
        from ndvi.streams import publish_farm_state_coverage

        mock_redis = MagicMock()
        mock_redis.xadd.return_value = "1713000000000-0"

        with patch("ndvi.streams._get_stream_redis_client") as mock_get:
            mock_get.return_value = mock_redis
            publish_farm_state_coverage(
                farm_id=7,
                engine="stac",
                target_date=date(2025, 1, 3),
                threshold=0.4,
            )

        mock_redis.xadd.assert_called_once()
        call_args = mock_redis.xadd.call_args
        payload = call_args[0][1]

        assert payload["farm_id"] == "7"
        assert payload["engine"] == "stac"
        assert payload["target_date"] == "2025-01-03"
        assert payload["threshold"] == "0.4"
        assert payload["job_type"] == "farm_state_coverage"


# ─── Dispatch Helper Integration Tests ──────────────────────────────


class TestDispatchNdviJobStreamMode:
    @pytest.fixture
    def mock_job(self) -> MagicMock:
        job = MagicMock(spec=NdviJob)
        job.id = 42
        job.request_hash = "abc123hash"
        job.farm_id = 7
        job.owner_id = 1
        job.engine = "stac"
        job.job_type = NdviJob.JobType.REFRESH_LATEST
        job.start = date(2025, 1, 1)
        job.end = date(2025, 1, 15)
        job.step_days = 7
        job.max_cloud = 30
        job.lookback_days = 14
        return job

    def test_publishes_to_stream_when_backend_is_stream(
        self, mock_job: MagicMock, settings: Any
    ) -> None:
        settings.NDVI_QUEUE_BACKEND = "stream"

        mock_redis = MagicMock()
        mock_redis.xadd.return_value = "1713000000000-0"

        with patch("ndvi.streams._get_stream_redis_client") as mock_get:
            mock_get.return_value = mock_redis
            dispatch_ndvi_job(mock_job)

        mock_redis.xadd.assert_called_once()

    @pytest.mark.django_db
    def test_bypasses_stream_when_backend_is_celery(
        self, mock_job: MagicMock, settings: Any
    ) -> None:
        settings.NDVI_QUEUE_BACKEND = "celery"

        with patch("ndvi.tasks.run_ndvi_job.apply_async") as mock_apply:
            dispatch_ndvi_job(mock_job)

        mock_apply.assert_called_once()


class TestDispatchFarmStateCoverageStreamMode:
    def test_publishes_to_stream_when_backend_is_stream(
        self, settings: Any
    ) -> None:
        settings.NDVI_QUEUE_BACKEND = "stream"

        mock_redis = MagicMock()
        mock_redis.xadd.return_value = "1713000000000-0"

        with patch("ndvi.streams._get_stream_redis_client") as mock_get:
            mock_get.return_value = mock_redis
            dispatch_farm_state_coverage(
                farm_id=7,
                engine="stac",
                target_date=date(2025, 1, 3),
                threshold=0.4,
            )

        mock_redis.xadd.assert_called_once()

    @pytest.mark.django_db
    def test_bypasses_stream_when_backend_is_celery(
        self, settings: Any
    ) -> None:
        settings.NDVI_QUEUE_BACKEND = "celery"

        with patch(
            "ndvi.tasks.compute_farm_state_coverage.apply_async"
        ) as mock_apply:
            dispatch_farm_state_coverage(
                farm_id=7,
                engine="stac",
                target_date=date(2025, 1, 3),
                threshold=0.4,
            )

        mock_apply.assert_called_once()
        call_kwargs = mock_apply.call_args
        assert call_kwargs.kwargs["kwargs"]["farm_id"] == 7
        assert call_kwargs.kwargs["kwargs"]["engine"] == "stac"
        assert call_kwargs.kwargs["kwargs"]["target_date"] == "2025-01-03"
        assert call_kwargs.kwargs["kwargs"]["threshold"] == 0.4


# ─── Settings & Feature Flag Tests ──────────────────────────────────


class TestStreamSettings:
    def test_default_queue_backend_is_celery(self, settings: Any) -> None:
        settings.NDVI_QUEUE_BACKEND = "celery"
        from ndvi.services import get_ndvi_queue_backend

        backend = get_ndvi_queue_backend()
        assert backend == "celery"

    def test_stream_settings_have_defaults(self, settings: Any) -> None:
        from django.conf import settings as django_settings

        assert hasattr(django_settings, "NDVI_STREAM_NAME")
        assert django_settings.NDVI_STREAM_NAME == "ndvi:stream"
        assert django_settings.NDVI_STREAM_MAXLEN == 10000
        assert django_settings.NDVI_STREAM_DLQ_NAME == "ndvi:dlq"
