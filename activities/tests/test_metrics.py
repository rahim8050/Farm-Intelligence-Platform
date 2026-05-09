"""Tests for activity metrics."""

from django.test import TestCase

from activities.metrics import (
    activities_active,
    activities_dispatched,
    activity_duration_seconds,
)


class TestActivityMetrics(TestCase):
    """Test activity Prometheus metrics."""

    def test_activities_dispatched_counter_exists(self) -> None:
        """Test activities_dispatched Counter is defined."""
        self.assertIsNotNone(activities_dispatched)

    def test_activities_dispatched_labels_function(self) -> None:
        """Test activities_dispatched labels method works."""
        labels = activities_dispatched.labels(
            type="vaccination", status="success"
        )
        labels.inc()

    def test_activity_duration_seconds_histogram_exists(self) -> None:
        """Test activity_duration_seconds Histogram is defined."""
        self.assertIsNotNone(activity_duration_seconds)

    def test_activity_duration_histogram_observe(self) -> None:
        """Test activity_duration_seconds can observe values."""
        labels = activity_duration_seconds.labels(type="fertilizer")
        labels.observe(1.5)

    def test_activities_active_gauge_exists(self) -> None:
        """Test activities_active Gauge is defined."""
        self.assertIsNotNone(activities_active)

    def test_activities_active_gauge_set(self) -> None:
        """Test activities_active can be set."""
        labels = activities_active.labels(type="irrigation", status="running")
        labels.set(5)

    def test_all_metrics_callable(self) -> None:
        """Test all metrics are callable with labels."""
        activities_dispatched.labels(
            type="vaccination", status="success"
        ).inc()
        activity_duration_seconds.labels(type="vaccination").observe(0.5)
        activities_active.labels(type="vaccination", status="running").set(2)

    def test_metric_docstrings_exist(self) -> None:
        """Test metrics have documentation."""
        self.assertIsNotNone(activities_dispatched._documentation)
        self.assertIsNotNone(activity_duration_seconds._documentation)
        self.assertIsNotNone(activities_active._documentation)
