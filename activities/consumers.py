"""WebSocket consumers for activity notifications.

Provides real-time activity status updates via Django Channels.

Auth: Uses AuthMiddlewareStack for user authentication.
WebSocket URL: ws://domain/ws/activities/
"""

import json
from typing import Any

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.layers import get_channel_layer


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

        # Join user-specific group
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

        Args:
            event: Dict containing the activity event data.
        """
        await self.send(text_data=json.dumps({
            "type": "activity_event",
            "event": event["event"],
        }))


async def emit_activity_event(user_id: int, event: dict) -> None:
    """Emit activity event to user's WebSocket.

    Args:
        user_id: User ID to send event to.
        event: Event data dict.
    """
    channel_layer = get_channel_layer()

    await channel_layer.group_send(
        f"user_{user_id}",
        {
            "type": "activity_event",
            "event": event,
        },
    )
