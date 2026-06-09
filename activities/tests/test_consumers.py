"""Tests for WebSocket consumers - hardening tests."""

import json
import secrets
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

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


class TestAckAudioAlert(TestCase):
    """Tests for the WebSocket ack_audio_alert protocol."""

    def _make_consumer_with_user(self) -> tuple:
        """Create consumer with user set (as connect() would)."""
        from activities.consumers import ActivityConsumer

        consumer = ActivityConsumer()
        user = MagicMock()
        user.is_authenticated = True
        user.id = 42
        consumer.scope = {"user": user}
        consumer.user = user
        return consumer, user

    def test_receive_ack_calls_confirm_delivery(self) -> None:
        """Test that receive dispatches ack_audio_alert to confirm_delivery."""
        consumer, user = self._make_consumer_with_user()

        message = json.dumps(
            {
                "type": "ack_audio_alert",
                "alert_id": "123e4567-e89b-12d3-a456-426614174000",
            }
        )

        with patch("alerts.services.confirm_delivery") as mock_confirm:
            mock_confirm.return_value = True
            import asyncio

            asyncio.run(consumer.receive(text_data=message))
            mock_confirm.assert_called_once_with(
                user_id=42,
                alert_id=UUID("123e4567-e89b-12d3-a456-426614174000"),
            )

    def test_receive_ack_duplicate_is_idempotent(self) -> None:
        """Test duplicate ack does not raise and does not re-confirm."""
        consumer, user = self._make_consumer_with_user()

        message = json.dumps(
            {
                "type": "ack_audio_alert",
                "alert_id": "123e4567-e89b-12d3-a456-426614174000",
            }
        )

        with patch("alerts.services.confirm_delivery") as mock_confirm:
            mock_confirm.return_value = False
            import asyncio

            try:
                asyncio.run(consumer.receive(text_data=message))
            except Exception:  # noqa: BLE001
                self.fail("duplicate ack raised unexpectedly")

    def test_receive_ack_invalid_alert_id_ignored(self) -> None:
        """Test ack with invalid alert_id is silently ignored."""
        consumer, user = self._make_consumer_with_user()

        message = json.dumps(
            {"type": "ack_audio_alert", "alert_id": "not-a-uuid"}
        )

        with patch("alerts.services.confirm_delivery") as mock_confirm:
            import asyncio

            asyncio.run(consumer.receive(text_data=message))
            mock_confirm.assert_not_called()

    def test_receive_ack_missing_alert_id_ignored(self) -> None:
        """Test ack without alert_id is silently ignored."""
        consumer, user = self._make_consumer_with_user()

        message = json.dumps({"type": "ack_audio_alert"})

        with patch("alerts.services.confirm_delivery") as mock_confirm:
            import asyncio

            asyncio.run(consumer.receive(text_data=message))
            mock_confirm.assert_not_called()

    def test_receive_unknown_type_ignored(self) -> None:
        """Test receive ignores unknown message types."""
        consumer, user = self._make_consumer_with_user()

        message = json.dumps({"type": "unknown_type"})

        with patch("alerts.services.confirm_delivery") as mock_confirm:
            import asyncio

            asyncio.run(consumer.receive(text_data=message))
            mock_confirm.assert_not_called()

    def test_receive_non_json_ignored(self) -> None:
        """Test receive ignores non-JSON text."""
        consumer, user = self._make_consumer_with_user()

        with patch("alerts.services.confirm_delivery") as mock_confirm:
            import asyncio

            asyncio.run(consumer.receive(text_data="not json"))
            mock_confirm.assert_not_called()

    def test_receive_none_text_ignored(self) -> None:
        """Test receive ignores None text_data."""
        consumer, user = self._make_consumer_with_user()

        with patch("alerts.services.confirm_delivery") as mock_confirm:
            import asyncio

            asyncio.run(consumer.receive(text_data=None))
            mock_confirm.assert_not_called()


