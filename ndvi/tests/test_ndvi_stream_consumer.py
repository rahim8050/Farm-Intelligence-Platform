"""Tests for NDVI stream consumer observability hooks."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import redis

from ndvi.management.commands.consume_ndvi_stream import Command


class TestStreamConsumerMetrics:
    def test_starts_metrics_server_on_configured_port(self) -> None:
        command = Command()

        with (
            patch("django.conf.settings.NDVI_STREAM_METRICS_PORT", 8002),
            patch(
                "ndvi.management.commands.consume_ndvi_stream.start_http_server"
            ) as mock_start_http_server,
        ):
            command._start_metrics_server()

        mock_start_http_server.assert_called_once_with(8002)
        assert command._metrics_server_started

    def test_updates_pending_and_age_metrics(self) -> None:
        command = Command()
        command.consumer_name = "test-consumer"
        mock_client = MagicMock()
        mock_client.xpending.return_value = {
            "pending": 3,
            "min": "1713000000000-0",
            "max": "1713000009000-0",
        }
        mock_client.xpending_range.return_value = [
            {
                "message_id": "1713000000000-0",
                "consumer": "test-consumer",
                "time_since_delivered": 120000,
                "times_delivered": 2,
            }
        ]

        with (
            patch(
                "ndvi.management.commands.consume_ndvi_stream."
                "redis_stream_pending_entries"
            ) as pending_metric,
            patch(
                "ndvi.management.commands.consume_ndvi_stream."
                "redis_stream_pending_age_max"
            ) as age_metric,
        ):
            command._update_stream_metrics(mock_client)

        pending_metric.labels.assert_called_once_with(group="ndvi-group")
        pending_metric.labels().set.assert_called_once_with(3)
        age_metric.labels.assert_called_once_with(group="ndvi-group")
        age_metric.labels().set.assert_called_once_with(120.0)

    def test_heartbeat_records_timestamp(self) -> None:
        command = Command()
        command.consumer_name = "test-consumer"

        with patch(
            "ndvi.management.commands.consume_ndvi_stream."
            "ndvi_stream_consumer_heartbeat"
        ) as heartbeat_metric:
            command._mark_heartbeat()

        heartbeat_metric.labels.assert_called_once_with(
            consumer="test-consumer"
        )
        heartbeat_metric.labels().set.assert_called_once()

    def test_failure_counter_tracks_failure_type(self) -> None:
        command = Command()
        command.consumer_name = "test-consumer"

        with patch(
            "ndvi.management.commands.consume_ndvi_stream."
            "ndvi_stream_consumer_failures_total"
        ) as failure_metric:
            command._record_failure("loop_exception")

        failure_metric.labels.assert_called_once_with(
            consumer="test-consumer",
            failure_type="loop_exception",
        )
        failure_metric.labels().inc.assert_called_once()


class TestStreamConsumerProcessing:
    def test_ensure_group_creates_if_missing(self) -> None:
        command = Command()
        command.consumer_name = "test-consumer"
        mock_client = MagicMock()

        with (
            patch("django.conf.settings.NDVI_STREAM_NAME", "test-stream"),
            patch("django.conf.settings.NDVI_STREAM_GROUP", "test-group"),
            patch("django.conf.settings.NDVI_STREAM_START_ID", "0"),
        ):
            command._ensure_group(mock_client)

        mock_client.xgroup_create.assert_called_once_with(
            "test-stream", "test-group", id="0", mkstream=True
        )

    def test_ensure_group_ignores_busygroup(self) -> None:
        command = Command()
        command.consumer_name = "test-consumer"
        mock_client = MagicMock()
        mock_client.xgroup_create.side_effect = redis.ResponseError(
            "BUSYGROUP Consumer Group name already exists"
        )

        with (
            patch("django.conf.settings.NDVI_STREAM_NAME", "test-stream"),
            patch("django.conf.settings.NDVI_STREAM_GROUP", "test-group"),
            patch("django.conf.settings.NDVI_STREAM_START_ID", "0"),
        ):
            command._ensure_group(mock_client)

        mock_client.xgroup_create.assert_called_once()

    def test_read_messages_decodes_correctly(self) -> None:
        command = Command()
        command.consumer_name = "test-consumer"
        mock_client = MagicMock()
        mock_client.xreadgroup.return_value = [
            (
                b"stream",
                [(b"1-0", {b"job_id": b"123", b"job_type": b"refresh"})],
            )
        ]

        with (
            patch("django.conf.settings.NDVI_STREAM_NAME", "test-stream"),
            patch("django.conf.settings.NDVI_STREAM_GROUP", "test-group"),
            patch("django.conf.settings.NDVI_STREAM_BLOCK_MS", 5000),
            patch("django.conf.settings.NDVI_STREAM_BATCH_SIZE", 10),
        ):
            messages = command._read_messages(mock_client)

        assert len(messages) == 1
        assert messages[0][0] == "1-0"
        assert messages[0][1]["job_id"] == "123"
        assert messages[0][2] == 1

    def test_process_message_routes_ndvi_job(self) -> None:
        command = Command()
        command.consumer_name = "test-consumer"
        mock_client = MagicMock()
        payload = {
            "job_id": "123",
            "job_type": "refresh",
            "request_hash": "hash",
            "farm_id": "1",
            "owner_id": "1",
            "engine": "stac",
        }

        with (
            patch("django.conf.settings.NDVI_STREAM_NAME", "test-stream"),
            patch("django.conf.settings.NDVI_STREAM_GROUP", "test-group"),
            patch("django.conf.settings.NDVI_STREAM_MAX_DELIVERIES", 5),
        ):
            with patch(
                "ndvi.management.commands.consume_ndvi_stream.run_ndvi_job"
            ) as mock_task:
                command._process_message(mock_client, "1-0", payload, 1)

        mock_task.delay.assert_called_once_with(123)
        mock_client.xack.assert_called_once()

    def test_process_message_routes_farm_state_coverage(self) -> None:
        command = Command()
        command.consumer_name = "test-consumer"
        mock_client = MagicMock()
        payload = {
            "job_type": "farm_state_coverage",
            "farm_id": "1",
            "target_date": "2024-01-01",
            "threshold": "0.5",
            "engine": "stac",
        }

        with (
            patch("django.conf.settings.NDVI_STREAM_NAME", "test-stream"),
            patch("django.conf.settings.NDVI_STREAM_GROUP", "test-group"),
            patch("django.conf.settings.NDVI_STREAM_MAX_DELIVERIES", 5),
        ):
            with patch(
                "ndvi.management.commands.consume_ndvi_stream."
                "compute_farm_state_coverage"
            ) as mock_task:
                command._process_message(mock_client, "1-0", payload, 1)

        mock_task.delay.assert_called_once_with(
            farm_id=1,
            engine="stac",
            target_date="2024-01-01",
            threshold=0.5,
        )
        mock_client.xack.assert_called_once()

    def test_process_message_moves_to_dlq_on_poison(self) -> None:
        command = Command()
        command.consumer_name = "test-consumer"
        mock_client = MagicMock()
        payload = {"job_id": "123", "job_type": "refresh"}

        with (
            patch("django.conf.settings.NDVI_STREAM_NAME", "test-stream"),
            patch("django.conf.settings.NDVI_STREAM_GROUP", "test-group"),
            patch("django.conf.settings.NDVI_STREAM_DLQ_NAME", "test-dlq"),
            patch("django.conf.settings.NDVI_STREAM_MAX_DELIVERIES", 3),
        ):
            command._process_message(mock_client, "1-0", payload, 5)

        mock_client.xadd.assert_called_once()
        mock_client.xack.assert_called_once()

    def test_trim_streams(self) -> None:
        command = Command()
        command.consumer_name = "test-consumer"
        mock_client = MagicMock()

        with (
            patch("django.conf.settings.NDVI_STREAM_NAME", "test-stream"),
            patch("django.conf.settings.NDVI_STREAM_MAXLEN", 1000),
            patch("django.conf.settings.NDVI_STREAM_DLQ_NAME", "test-dlq"),
            patch("django.conf.settings.NDVI_STREAM_DLQ_MAXLEN", 500),
        ):
            command._trim_streams(mock_client)

        mock_client.xtrim.assert_called()

    def test_extract_pending_age_seconds_with_delivered_time(self) -> None:
        entry = {"time_since_delivered": 5000}
        age = Command._extract_pending_age_seconds(entry)
        assert age == 5.0

    def test_extract_pending_age_seconds_fallback_to_message_id(self) -> None:
        ms_now = int(time.time() * 1000)
        entry: dict[str, str | int] = {"message_id": f"{ms_now - 10000}-0"}
        age = Command._extract_pending_age_seconds(entry)
        assert 9.0 <= age <= 11.0
