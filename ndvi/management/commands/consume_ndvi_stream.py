"""Redis Stream consumer for NDVI jobs.

This command implements Stage 4 of the NDVI Phase 2 Implementation Plan.
It consumes entries from the Redis stream, validates payloads, and
enqueues corresponding Celery tasks.

Refinements:
- Configurable start ID (0 vs $)
- Delivery count from Redis metadata (XAUTOCLAIM)
- XAUTOCLAIM for efficient stale message recovery
- Timed reclaim intervals
- Structured logging (message_id, delivery_count, action)
- Enriched DLQ metadata
- Graceful shutdown waiting for in-flight processing
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
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
        self._processing_lock = threading.Lock()
        self._last_reclaim_time = 0.0
        self._autoclaim_start_id = "0-0"

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
                # 1. Periodic bounded reclaim pass using XAUTOCLAIM
                self._periodic_reclaim(client)

                # 2. Block on XREADGROUP for new work (">")
                messages = self._read_messages(client)
                if messages:
                    # Use lock to ensure graceful shutdown waits
                    with self._processing_lock:
                        self._process_batch(client, messages)

                # 3. Stream trimming
                self._trim_streams(client)

            except redis.ConnectionError as exc:
                logger.error(
                    "Redis connection error: %s. Retrying in 5s...",
                    exc,
                    extra={"action": "reconnect"},
                )
                time.sleep(5)
            except Exception as exc:
                logger.exception(
                    "Unexpected error in consumer loop: %s",
                    exc,
                    extra={"action": "error"},
                )
                time.sleep(1)

        # Final wait for any in-flight processing that might have started
        # just before should_exit became True
        with self._processing_lock:
            self.stdout.write(
                self.style.SUCCESS("NDVI stream consumer stopped.")
            )

    def _handle_signal(self, signum: int, frame: Any) -> None:
        """Handle termination signals."""
        self.stdout.write(
            self.style.WARNING(
                f"\nReceived signal {signum}. Waiting for in-flight tasks..."
            )
        )
        self.should_exit = True

    def _ensure_group(self, client: redis.Redis) -> None:
        """Create the consumer group if it doesn't exist."""
        stream_name = settings.NDVI_STREAM_NAME
        group_name = settings.NDVI_STREAM_GROUP
        start_id = settings.NDVI_STREAM_START_ID

        try:
            client.xgroup_create(
                stream_name, group_name, id=start_id, mkstream=True
            )
            self.stdout.write(
                f"Created group {group_name} on {stream_name} from {start_id}"
            )
        except redis.ResponseError as exc:
            if "BUSYGROUP" in str(exc):
                pass
            else:
                raise

    def _read_messages(
        self, client: redis.Redis
    ) -> list[tuple[str, dict[str, str], int]]:
        """Read a batch of new messages from the stream."""
        stream_name = settings.NDVI_STREAM_NAME
        group_name = settings.NDVI_STREAM_GROUP
        block_ms = settings.NDVI_STREAM_BLOCK_MS
        batch_size = settings.NDVI_STREAM_BATCH_SIZE

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
                    decoded_payload = self._decode_payload(payload)
                    entry_id_str = self._decode_str(entry_id)

                    # For new messages, delivery count isn't in XREADGROUP.
                    # It is always 1 on the first delivery.
                    messages.append((entry_id_str, decoded_payload, 1))
        return messages

    def _periodic_reclaim(self, client: redis.Redis) -> None:
        """Run XAUTOCLAIM if the interval has passed."""
        now = time.time()
        interval = settings.NDVI_STREAM_RECLAIM_INTERVAL_SECONDS

        if now - self._last_reclaim_time < interval:
            return

        self._last_reclaim_time = now
        self._run_autoclaim(client)

    def _run_autoclaim(self, client: redis.Redis) -> None:
        """Reclaim stale messages using XAUTOCLAIM."""
        stream_name = settings.NDVI_STREAM_NAME
        group_name = settings.NDVI_STREAM_GROUP
        idle_ms = settings.NDVI_STREAM_CLAIM_IDLE_MS
        batch_size = settings.NDVI_STREAM_BATCH_SIZE

        try:
            # xautoclaim returns [next_start_id, entries, deleted_ids]
            # each entry in entries is [id, payload]
            # Note: redis-py 4.x+ supports xautoclaim
            # We need to get delivery count. xautoclaim doesn't return it
            # directly in the same way xpending does, but it increments it.
            # Actually, to get delivery count accurately for DLQ logic,
            # we need to use xpending_range for the reclaimed messages.

            result = client.xautoclaim(
                stream_name,
                group_name,
                self.consumer_name,
                idle_ms,
                start_id=self._autoclaim_start_id,
                count=batch_size,
            )

            if not isinstance(result, (list, tuple)) or len(result) < 2:
                logger.error("Invalid xautoclaim result format: %s", result)
                return

            self._autoclaim_start_id = self._decode_str(result[0])
            entries = result[1]

            if entries:
                logger.info(
                    "Reclaimed %d stale messages",
                    len(entries),
                    extra={"action": "reclaim", "count": len(entries)},
                )
                reclaimed_messages = []
                for entry_id, payload in entries:
                    eid_str = self._decode_str(entry_id)
                    # Get accurate delivery count from Redis metadata
                    pending_info = client.xpending_range(
                        stream_name, group_name, eid_str, eid_str, 1
                    )
                    delivery_count = 1
                    if (
                        isinstance(pending_info, (list, tuple))
                        and pending_info
                    ):
                        delivery_count = pending_info[0]["times_delivered"]

                    reclaimed_messages.append(
                        (
                            eid_str,
                            self._decode_payload(payload),
                            delivery_count,
                        )
                    )

                with self._processing_lock:
                    self._process_batch(client, reclaimed_messages)

        except Exception as exc:
            logger.exception("XAUTOCLAIM failed: %s", exc)

    def _process_batch(
        self,
        client: redis.Redis,
        messages: list[tuple[str, dict[str, str], int]],
    ) -> None:
        """Process a batch of stream entries."""
        for entry_id, payload, delivery_count in messages:
            if self.should_exit:
                break
            self._process_message(client, entry_id, payload, delivery_count)

    def _process_message(
        self,
        client: redis.Redis,
        entry_id: str,
        payload: dict[str, str],
        delivery_count: int,
    ) -> None:
        """Process a single stream entry."""
        stream_name = settings.NDVI_STREAM_NAME
        group_name = settings.NDVI_STREAM_GROUP
        max_deliveries = settings.NDVI_STREAM_MAX_DELIVERIES

        log_context = {
            "message_id": entry_id,
            "delivery_count": delivery_count,
            "job_type": payload.get("job_type"),
        }

        try:
            # 1. Check delivery count for poison messages
            if delivery_count > max_deliveries:
                self._move_to_dlq(
                    client, entry_id, payload, "max_deliveries_exceeded"
                )
                client.xack(stream_name, group_name, entry_id)
                logger.warning(
                    "Poison message moved to DLQ",
                    extra={**log_context, "action": "dlq"},
                )
                return

            # 2. Route payload
            job_type = payload.get("job_type")
            success = False

            if job_type == "farm_state_coverage":
                success = self._route_farm_state_coverage(payload, log_context)
            else:
                success = self._route_ndvi_job(payload, log_context)

            # 3. Acknowledge on success
            if success:
                client.xack(stream_name, group_name, entry_id)
                logger.info(
                    "Acknowledged message",
                    extra={**log_context, "action": "ack"},
                )
            else:
                logger.warning(
                    "Failed to route message",
                    extra={**log_context, "action": "retry_later"},
                )

        except Exception as exc:
            logger.exception(
                "Error processing message %s: %s",
                entry_id,
                exc,
                extra={**log_context, "action": "error"},
            )

    def _route_farm_state_coverage(
        self, payload: dict[str, str], log_ctx: dict[str, Any]
    ) -> bool:
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
                "Structural error in farm_state_coverage payload: %s",
                exc,
                extra={**log_ctx, "error": str(exc)},
            )
            return False

    def _route_ndvi_job(
        self, payload: dict[str, str], log_ctx: dict[str, Any]
    ) -> bool:
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
            logger.error(
                "Structural error in NDVI job payload: %s",
                exc,
                extra={**log_ctx, "error": str(exc)},
            )
            return False

    def _move_to_dlq(
        self,
        client: redis.Redis,
        entry_id: str,
        payload: dict[str, str],
        reason: str,
    ) -> None:
        """Push a poison message to the DLQ stream with metadata."""
        dlq_name = settings.NDVI_STREAM_DLQ_NAME
        dlq_maxlen = settings.NDVI_STREAM_DLQ_MAXLEN

        dlq_payload = payload.copy()
        dlq_payload["dlq_reason"] = reason
        dlq_payload["dlq_original_id"] = entry_id
        dlq_payload["dlq_timestamp"] = str(time.time())
        dlq_payload["dlq_consumer"] = self.consumer_name
        dlq_payload["dlq_stream"] = settings.NDVI_STREAM_NAME

        # Source accurate delivery count for DLQ record
        pending_info = client.xpending_range(
            settings.NDVI_STREAM_NAME,
            settings.NDVI_STREAM_GROUP,
            entry_id,
            entry_id,
            1,
        )
        if isinstance(pending_info, (list, tuple)) and pending_info:
            count = pending_info[0]["times_delivered"]
            dlq_payload["dlq_delivery_count"] = str(count)
        else:
            # Fallback if pending_info is unexpectedly empty
            dlq_payload["dlq_delivery_count"] = "1"

        client.xadd(
            dlq_name,
            dlq_payload,  # type: ignore[arg-type]
            maxlen=dlq_maxlen,
            approximate=True,
        )

    def _decode_str(self, val: Any) -> str:
        """Safe decode bytes to str."""
        if isinstance(val, bytes):
            return val.decode()
        return str(val)

    def _decode_payload(self, payload: dict[Any, Any]) -> dict[str, str]:
        """Decode all keys and values in a payload dict."""
        return {
            self._decode_str(k): self._decode_str(v)
            for k, v in payload.items()
        }

    def _trim_streams(self, client: redis.Redis) -> None:
        """Trim the main stream and DLQ."""
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
