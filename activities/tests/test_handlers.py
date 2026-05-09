"""Tests for activity handlers."""

import secrets
import time
from typing import Any
from unittest.mock import MagicMock

from django.test import TestCase

from activities.handlers.base import HandlerResult
from activities.handlers.fertilizer import FertilizerHandler
from activities.handlers.irrigation import IrrigationHandler
from activities.handlers.registry import get_handler, register_handler
from activities.handlers.vaccination import VaccinationHandler

TEST_PASSWORD = secrets.token_urlsafe(16)


class TestHandlerResult(TestCase):
    """Test HandlerResult dataclass."""

    def test_handler_result_creation(self) -> None:
        """Test HandlerResult with all fields."""
        result = HandlerResult(
            success=True,
            message="Test message",
            metadata={"key": "value"},
        )
        self.assertTrue(result.success)
        self.assertEqual(result.message, "Test message")
        self.assertEqual(result.metadata, {"key": "value"})

    def test_handler_result_defaults(self) -> None:
        """Test HandlerResult defaults."""
        result = HandlerResult(success=True, message="Test")
        self.assertIsNone(result.metadata)


class TestVaccinationHandler(TestCase):
    """Test VaccinationHandler."""

    def test_handler_type(self) -> None:
        """Test handler has correct type."""
        handler = VaccinationHandler()
        self.assertEqual(handler.type, "vaccination")

    def test_execute_returns_handler_result(self) -> None:
        """Test execute returns HandlerResult."""
        handler = VaccinationHandler()
        activity = MagicMock()
        activity.metadata = {"cattle_id": "C123"}

        result = handler.execute(activity)

        self.assertIsInstance(result, HandlerResult)
        self.assertTrue(result.success)
        self.assertEqual(result.message, "Vaccination completed")
        self.assertEqual(result.metadata["cattle_id"], "C123")

    def test_execute_missing_metadata(self) -> None:
        """Test execute with missing cattle_id."""
        handler = VaccinationHandler()
        activity = MagicMock()
        activity.metadata = {}

        result = handler.execute(activity)

        self.assertIsInstance(result, HandlerResult)
        self.assertTrue(result.success)
        self.assertEqual(result.metadata["cattle_id"], "unknown")


class TestFertilizerHandler(TestCase):
    """Test FertilizerHandler."""

    def test_handler_type(self) -> None:
        """Test handler has correct type."""
        handler = FertilizerHandler()
        self.assertEqual(handler.type, "fertilizer")

    def test_execute_returns_handler_result(self) -> None:
        """Test execute returns HandlerResult."""
        handler = FertilizerHandler()
        activity = MagicMock()
        activity.metadata = {"amount_kg": 50, "fertilizer_type": "urea"}

        result = handler.execute(activity)

        self.assertIsInstance(result, HandlerResult)
        self.assertTrue(result.success)
        self.assertEqual(result.message, "Fertilizer applied")
        self.assertEqual(result.metadata["amount_kg"], 50)
        self.assertEqual(result.metadata["fertilizer_type"], "urea")

    def test_execute_missing_metadata(self) -> None:
        """Test execute with missing metadata."""
        handler = FertilizerHandler()
        activity = MagicMock()
        activity.metadata = {}

        result = handler.execute(activity)

        self.assertIsInstance(result, HandlerResult)
        self.assertEqual(result.metadata["amount_kg"], 0)
        self.assertEqual(result.metadata["fertilizer_type"], "unknown")


class TestIrrigationHandler(TestCase):
    """Test IrrigationHandler."""

    def test_handler_type(self) -> None:
        """Test handler has correct type."""
        handler = IrrigationHandler()
        self.assertEqual(handler.type, "irrigation")

    def test_execute_returns_handler_result(self) -> None:
        """Test execute returns HandlerResult."""
        handler = IrrigationHandler()
        activity = MagicMock()
        activity.metadata = {"duration_min": 30}

        result = handler.execute(activity)

        self.assertIsInstance(result, HandlerResult)
        self.assertTrue(result.success)
        self.assertEqual(result.message, "Irrigation completed")
        self.assertEqual(result.metadata["duration_min"], 30)

    def test_execute_missing_metadata(self) -> None:
        """Test execute with missing metadata."""
        handler = IrrigationHandler()
        activity = MagicMock()
        activity.metadata = {}

        result = handler.execute(activity)

        self.assertIsInstance(result, HandlerResult)
        self.assertEqual(result.metadata["duration_min"], 15)


class TestHandlerRegistry(TestCase):
    """Test handler registry."""

    def test_get_handler_vaccination(self) -> None:
        """Test get_handler returns VaccinationHandler."""
        handler = get_handler("vaccination")
        self.assertIsInstance(handler, VaccinationHandler)

    def test_get_handler_fertilizer(self) -> None:
        """Test get_handler returns FertilizerHandler."""
        handler = get_handler("fertilizer")
        self.assertIsInstance(handler, FertilizerHandler)

    def test_get_handler_irrigation(self) -> None:
        """Test get_handler returns IrrigationHandler."""
        handler = get_handler("irrigation")
        self.assertIsInstance(handler, IrrigationHandler)

    def test_get_handler_unknown_returns_default(self) -> None:
        """Test get_handler returns DefaultHandler for unknown type."""
        from activities.handlers.registry import DefaultHandler

        handler = get_handler("unknown_type")
        self.assertIsInstance(handler, DefaultHandler)

    def test_register_handler_function(self) -> None:
        """Test register_handler adds to registry."""
        from activities.handlers.base import ActivityHandler

        class TestHandler(ActivityHandler):
            type = "test_handler"

            def execute(self, activity: Any) -> str:
                return "test"

        class_name = "custom_test_handler_" + str(time.time())
        TestHandler.type = class_name

        register_handler(TestHandler)

        handler = get_handler(class_name)
        self.assertIsInstance(handler, TestHandler)
