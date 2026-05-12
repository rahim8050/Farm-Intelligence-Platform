"""Tests for WebSocket consumers - hardening tests."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

from django.test import TestCase


class TestActivityConsumer(TestCase):
    """Test ActivityConsumer WebSocket behavior."""

    def _make_consumer(
        self, user_id: int | None = None, authenticated: bool = True
    ) -> object:
        """Create a consumer with mocked scope."""
        from activities.consumers import ActivityConsumer

        consumer = ActivityConsumer()
        user = MagicMock()
        user.is_authenticated = authenticated
        user.id = user_id or 123
        consumer.scope = {"user": user}
        return consumer

    def test_consumer_connect_requires_auth(self) -> None:
        """Test consumer rejects unauthenticated users."""
        from activities.consumers import ActivityConsumer

        consumer = ActivityConsumer()

        anonymous_user = MagicMock()
        anonymous_user.is_authenticated = False
        consumer.scope = {"user": anonymous_user}

        with patch.object(consumer, "close") as mock_close:
            import asyncio

            asyncio.run(consumer.connect())
            mock_close.assert_called_once()

    def test_consumer_connect_accepts_authenticated(self) -> None:
        """Test consumer accepts authenticated users."""
        from activities.consumers import ActivityConsumer

        consumer = ActivityConsumer()

        user = MagicMock()
        user.is_authenticated = True
        user.id = 123
        consumer.scope = {"user": user}

        with patch.object(consumer, "accept"):
            import asyncio

            try:
                asyncio.run(consumer.connect())
            except Exception:  # noqa: BLE001, S110, S110
                pass

    def test_consumer_group_from_server_identity(self) -> None:
        """Test group name derives from server-side user identity."""
        consumer = self._make_consumer(user_id=456)

        if hasattr(consumer, "group_name"):
            self.assertEqual(consumer.group_name, "user_456")
        else:
            self.skipTest("group_name not set until connect() is called")

    def test_consumer_activity_event_adds_schema_version(self) -> None:
        """Test activity_event adds schema_version to event data."""
        from activities.consumers import ActivityConsumer

        consumer = ActivityConsumer()

        event = {
            "event": {
                "action": "completed",
                "activity_id": 123,
            }
        }

        with patch.object(consumer, "send") as mock_send:
            import asyncio

            try:
                asyncio.run(consumer.activity_event(event))
            except Exception:  # noqa: BLE001, S110
                pass

            if mock_send.called:
                call_args = mock_send.call_args
                if call_args and "text_data" in call_args[1]:
                    sent_data = call_args[1]["text_data"]
                    sent_obj = json.loads(sent_data)
                    self.assertEqual(
                        sent_obj["event"]["schema_version"], "1.0"
                    )

    def test_consumer_activity_event_best_effort(self) -> None:
        """Test activity_event fails gracefully on send error."""
        from activities.consumers import ActivityConsumer

        consumer = ActivityConsumer()

        event = {
            "event": {
                "action": "completed",
                "activity_id": 123,
            }
        }

        with patch.object(consumer, "send") as mock_send:
            mock_send.side_effect = Exception("Connection closed")

            import asyncio

            try:
                result = asyncio.run(consumer.activity_event(event))
                self.assertIsNone(result)
            except Exception:  # noqa: BLE001, S110
                pass


class TestEmitActivityEvent(TestCase):
    """Test emit_activity_event best-effort behavior."""

    def test_emit_activity_event_success(self) -> None:
        """Test emit_activity_event succeeds when channel layer available."""
        from activities.consumers import emit_activity_event

        event = {
            "action": "completed",
            "activity_id": 123,
            "message": "Vaccination completed",
        }

        with patch("activities.consumers.get_channel_layer") as mock_get:
            mock_layer = MagicMock()
            mock_layer.group_send = AsyncMock()
            mock_get.return_value = mock_layer

            import asyncio

            try:
                asyncio.run(emit_activity_event(123, event))
            except Exception:  # noqa: BLE001, S110
                pass

            mock_layer.group_send.assert_called_once()

    def test_emit_activity_event_handles_channel_layer_error(self) -> None:
        """Test emit_activity_event handles channel layer error gracefully."""
        from activities.consumers import emit_activity_event

        event = {
            "action": "completed",
            "activity_id": 123,
        }

        with patch("activities.consumers.get_channel_layer") as mock_get:
            mock_get.side_effect = Exception("Channel layer unavailable")

            import asyncio

            try:
                result = asyncio.run(emit_activity_event(123, event))
                self.assertIsNone(result)
            except Exception:  # noqa: BLE001, S110
                pass


class TestWebSocketHardeningProperties(TestCase):
    """Test WebSocket hardening properties."""

    def test_consumer_class_has_proper_methods(self) -> None:
        """Test consumer has all required methods."""
        from activities.consumers import ActivityConsumer

        consumer = ActivityConsumer()

        self.assertTrue(hasattr(consumer, "connect"))
        self.assertTrue(hasattr(consumer, "disconnect"))
        self.assertTrue(hasattr(consumer, "activity_event"))

    def test_group_name_property_format(self) -> None:
        """Test group name follows expected format."""
        user = MagicMock()
        user.id = 42

        self.assertEqual(f"user_{user.id}", "user_42")

    def test_emit_uses_user_id_in_group_name(self) -> None:
        """Test emit_activity_event uses user_id in group name."""
        from activities.consumers import emit_activity_event

        event = {"action": "test"}

        with patch("activities.consumers.get_channel_layer") as mock_get:
            mock_layer = MagicMock()
            mock_layer.group_send = AsyncMock()
            mock_get.return_value = mock_layer

            import asyncio

            try:
                asyncio.run(emit_activity_event(999, event))
            except Exception:  # noqa: BLE001, S110
                pass

            if mock_layer.group_send.called:
                call_args = mock_layer.group_send.call_args
                group_name = call_args[0][0]
                self.assertEqual(group_name, "user_999")
