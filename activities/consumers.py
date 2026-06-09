"""WebSocket consumers for activity notifications.

Provides real-time activity status updates via Django Channels.

Auth: Uses AuthMiddlewareStack for user authentication.
WebSocket URL: ws://domain/ws/activities/

Semantics:
- PostgreSQL = authoritative source of truth
- Redis/WebSocket = best effort only (failures must not affect correctness)

Backpressure Strategy:
- If channel layer is overloaded, emits will fail gracefully
- Clients should reconnect on disconnect
- Server-side rate limiting can be added via throttle scopes
- No guaranteed delivery - client should poll REST API for state

Acknowledgment Protocol:
- Clients send ``{"type": "ack_audio_alert", "alert_id": "<uuid>"}`` after
  receiving and decoding an ``audio_alert`` payload. The server records
  the ``client_confirmed_at`` timestamp on the corresponding
  ``AudioAlert`` row and the system can distinguish "server pushed" from
  "client confirmed receipt".
- On reconnect, any unacknowledged alerts are automatically replayed,
  giving recovering clients a chance to catch up on missed alerts.
- Both the ack and the replay are best-effort; failures are logged and
  counted but never propagated.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.layers import get_channel_layer

from activities.metrics import (
    activities_websocket_events,
    activities_websocket_failures,
)

logger = logging.getLogger("activities")


class ActivityConsumer(AsyncWebsocketConsumer):
    """WebSocket consumer for activity events.

    Auth: AuthMiddlewareStack (authenticated users only)
    WebSocket URL: ws://domain/ws/activities/

    Events (server -> client):
        - activity_event: Sent when activity status changes
        - audio_alert: Sent when an audio alert is dispatched

    Messages (client -> server):
        - ack_audio_alert: Client confirms receipt of an audio alert
    """

    async def connect(self) -> None:
        """Handle WebSocket connection."""
        self.user = self.scope["user"]

        if not self.user.is_authenticated:
            await self.close()
            return

        # Group assignment derives ONLY from server-side authenticated identity
        # Client cannot spoof group membership
        self.group_name = f"user_{self.user.id}"
        await self.channel_layer.group_add(
            self.group_name,
            self.channel_name,
        )

        await self.accept()

        # Replay any unacknowledged alerts on reconnect
        await self._replay_alerts()

    async def disconnect(self, close_code: Any) -> None:
        """Handle WebSocket disconnection."""
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(
                self.group_name,
                self.channel_name,
            )

    async def receive(
        self, text_data: str | None = None, bytes_data: bytes | None = None
    ) -> None:  # noqa: ARG002
        """Handle an incoming WebSocket message.

        Supports:
            - ``ack_audio_alert``: client confirms delivery of an audio alert.
        """
        if not text_data:
            return
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            return
        msg_type = data.get("type")
        if msg_type == "ack_audio_alert":
            await self._handle_ack(data)

    async def _handle_ack(self, data: dict[str, Any]) -> None:
        """Process a client ack for an audio alert.

        Expected payload: ``{"type": "ack_audio_alert", "alert_id": "<uuid>"}``
        """
        raw_id = data.get("alert_id")
        if not raw_id:
            return
        try:
            alert_id = UUID(raw_id)
        except (ValueError, TypeError):
            logger.warning(
                "websocket_ack_invalid_id user=%s alert_id=%s",
                self.user.id,
                raw_id,
            )
            return
        from alerts.services import confirm_delivery

        ok = await sync_to_async(confirm_delivery)(
            user_id=self.user.id, alert_id=alert_id
        )
        if ok:
            activities_websocket_events.labels(status="acked").inc()
        else:
            activities_websocket_events.labels(status="ack_duplicate").inc()

    async def activity_event(self, event: dict) -> None:
        """Handle activity events from worker.

        Note: This is best-effort. WebSocket failures must not
        affect activity execution correctness.

        Args:
            event: Dict containing the activity event data.
        """
        try:
            # Add schema version for forward compatibility
            event_data = event["event"].copy()
            event_data["schema_version"] = "1.0"
            await self.send(
                text_data=json.dumps(
                    {
                        "type": "activity_event",
                        "event": event_data,
                    }
                )
            )
            activities_websocket_events.labels(status="sent").inc()
        except Exception:
            # Best-effort: log and continue (PostgreSQL is authoritative)
            activities_websocket_failures.labels(stage="send").inc()
            logger.warning("websocket_send_failed: event=%s", event)

    async def audio_alert(self, event: dict) -> None:
        """Handle audio-alert events from ``alerts.services``.

        The audio-alert payload is shaped by
        :func:`alerts.services.emit_audio_alert_event` and is sent
        verbatim to the client. As with ``activity_event`` this is
        best-effort: a send failure only logs and increments the
        failure counter; the alert row is already persisted and
        the client can recover via ``GET /api/v1/alerts/``.

        Args:
            event: Dict with a ``payload`` key (added by the
                ``alerts.services`` group send).
        """
        try:
            payload = event.get("payload", {})
            await self.send(
                text_data=json.dumps({"type": "audio_alert", "event": payload})
            )
            activities_websocket_events.labels(status="sent").inc()
        except Exception:
            activities_websocket_failures.labels(stage="send").inc()
            logger.warning("websocket_send_failed: event=%s", event)

    async def _replay_alerts(self) -> None:
        """Push any unacknowledged alerts to the newly-connected client.

        On reconnect, the client may have missed alerts while
        disconnected. This method queries the DB for alerts where
        ``is_acknowledged=False`` and re-pushes them.

        Best-effort: failures are logged and counted but not propagated.
        """
        from alerts.metrics import replay as replay_metric
        from alerts.models import AudioAlert
        from alerts.services import build_push_payload

        try:
            alerts = await sync_to_async(list)(
                AudioAlert.objects.filter(
                    user=self.user, is_acknowledged=False
                ).order_by("-created_at")[:50]
            )
        except Exception:
            logger.warning(
                "websocket_replay_query_failed user=%s", self.user.id
            )
            return

        for alert in alerts:
            try:
                payload = await sync_to_async(build_push_payload)(alert)
                await self.send(
                    text_data=json.dumps(
                        {"type": "audio_alert", "event": payload}
                    )
                )
                activities_websocket_events.labels(status="replayed").inc()
            except Exception:
                activities_websocket_failures.labels(stage="replay").inc()
                logger.warning(
                    "websocket_replay_failed user=%s alert=%s",
                    self.user.id,
                    alert.id,
                )
        replay_metric(result="success")


async def emit_activity_event(user_id: int, event: dict) -> None:
    """Emit activity event to user's WebSocket.

    Note: This is best-effort. Redis/WebSocket failures must not
    affect activity execution correctness.

    Args:
        user_id: User ID to send event to.
        event: Event data dict.
    """
    channel_layer = get_channel_layer()

    try:
        await channel_layer.group_send(
            f"user_{user_id}",
            {
                "type": "activity_event",
                "event": event,
            },
        )
        activities_websocket_events.labels(status="queued").inc()
    except Exception:
        # Best-effort: log and continue (PostgreSQL is authoritative)
        activities_websocket_failures.labels(stage="emit").inc()
        logger.warning(
            "websocket_emit_failed: user_id=%d event_type=%s",
            user_id,
            event.get("action", "unknown"),
        )