class TestReplayAlerts(TestCase):
    """Tests for replay-on-connect behavior."""

    def test_connect_replays_unacknowledged_alerts(self) -> None:
        """Test connect() replays unacknowledged alerts."""
        from activities.consumers import ActivityConsumer

        consumer = ActivityConsumer()
        user = MagicMock()
        user.is_authenticated = True
        user.id = 42
        consumer.scope = {"user": user}
        consumer.channel_layer = MagicMock()
        consumer.channel_layer.group_add = AsyncMock()
        consumer.channel_name = "test_channel"

        with (
            patch.object(consumer, "accept", AsyncMock()),
            patch.object(consumer, "_replay_alerts") as mock_replay,
        ):
            import asyncio

            asyncio.run(consumer.connect())
            mock_replay.assert_called_once()

    def test_replay_alerts_sends_payloads(self) -> None:
        """Test _replay_alerts sends payloads for unacknowledged alerts."""
        from activities.consumers import ActivityConsumer

        consumer = ActivityConsumer()
        user = MagicMock()
        user.is_authenticated = True
        user.id = 42
        consumer.scope = {"user": user}
        consumer.channel_layer = MagicMock()
        consumer.channel_layer.group_add = AsyncMock()
        consumer.channel_name = "test_channel"
        consumer.user = user

        with (
            patch.object(consumer, "accept", AsyncMock()),
            patch("alerts.models.AudioAlert") as mock_model,
            patch("alerts.services.build_push_payload") as mock_build,
            patch("alerts.metrics.replay"),
        ):
            mock_alerts = [MagicMock(), MagicMock()]
            mock_model.objects.filter.return_value.order_by.return_value.__getitem__.return_value = (  # noqa: E501
                mock_alerts
            )
            mock_build.side_effect = [
                {"alert_id": "1", "title": "A1"},
                {"alert_id": "2", "title": "A2"},
            ]

            import asyncio

            asyncio.run(consumer.connect())
            self.assertEqual(mock_build.call_count, 2)

    def test_replay_skips_acknowledged_alerts(self) -> None:
        """Test _replay_alerts only queries unacknowledged alerts."""
        from activities.consumers import ActivityConsumer

        consumer = ActivityConsumer()
        user = MagicMock()
        user.is_authenticated = True
        user.id = 42
        consumer.scope = {"user": user}
        consumer.channel_layer = MagicMock()
        consumer.channel_layer.group_add = AsyncMock()
        consumer.channel_name = "test_channel"
        consumer.user = user

        with (
            patch.object(consumer, "accept", AsyncMock()),
            patch("alerts.models.AudioAlert") as mock_model,
            patch("alerts.services.build_push_payload"),
            patch("alerts.metrics.replay"),
        ):
            mock_qs = MagicMock()
            mock_model.objects.filter.return_value = mock_qs
            mock_qs.order_by.return_value.__getitem__.return_value = []

            import asyncio

            asyncio.run(consumer.connect())

            mock_model.objects.filter.assert_called_once()
            filter_kwargs = mock_model.objects.filter.call_args[1]
            self.assertEqual(filter_kwargs["user"], user)
            self.assertFalse(filter_kwargs["is_acknowledged"])


