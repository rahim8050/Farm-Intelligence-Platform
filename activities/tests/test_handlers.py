"""Tests for activity handlers."""

import secrets
import time
from typing import Any
from unittest.mock import MagicMock, patch

from django.test import TestCase

from activities.handlers.base import HandlerResult
from activities.handlers.fertilizer import FertilizerHandler
from activities.handlers.irrigation import IrrigationHandler
from activities.handlers.ndvi_trigger import (
    ALLOWED_ACTIONS,
    DEFAULT_STATE_ACTIONS,
    FarmState,
    NdviTriggerHandler,
    RecommendedAction,
)
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


class TestNdviTriggerHandler(TestCase):
    """Test NdviTriggerHandler."""

    def test_handler_type(self) -> None:
        """Test handler has correct type."""
        handler = NdviTriggerHandler()
        self.assertEqual(handler.type, "ndvi_trigger")

    def test_get_handler_ndvi_trigger(self) -> None:
        """Test get_handler returns NdviTriggerHandler."""
        handler = get_handler("ndvi_trigger")
        self.assertIsInstance(handler, NdviTriggerHandler)

    def test_execute_missing_farm_id(self) -> None:
        """Test execute fails gracefully when farm_id is missing."""
        handler = NdviTriggerHandler()
        activity = MagicMock()
        activity.metadata = {}
        activity.farm_id = None

        result = handler.execute(activity)

        self.assertIsInstance(result, HandlerResult)
        self.assertFalse(result.success)
        self.assertIn("farm_id", result.message.lower())

    def test_execute_with_farm_id_from_metadata(self) -> None:
        """Test execute uses farm_id from metadata."""
        from ndvi.farm_state import FarmStateResult

        handler = NdviTriggerHandler()
        activity = MagicMock()
        activity.id = 1
        activity.metadata = {"farm_id": 123}
        activity.farm_id = None

        mock_result = FarmStateResult(
            farm_id=123,
            state="growth",
            mean_ndvi=0.5,
            max_ndvi=0.7,
            coverage_pct=60.0,
            trend=0.01,
            interpretation="Good growth",
            action="Continue monitoring",
        )

        with patch("django.core.cache.cache.get", return_value=None):
            with patch("django.core.cache.cache.set"):
                with patch("django.core.cache.cache.add", return_value=True):
                    with patch(
                        "farms.models.Farm.objects.get",
                        return_value=MagicMock(),
                    ):
                        with patch(
                            "ndvi.farm_state.build_farm_state",
                            return_value=mock_result,
                        ):
                            result = handler.execute(activity)

        self.assertIsInstance(result, HandlerResult)
        self.assertTrue(result.success)
        self.assertEqual(result.metadata["farm_id"], 123)
        self.assertEqual(result.metadata["state"], "growth")

    def test_execute_with_custom_action_mapping(self) -> None:
        """Test execute uses custom action_on_state mapping."""
        from ndvi.farm_state import FarmStateResult

        handler = NdviTriggerHandler()
        activity = MagicMock()
        activity.id = 2
        activity.metadata = {
            "farm_id": 456,
            "action_on_state": {"decline": ["irrigation"]},
        }
        activity.farm_id = None

        mock_result = FarmStateResult(
            farm_id=456,
            state="decline",
            mean_ndvi=0.2,
            max_ndvi=0.3,
            coverage_pct=20.0,
            trend=-0.02,
            interpretation="NDVI declining",
            action="Check irrigation",
        )

        with patch("django.core.cache.cache.get", return_value=None):
            with patch("django.core.cache.cache.set"):
                with patch("django.core.cache.cache.add", return_value=True):
                    with patch(
                        "farms.models.Farm.objects.get",
                        return_value=MagicMock(),
                    ):
                        with patch(
                            "ndvi.farm_state.build_farm_state",
                            return_value=mock_result,
                        ):
                            result = handler.execute(activity)

        self.assertTrue(result.success)
        expected_actions = ["irrigation"]
        self.assertEqual(
            result.metadata["recommended_actions"], expected_actions
        )

    def test_state_action_mapping_defaults(self) -> None:
        """Test default state action mapping is used."""
        self.assertIn("establishment", DEFAULT_STATE_ACTIONS)
        self.assertIn("decline", DEFAULT_STATE_ACTIONS)
        self.assertIsInstance(DEFAULT_STATE_ACTIONS["establishment"], list)

    def test_execute_handles_farm_not_found(self) -> None:
        """Test execute handles Farm.DoesNotExist gracefully."""
        from farms.models import Farm

        handler = NdviTriggerHandler()
        activity = MagicMock()
        activity.id = 3
        activity.metadata = {"farm_id": 999}
        activity.farm_id = None

        with patch("django.core.cache.cache.get", return_value=None):
            with patch("django.core.cache.cache.set"):
                with patch("django.core.cache.cache.add", return_value=True):
                    with patch(
                        "farms.models.Farm.objects.get",
                        side_effect=Farm.DoesNotExist(),
                    ):
                        result = handler.execute(activity)

        self.assertFalse(result.success)
        self.assertIn("farm_not_found", result.metadata.get("error", ""))

    def test_execute_handles_farm_state_error(self) -> None:
        """Test execute handles farm state errors gracefully."""
        handler = NdviTriggerHandler()
        activity = MagicMock()
        activity.id = 4
        activity.metadata = {"farm_id": 999}
        activity.farm_id = None

        with patch("django.core.cache.cache.get", return_value=None):
            with patch("django.core.cache.cache.set"):
                with patch("django.core.cache.cache.add", return_value=True):
                    with patch(
                        "farms.models.Farm.objects.get",
                        return_value=MagicMock(),
                    ):
                        with patch(
                            "ndvi.farm_state.build_farm_state",
                            side_effect=Exception("Service error"),
                        ):
                            result = handler.execute(activity)

        self.assertFalse(result.success)
        self.assertIn("farm_state_error", result.metadata.get("error", ""))

    def test_validate_actions_allowlist(self) -> None:
        """Test action validation against allowlist."""
        handler = NdviTriggerHandler()

        valid_actions = ["fertilizer", "irrigation"]
        result = handler._validate_actions(valid_actions)
        self.assertEqual(result, valid_actions)

        invalid_actions = ["fertilizer", "arbitrary_action"]
        result = handler._validate_actions(invalid_actions)
        self.assertEqual(result, ["fertilizer"])

    def test_validate_actions_rejects_all_invalid(self) -> None:
        """Test validation returns empty list when all actions invalid."""
        handler = NdviTriggerHandler()

        result = handler._validate_actions(["invalid_action", "another_one"])
        self.assertEqual(result, [])

    def test_execute_invalid_metadata_schema(self) -> None:
        """Test execute rejects invalid metadata schema."""
        handler = NdviTriggerHandler()
        activity = MagicMock()
        activity.metadata = {"action_on_state": "not_a_dict"}
        activity.farm_id = None

        result = handler.execute(activity)

        self.assertFalse(result.success)
        self.assertEqual(result.metadata.get("error"), "invalid_metadata")

    def test_execute_invalid_action_on_state_not_list(self) -> None:
        """Test execute rejects action_on_state with non-list values."""
        handler = NdviTriggerHandler()
        activity = MagicMock()
        activity.metadata = {"action_on_state": {"decline": "irrigation"}}
        activity.farm_id = None

        result = handler.execute(activity)

        self.assertFalse(result.success)
        self.assertEqual(result.metadata.get("error"), "invalid_metadata")

    def test_duplicate_execution_prevented(self) -> None:
        """Test duplicate execution is prevented."""
        handler = NdviTriggerHandler()
        activity = MagicMock()
        activity.id = 5
        activity.metadata = {"farm_id": 789}
        activity.farm_id = None

        def cache_get_side_effect(key: str) -> str | None:
            if key.startswith("ndvi_trigger:idempotency:"):
                return '{"timestamp": 1234567890}'
            if key.startswith("ndvi_trigger:prev_state:"):
                return "growth"
            return None

        with patch(
            "django.core.cache.cache.get", side_effect=cache_get_side_effect
        ):
            with patch("django.core.cache.cache.set"):
                with patch("django.core.cache.cache.add", return_value=False):
                    result = handler.execute(activity)

        self.assertFalse(result.success)
        self.assertEqual(result.metadata.get("error"), "duplicate_execution")

    def test_no_transition_when_state_unchanged(self) -> None:
        """Test no action triggered when state unchanged."""
        from ndvi.farm_state import FarmStateResult

        handler = NdviTriggerHandler()
        activity = MagicMock()
        activity.id = 6
        activity.metadata = {"farm_id": 101}
        activity.farm_id = None

        mock_result = FarmStateResult(
            farm_id=101,
            state="establishment",
            mean_ndvi=0.3,
            max_ndvi=0.4,
            coverage_pct=30.0,
            trend=0.01,
            interpretation="Establishment phase",
            action="Apply fertilizer",
        )

        with patch(
            "django.core.cache.cache.get", return_value="establishment"
        ):
            with patch("django.core.cache.cache.set"):
                with patch("django.core.cache.cache.add", return_value=True):
                    with patch(
                        "farms.models.Farm.objects.get",
                        return_value=MagicMock(),
                    ):
                        with patch(
                            "ndvi.farm_state.build_farm_state",
                            return_value=mock_result,
                        ):
                            result = handler.execute(activity)

        self.assertTrue(result.success)
        self.assertTrue(result.metadata.get("no_transition"))
        self.assertEqual(result.metadata["recommended_actions"], [])

    def test_transition_triggers_actions(self) -> None:
        """Test state transition triggers recommended actions."""
        from ndvi.farm_state import FarmStateResult

        handler = NdviTriggerHandler()
        activity = MagicMock()
        activity.id = 7
        activity.metadata = {"farm_id": 102}
        activity.farm_id = None

        mock_result = FarmStateResult(
            farm_id=102,
            state="decline",
            mean_ndvi=0.2,
            max_ndvi=0.3,
            coverage_pct=20.0,
            trend=-0.02,
            interpretation="Decline phase",
            action="Check irrigation",
        )

        with patch("django.core.cache.cache.get", return_value="growth"):
            with patch("django.core.cache.cache.set"):
                with patch("django.core.cache.cache.add", return_value=True):
                    with patch(
                        "farms.models.Farm.objects.get",
                        return_value=MagicMock(),
                    ):
                        with patch(
                            "ndvi.farm_state.build_farm_state",
                            return_value=mock_result,
                        ):
                            result = handler.execute(activity)

        self.assertTrue(result.success)
        self.assertFalse(result.metadata.get("no_transition"))
        self.assertIn("irrigation", result.metadata["recommended_actions"])
        self.assertIn("vaccination", result.metadata["recommended_actions"])

    def test_close_old_connections_called_on_success(self) -> None:
        """Test DB connections closed after successful execution."""
        handler = NdviTriggerHandler()
        activity = MagicMock()
        activity.id = 8
        activity.metadata = {}
        activity.farm_id = None

        with patch(
            "activities.handlers.ndvi_trigger.close_old_connections"
        ) as mock_close:
            handler.execute(activity)
            self.assertTrue(mock_close.called)

    def test_close_old_connections_called_on_error(self) -> None:
        """Test DB connections closed even on error."""
        from farms.models import Farm

        handler = NdviTriggerHandler()
        activity = MagicMock()
        activity.id = 9
        activity.metadata = {"farm_id": 999}
        activity.farm_id = None

        with patch(
            "activities.handlers.ndvi_trigger.close_old_connections"
        ) as mock_close:
            with patch("django.core.cache.cache.get", return_value=None):
                with patch("django.core.cache.cache.set"):
                    with patch(
                        "django.core.cache.cache.add", return_value=True
                    ):
                        with patch(
                            "farms.models.Farm.objects.get",
                            side_effect=Farm.DoesNotExist(),
                        ):
                            handler.execute(activity)
            self.assertTrue(mock_close.called)

    def test_enum_values(self) -> None:
        """Test FarmState and RecommendedAction enum values."""
        self.assertEqual(FarmState.ESTABLISHMENT.value, "establishment")
        self.assertEqual(FarmState.FULL_CANOPY.value, "full_canopy")
        self.assertEqual(FarmState.DECLINE.value, "decline")

        self.assertEqual(RecommendedAction.FERTILIZER.value, "fertilizer")
        self.assertEqual(RecommendedAction.IRRIGATION.value, "irrigation")
        self.assertEqual(RecommendedAction.VACCINATION.value, "vaccination")

    def test_allowed_actions_constant(self) -> None:
        """Test ALLOWED_ACTIONS contains expected values."""
        self.assertIn("fertilizer", ALLOWED_ACTIONS)
        self.assertIn("irrigation", ALLOWED_ACTIONS)
        self.assertIn("vaccination", ALLOWED_ACTIONS)
        self.assertEqual(len(ALLOWED_ACTIONS), 3)
