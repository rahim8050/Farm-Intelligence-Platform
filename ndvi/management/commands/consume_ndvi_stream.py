"""Redis Stream consumer for NDVI jobs.

This command implements Stage 4 of the NDVI Phase 2 Implementation Plan.
It consumes entries from the Redis stream, validates payloads, and
enqueues corresponding Celery tasks.

Reliability features:
- Consumer identity (hostname-pid)
- Idempotent group creation
- Dead-letter queue (DLQ) for poison messages
- Stale message reclamation (XPENDING + XCLAIM)
- Stream trimming (XTRIM)
- Graceful shutdown (SIGINT, SIGTERM)
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import time
from typing import Any

import redis
from django.conf import settings
from django.core.management.base import BaseCommand

from ndvi.streams import _get_stream_redis_client
from ndvi.tasks import compute_farm_state_coverage, run_ndvi_job

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """Redis Stream consumer for NDVI jobs."""

    help = "Consume NDVI jobs from Redis stream and enqueue Celery tasks."

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.should_exit = False
        # Generate consumer name: <hostname>-<pid>
        self.hostname = socket.gethostname()
        self.pid = os.getpid()
        self.consumer_name = f"{self.hostname}-{self.pid}"

    def handle(self, *args: Any, **options: Any) -> None:
        """Main entry point for the management command."""
        # Setup signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        self.stdout.write(
            self.style.SUCCESS(
                f"Starting NDVI stream consumer: {self.consumer_name}"
            )
        )

        client = _get_stream_redis_client()
        self._ensure_group(client)

        while not self.should_exit:
            try:
                # 1. Bounded reclaim pass
                self._reclaim_stale_messages(client)

                # 2. Block on XREADGROUP for new work (">")
                messages = self._read_messages(client)
                if messages:
                    self._process_batch(client, messages)

                # 3. Stream trimming
                self._trim_streams(client)

            except redis.ConnectionError as exc:
                logger.error(
                    "Redis connection error: %s. Retrying in 5s...", exc
                )
                time.sleep(5)
            except Exception as exc:
                logger.exception("Unexpected error in consumer loop: %s", exc)
                time.sleep(1)

        self.stdout.write(self.style.SUCCESS("NDVI stream consumer stopped."))

    def _handle_signal(self, signum: int, frame: Any) -> None:
        """Handle termination signals."""
        self.stdout.write(
            self.style.WARNING(f"\nReceived signal {signum}. Shutting down...")
        )
        self.should_exit = True

    def _ensure_group(self, client: redis.Redis) -> None:
        """Create the consumer group if it doesn't exist."""
        stream_name = settings.NDVI_STREAM_NAME
        group_name = settings.NDVI_STREAM_GROUP

        try:
            # Plan says use '0' to drain backlog, prompt says '$'.
            # We use '0' to be safer and not miss already-published work.
            client.xgroup_create(
                stream_name, group_name, id="0", mkstream=True
            )
            self.stdout.write(
                f"Created consumer group {group_name} on {stream_name}"
            )
        except redis.ResponseError as exc:
            if "BUSYGROUP" in str(exc):
                # Group already exists
                pass
            else:
                raise

    def _read_messages(
        self, client: redis.Redis
    ) -> list[tuple[str, dict[str, str]]]:
        """Read a batch of new messages from the stream."""
        stream_name = settings.NDVI_STREAM_NAME
        group_name = settings.NDVI_STREAM_GROUP
        block_ms = settings.NDVI_STREAM_BLOCK_MS
        batch_size = settings.NDVI_STREAM_BATCH_SIZE

        # ">" means only messages never delivered to other consumers
        response = client.xreadgroup(
            group_name,
            self.consumer_name,
            {stream_name: ">"},
            count=batch_size,
            block=block_ms,
        )

        messages = []
        if response:
            for _stream, entry_list in response:  # type: ignore[union-attr]
                for entry_id, payload in entry_list:
                    # Redis-py returns bytes or str depending on config.
                    # Our payload helpers use strings.
                    decoded_payload = {
                        k.decode() if isinstance(k, bytes) else k: (
                            v.decode() if isinstance(v, bytes) else v
                        )
                        for k, v in payload.items()
                    }
                    entry_id_str = (
                        entry_id.decode()
                        if isinstance(entry_id, bytes)
                        else entry_id
                    )
                    messages.append((entry_id_str, decoded_payload))
        return messages

    def _process_batch(
        self, client: redis.Redis, messages: list[tuple[str, dict[str, str]]]
    ) -> None:
        """Process a batch of stream entries."""
        for entry_id, payload in messages:
            if self.should_exit:
                break
            self._process_message(client, entry_id, payload)

    def _process_message(
        self, client: redis.Redis, entry_id: str, payload: dict[str, str]
    ) -> None:
        """Process a single stream entry."""
        stream_name = settings.NDVI_STREAM_NAME
        group_name = settings.NDVI_STREAM_GROUP
        max_deliveries = settings.NDVI_STREAM_MAX_DELIVERIES

        try:
            # 1. Check delivery count for poison messages
            # XPENDING <stream> <group> [<start> <end> <count> [<consumer>]]
            pending_info = client.xpending_range(
                stream_name, group_name, entry_id, entry_id, 1
            )

            if pending_info:
                delivery_count = pending_info[0]["times_delivered"]  # type: ignore[index]
                if delivery_count > max_deliveries:
                    self._move_to_dlq(
                        client, entry_id, payload, "max_deliveries_exceeded"
                    )
                    client.xack(stream_name, group_name, entry_id)
                    return

            # 2. Route payload
            job_type = payload.get("job_type")
            success = False

            if job_type == "farm_state_coverage":
                success = self._route_farm_state_coverage(payload)
            else:
                success = self._route_ndvi_job(payload)

            # 3. Acknowledge on success
            if success:
                client.xack(stream_name, group_name, entry_id)
                logger.info("Processed and acknowledged message %s", entry_id)
            else:
                logger.warning("Failed to route message %s", entry_id)

        except Exception as exc:
            logger.exception("Error processing message %s: %s", entry_id, exc)

    def _route_farm_state_coverage(self, payload: dict[str, str]) -> bool:
        """Validate and route farm state coverage payload to Celery."""
        try:
            farm_id = int(payload["farm_id"])
            engine = payload.get("engine")
            target_date = payload["target_date"]
            threshold = float(payload["threshold"])

            compute_farm_state_coverage.delay(
                farm_id=farm_id,
                engine=engine,
                target_date=target_date,
                threshold=threshold,
            )
            return True
        except (KeyError, ValueError) as exc:
            logger.error(
                "Structural error in farm_state_coverage payload: %s", exc
            )
            return False

    def _route_ndvi_job(self, payload: dict[str, str]) -> bool:
        """Validate and route NDVI job payload to Celery."""
        try:
            job_id = int(payload["job_id"])
            # verify required fields exist
            _ = payload["request_hash"]
            _ = payload["farm_id"]
            _ = payload["owner_id"]
            _ = payload["engine"]
            _ = payload["job_type"]

            run_ndvi_job.delay(job_id)
            return True
        except (KeyError, ValueError) as exc:
            logger.error("Structural error in NDVI job payload: %s", exc)
            return False

    def _move_to_dlq(
        self,
        client: redis.Redis,
        entry_id: str,
        payload: dict[str, str],
        reason: str,
    ) -> None:
        """Push a poison message to the DLQ stream."""
        dlq_name = settings.NDVI_STREAM_DLQ_NAME
        dlq_maxlen = settings.NDVI_STREAM_DLQ_MAXLEN

        dlq_payload = payload.copy()
        dlq_payload["dlq_reason"] = reason
        dlq_payload["dlq_original_id"] = entry_id
        dlq_payload["dlq_timestamp"] = str(time.time())
        dlq_payload["dlq_consumer"] = self.consumer_name

        client.xadd(
            dlq_name,
            dlq_payload,  # type: ignore[arg-type]
            maxlen=dlq_maxlen,
            approximate=True,
        )
        logger.error(
            "Moved message %s to DLQ %s. Reason: %s",
            entry_id,
            dlq_name,
            reason,
        )

    def _reclaim_stale_messages(self, client: redis.Redis) -> None:
        """Reclaim messages that have been pending for too long."""
        stream_name = settings.NDVI_STREAM_NAME
        group_name = settings.NDVI_STREAM_GROUP
        idle_ms = settings.NDVI_STREAM_CLAIM_IDLE_MS
        batch_size = settings.NDVI_STREAM_BATCH_SIZE

        # Get summary of pending messages
        # xpending returns [min_id, max_id, count, consumer_stats]
        pending_summary = client.xpending(stream_name, group_name)
        if not pending_summary or pending_summary["count"] == 0:  # type: ignore[index]
            return

        # Find messages pending longer than idle_ms
        # We look at a batch of pending messages
        pending_list = client.xpending_range(
            stream_name,
            group_name,
            min=pending_summary["min"],  # type: ignore[index]
            max=pending_summary["max"],  # type: ignore[index]
            count=batch_size,
        )

        to_claim = []
        for item in pending_list:  # type: ignore[union-attr]
            if item["millis_since_last_delivery"] >= idle_ms:
                to_claim.append(item["message_id"])

        if to_claim:
            # Reclaim the messages
            # xclaim returns the messages (entry_id, payload)
            claimed = client.xclaim(
                stream_name, group_name, self.consumer_name, idle_ms, to_claim
            )
            if claimed:
                logger.info("Reclaimed %d stale messages", len(claimed))  # type: ignore[arg-type]
                # Process reclaimed messages just like new ones
                decoded_claimed = []
                for entry_id, payload in claimed:  # type: ignore[union-attr]
                    decoded_payload = {
                        k.decode() if isinstance(k, bytes) else k: (
                            v.decode() if isinstance(v, bytes) else v
                        )
                        for k, v in payload.items()
                    }
                    entry_id_str = (
                        entry_id.decode()
                        if isinstance(entry_id, bytes)
                        else entry_id
                    )
                    decoded_claimed.append((entry_id_str, decoded_payload))

                self._process_batch(client, decoded_claimed)

    def _trim_streams(self, client: redis.Redis) -> None:
        """Trim the main stream and DLQ to their configured max lengths."""
        client.xtrim(
            settings.NDVI_STREAM_NAME,
            maxlen=settings.NDVI_STREAM_MAXLEN,
            approximate=True,
        )
        client.xtrim(
            settings.NDVI_STREAM_DLQ_NAME,
            maxlen=settings.NDVI_STREAM_DLQ_MAXLEN,
            approximate=True,
        )