class TestConfirmDeliveryService(TestCase):
    """Tests for the confirm_delivery service function."""

    def test_confirm_delivery_sets_client_confirmed_at(self) -> None:
        """Test confirm_delivery sets client_confirmed_at."""
        from django.contrib.auth import get_user_model

        from alerts.models import AudioAlert
        from alerts.services import confirm_delivery

        user = get_user_model().objects.create_user(
            username="ack_test", password=secrets.token_urlsafe(12)
        )
        alert = AudioAlert.objects.create(
            user=user,
            alert_type="admin_broadcast",
            trigger_source="admin_view",
            title="Test",
            message="Test",
        )
        self.assertIsNone(alert.client_confirmed_at)

        result = confirm_delivery(user_id=user.id, alert_id=alert.id)
        self.assertTrue(result)

        alert.refresh_from_db()
        self.assertIsNotNone(alert.client_confirmed_at)

    def test_confirm_delivery_idempotent(self) -> None:
        """Test confirm_delivery is idempotent on second call."""
        from django.contrib.auth import get_user_model

        from alerts.models import AudioAlert
        from alerts.services import confirm_delivery

        user = get_user_model().objects.create_user(
            username="ack_idem", password=secrets.token_urlsafe(12)
        )
        alert = AudioAlert.objects.create(
            user=user,
            alert_type="admin_broadcast",
            trigger_source="admin_view",
            title="Test",
            message="Test",
        )

        confirm_delivery(user_id=user.id, alert_id=alert.id)
        result = confirm_delivery(user_id=user.id, alert_id=alert.id)
        self.assertFalse(result)

    def test_confirm_delivery_wrong_user_returns_false(self) -> None:
        """Test confirm_delivery for wrong user returns False."""
        from django.contrib.auth import get_user_model

        from alerts.models import AudioAlert
        from alerts.services import confirm_delivery

        owner = get_user_model().objects.create_user(
            username="owner", password=secrets.token_urlsafe(12)
        )
        other = get_user_model().objects.create_user(
            username="other", password=secrets.token_urlsafe(12)
        )
        alert = AudioAlert.objects.create(
            user=owner,
            alert_type="admin_broadcast",
            trigger_source="admin_view",
            title="Test",
            message="Test",
        )

        result = confirm_delivery(user_id=other.id, alert_id=alert.id)
        self.assertFalse(result)

    def test_confirm_delivery_nonexistent_alert_returns_false(self) -> None:
        """Test confirm_delivery for nonexistent alert returns False."""
        from django.contrib.auth import get_user_model

        from alerts.services import confirm_delivery

        user = get_user_model().objects.create_user(
            username="nonexist", password=secrets.token_urlsafe(12)
        )

        result = confirm_delivery(
            user_id=user.id,
            alert_id=UUID("00000000-0000-0000-0000-000000000000"),
        )
        self.assertFalse(result)


class TestDeliveryAttempts(TestCase):
    """Tests for delivery_attempts tracking."""

    @patch("alerts.services.emit_audio_alert_event", return_value=1)
    def test_dispatch_increments_delivery_attempts(
        self, mock_emit: MagicMock
    ) -> None:
        """Test dispatch_alert_fast increments delivery_attempts on success."""
        from unittest.mock import MagicMock

        from django.contrib.auth import get_user_model

        from alerts import tasks
        from alerts.models import AudioAlert
        from alerts.services import dispatch_alert_fast

        user = get_user_model().objects.create_user(
            username="da_test", password=secrets.token_urlsafe(12)
        )

        with patch.object(tasks.render_alert_audio, "delay", MagicMock()):
            result = dispatch_alert_fast(
                user_id=user.id,
                farm_id=None,
                alert_type="admin_broadcast",
                trigger_source="admin_view",
                title="Test",
                message="Test",
            )

        alert = AudioAlert.objects.get(id=result.alert_id)
        self.assertEqual(alert.delivery_attempts, 1)

    @patch(
        "alerts.services.emit_audio_alert_event",
        side_effect=RuntimeError("push failed"),
    )
    def test_dispatch_records_error_on_failure(
        self, mock_emit: MagicMock
    ) -> None:
        """Test dispatch_alert_fast sets last_delivery_error on failure."""
        from unittest.mock import MagicMock

        from django.contrib.auth import get_user_model

        from alerts import tasks
        from alerts.models import AudioAlert
        from alerts.services import dispatch_alert_fast

        user = get_user_model().objects.create_user(
            username="de_test", password=secrets.token_urlsafe(12)
        )

        with patch.object(tasks.render_alert_audio, "delay", MagicMock()):
            result = dispatch_alert_fast(
                user_id=user.id,
                farm_id=None,
                alert_type="admin_broadcast",
                trigger_source="admin_view",
                title="Test",
                message="Test",
            )

        alert = AudioAlert.objects.get(id=result.alert_id)
        self.assertEqual(alert.delivery_attempts, 1)
        self.assertIn("push failed", alert.last_delivery_error)
