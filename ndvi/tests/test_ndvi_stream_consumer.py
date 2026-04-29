"""Tests for NDVI stream consumer observability hooks."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from ndvi.management.commands.consume_ndvi_stream import Command


class TestStreamConsumerMetrics:
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
