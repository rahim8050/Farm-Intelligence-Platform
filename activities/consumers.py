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
"""

import logging
from typing import Any

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.layers import get_channel_layer

logger = logging.getLogger("activities")


class ActivityConsumer(AsyncWebsocketConsumer):
    """WebSocket consumer for activity events.

    Auth: AuthMiddlewareStack (authenticated users only)
    WebSocket URL: ws://domain/ws/activities/

    Events:
        - activity_event: Sent when activity status changes
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

    async def disconnect(self, close_code: Any) -> None:
        """Handle WebSocket disconnection."""
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(
                self.group_name,
                self.channel_name,
            )

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
                text_data=__import__("json").dumps(
                    {
                        "type": "activity_event",
                        "event": event_data,
                    }
                )
            )
        except Exception:
            # Best-effort: log and continue (PostgreSQL is authoritative)
            logger.warning("websocket_send_failed: event=%s", event)


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
    except Exception:
        # Best-effort: log and continue (PostgreSQL is authoritative)
        logger.warning(
            "websocket_emit_failed: user_id=%d event_type=%s",
            user_id,
            event.get("action", "unknown"),
        )
