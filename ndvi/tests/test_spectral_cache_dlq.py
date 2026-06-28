from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import caches
from django.test import override_settings

from farms.models import Farm
from ndvi.cache import (
    DEFAULT_L2_TTL_SECONDS,
    STALE_TTL_SECONDS,
    cache_manager,
    clear_l1_caches,
)
from ndvi.models import NdviJob
from ndvi.queues.dead_letter import (
    DEAD_LETTER_TTL_SECONDS,
    clear_queue,
    get_dead_letters,
    push_dead_letter,
    remove_dead_letter,
    replay_dead_letters,
)

PASSWORD = "test-password-123"  # noqa: S105


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeS3Client:
    class exceptions:  # noqa: N801
        class NoSuchKey(Exception):  # noqa: N818
            pass

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def get_object(
        self,
        Bucket: str,  # noqa: N803
        Key: str,  # noqa: N803
    ) -> dict[str, Any]:
        if Key not in self.objects:
            raise self.exceptions.NoSuchKey("NoSuchKey")
        import io

        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(
        self,
        Bucket: str,  # noqa: N803
        Key: str,  # noqa: N803
        Body: bytes,  # noqa: N803
        ContentType: str,  # noqa: N803
    ) -> None:
        self.objects[Key] = Body

    def delete_object(
        self,
        Bucket: str,  # noqa: N803
        Key: str,  # noqa: N803
    ) -> None:
        self.objects.pop(Key, None)


class FakeRedisClient:
    def __init__(self) -> None:
        self.store: dict[str, set[bytes]] = {}
        self.ttls: dict[str, int] = {}

    def sadd(self, key: str, val: bytes | str) -> int:
        bval = val if isinstance(val, bytes) else val.encode("utf-8")
        self.store.setdefault(key, set()).add(bval)
        return 1

    def expire(self, key: str, ttl: int) -> int:
        self.ttls[key] = ttl
        return 1

    def smembers(self, key: str) -> set[bytes]:
        return self.store.get(key, set())

    def srem(self, key: str, val: bytes | str) -> int:
        bval = val if isinstance(val, bytes) else val.encode("utf-8")
        if key in self.store and bval in self.store[key]:
            self.store[key].remove(bval)
            return 1
        return 0

    def scard(self, key: str) -> int:
        return len(self.store.get(key, set()))

    def delete(self, *keys: str) -> int:
        count = 0
        for key in keys:
            if key in self.store:
                self.store.pop(key)
                count += 1
        return count

    def keys(self, pattern: str) -> list[str]:
        prefix = pattern.replace("*", "")
        return [k for k in self.store.keys() if k.startswith(prefix)]


# ---------------------------------------------------------------------------
# Cache Tests
# ---------------------------------------------------------------------------


class TestCacheManager:
    """Tests for multi-level CacheManager."""

    def test_l2_cache_lifecycle(self) -> None:
        key = "test_l2_key"
        val = {"metric": 0.9}

        caches["default"].clear()
        cache_manager.set_l2(key, val)

        # Fresh hit
        retrieved, is_stale = cache_manager.get_l2(key)
        assert retrieved == val
        assert is_stale is False

        # Stale hit simulation
        fake_time = time.monotonic() + DEFAULT_L2_TTL_SECONDS + 10
        with patch("time.monotonic", return_value=fake_time):
            retrieved, is_stale = cache_manager.get_l2(key)
            assert retrieved == val
            assert is_stale is True

        # Expired simulation
        fake_time = time.monotonic() + STALE_TTL_SECONDS + 10
        with patch("time.monotonic", return_value=fake_time):
            retrieved, is_stale = cache_manager.get_l2(key)
            assert retrieved is None
            assert is_stale is False

        # Legacy format fallback
        caches["default"].set(key, val, 100)
        retrieved, is_stale = cache_manager.get_l2(key)
        assert retrieved == val
        assert is_stale is False

        # Deletion
        cache_manager.delete_l2(key)
        retrieved, is_stale = cache_manager.get_l2(key)
        assert retrieved is None

    @patch("ndvi.cache._get_s3_bucket", return_value="test-bucket")
    @patch("ndvi.cache._get_s3_client")
    def test_l3_cache_operations(
        self,
        mock_client_factory: Any,
        mock_bucket: Any,
    ) -> None:
        fake_client = FakeS3Client()
        mock_client_factory.return_value = fake_client

        key = "test_l3_cog.tif"
        data = b"fake-tif-bytes"

        # Miss
        assert cache_manager.get_l3(key) is None

        # Set & Get
        cache_manager.set_l3(key, data)
        assert cache_manager.get_l3(key) == data

        # Delete
        cache_manager.delete_l3(key)
        assert cache_manager.get_l3(key) is None

    @patch("ndvi.cache._get_s3_client")
    def test_l3_cache_handles_exceptions(
        self,
        mock_client_factory: Any,
    ) -> None:
        mock_client = MagicMock()
        mock_client.exceptions.NoSuchKey = FakeS3Client.exceptions.NoSuchKey
        mock_client.get_object.side_effect = Exception("S3 Get Error")
        mock_client.put_object.side_effect = Exception("S3 Put Error")
        mock_client.delete_object.side_effect = Exception("S3 Delete Error")
        mock_client_factory.return_value = mock_client

        # S3 failures should be caught and return None
        assert cache_manager.get_l3("key") is None
        cache_manager.set_l3("key", b"data")
        cache_manager.delete_l3("key")

    def test_generic_get_set(self) -> None:
        key = "generic_key"
        val = b"generic_val"

        caches["default"].clear()
        # L2
        cache_manager.set(key, val, level=2)
        assert cache_manager.get(key, level=2) == val

        # L1 (implicit, no-op)
        assert cache_manager.get(key, level=1) is None
        cache_manager.set(key, val, level=1)

        # L4 (implicit, no-op)
        assert cache_manager.get(key, level=4) is None
        cache_manager.set(key, val, level=4)

        # Unknown level
        assert cache_manager.get(key, level=99) is None
        cache_manager.set(key, val, level=99)

        # L3 S3 type warning on non-bytes
        with patch.object(cache_manager, "set_l3") as mock_set_l3:
            cache_manager.set("key", "non-bytes-string", level=3)
            mock_set_l3.assert_not_called()

    def test_cache_invalidation(self) -> None:
        fake_redis = FakeRedisClient()
        fake_redis.sadd("spectral:NDVI:cache:123:stac:1", b"val")

        original_cache = cache_manager._cache
        cache_manager._cache = MagicMock()
        try:
            # Mock Redis client attachment
            mock_client = MagicMock()
            mock_client.get_client.return_value = fake_redis
            cache_manager._cache.client = mock_client

            cache_manager.invalidate_farm(123, "NDVI")
            assert len(fake_redis.store) == 0

            # Exception handling in invalidation
            mock_client.get_client.side_effect = Exception(
                "Redis connection lost"
            )
            cache_manager.invalidate("pattern")
        finally:
            cache_manager._cache = original_cache

    def test_key_builders(self) -> None:
        assert (
            cache_manager.build_l2_key("NDVI", 1, "stac", "2026-06-28", 256)
            == "spectral:NDVI:cache:1:stac:2026-06-28:256"
        )
        assert (
            cache_manager.build_l3_key("NDVI", "hash", 1, "2026-06-28")
            == "spectral/cogs/NDVI/hash/1/2026-06-28.tif"
        )

        prov = {"sensor": "Sentinel-2", "cloud": 12.5}
        key = cache_manager.build_l3_key_from_provenance(
            "NDVI", 1, "2026-06-28", prov
        )
        assert "spectral/cogs/NDVI/" in key
        assert "/1/2026-06-28.tif" in key

    def test_get_with_stale(self) -> None:
        key = "stale_helper_key"
        caches["default"].clear()
        cache_manager.set_l2(key, "val")

        val, headers = cache_manager.get_with_stale(key)
        assert val == "val"
        assert "Warning" not in headers

        fake_time = time.monotonic() + DEFAULT_L2_TTL_SECONDS + 10
        with patch("time.monotonic", return_value=fake_time):
            val, headers = cache_manager.get_with_stale(key)
            assert val == "val"
            assert headers["Warning"] == "299 - stale"

    def test_clear_l1_caches(self) -> None:
        clear_l1_caches()


# ---------------------------------------------------------------------------
# Dead Letter Queue Tests
# ---------------------------------------------------------------------------


class TestDeadLetterQueue:
    """Tests for Dead Letter Queue operations."""

    @patch("ndvi.queues.dead_letter._get_redis_client")
    def test_push_and_get_dead_letters(self, mock_get_redis: Any) -> None:
        fake_redis = FakeRedisClient()
        mock_get_redis.return_value = fake_redis

        queue = "ndvi_ingestion"
        assert get_dead_letters(queue) == []

        # Push
        push_dead_letter(queue, 42, metadata={"err": "timeout"})
        entries = get_dead_letters(queue)
        assert len(entries) == 1
        assert entries[0]["job_id"] == 42
        assert entries[0]["queue"] == queue
        assert entries[0]["metadata"] == {"err": "timeout"}
        assert (
            fake_redis.ttls[f"dead_letter:{queue}"] == DEAD_LETTER_TTL_SECONDS
        )

        # Remove
        remove_dead_letter(queue, 42)
        assert get_dead_letters(queue) == []

    @patch("ndvi.queues.dead_letter._get_redis_client", return_value=None)
    def test_ops_with_no_redis(self, mock_get_redis: Any) -> None:
        # None of these should crash when Redis is unavailable
        push_dead_letter("q", 1)
        assert get_dead_letters("q") == []
        remove_dead_letter("q", 1)
        assert clear_queue("q") == 0

    @patch("ndvi.queues.dead_letter._get_redis_client")
    def test_clear_queue(self, mock_get_redis: Any) -> None:
        fake_redis = FakeRedisClient()
        mock_get_redis.return_value = fake_redis

        push_dead_letter("q", 1)
        push_dead_letter("q", 2)
        assert clear_queue("q") == 2
        assert get_dead_letters("q") == []

    @patch("ndvi.queues.dead_letter._get_redis_client")
    def test_json_parse_error_tolerance(self, mock_get_redis: Any) -> None:
        fake_redis = FakeRedisClient()
        mock_get_redis.return_value = fake_redis

        # Add invalid JSON manually
        fake_redis.sadd("dead_letter:q", "invalid-json{")
        fake_redis.sadd("dead_letter:q", '{"job_id": 99}')

        entries = get_dead_letters("q")
        assert len(entries) == 1
        assert entries[0]["job_id"] == 99

        # Test remove tolerance with invalid JSON
        fake_redis.sadd("dead_letter:q", "invalid-json{")
        remove_dead_letter("q", 99)

    @pytest.mark.django_db
    @override_settings(CELERY_TASK_ALWAYS_EAGER=True)
    @patch("ndvi.queues.dead_letter._get_redis_client")
    @patch("ndvi.tasks.run_ndvi_job.delay")
    def test_replay_dead_letters(
        self,
        mock_delay: Any,
        mock_get_redis: Any,
        django_user_model: Any,
    ) -> None:
        fake_redis = FakeRedisClient()
        mock_get_redis.return_value = fake_redis

        user = django_user_model.objects.create_user(
            username="dlq-user", password=PASSWORD
        )
        farm = Farm.objects.create(
            owner=user,
            name="Farm",
            slug="dlq-farm",
            bbox_south=0.0,
            bbox_west=0.0,
            bbox_north=0.2,
            bbox_east=0.2,
            is_active=True,
        )
        job = NdviJob.objects.create(
            owner=user,
            farm=farm,
            job_type=NdviJob.JobType.REFRESH_LATEST,
            status=NdviJob.JobStatus.FAILED,
        )

        # 1. Normal active job replay
        push_dead_letter("ndvi_ingestion", job.id)
        report = replay_dead_letters()
        assert report["replayed"] == 1
        mock_delay.assert_called_once_with(job.id)
        job.refresh_from_db()
        assert job.status == NdviJob.JobStatus.QUEUED

        # 2. Replaying job that does not exist (should delete DLQ entry)
        mock_delay.reset_mock()
        push_dead_letter("ndvi_ingestion", 9999)
        report = replay_dead_letters()
        assert report["failed"] == 1
        assert get_dead_letters("ndvi_ingestion") == []

        # 3. Replaying stale job (>72 hours)
        mock_delay.reset_mock()
        stale_time = (datetime.now(UTC) - timedelta(hours=73)).isoformat()
        entry = {
            "job_id": job.id,
            "queue": "ndvi_ingestion",
            "failed_at": stale_time,
            "metadata": {},
        }
        fake_redis.sadd("dead_letter:ndvi_ingestion", json.dumps(entry))
        report = replay_dead_letters()
        assert report["stale"] == 1
        mock_delay.assert_not_called()

        # 4. Replaying entry with no job_id or bad json format
        mock_delay.reset_mock()
        entry_no_id = {
            "queue": "ndvi_ingestion",
            "failed_at": datetime.now(UTC).isoformat(),
        }
        fake_redis.sadd("dead_letter:ndvi_ingestion", json.dumps(entry_no_id))
        report = replay_dead_letters()
        # one from missing id (the stale job is skipped as stale,
        # the missing job 9999 was cleared in step 2)
        assert report["failed"] == 1

    @pytest.mark.django_db
    @patch("ndvi.queues.dead_letter._get_redis_client")
    @patch(
        "ndvi.queues.dead_letter.replay_dead_letter_entry",
        side_effect=Exception("Replay error"),
    )
    def test_replay_exception_handling(
        self,
        mock_replay_entry: Any,
        mock_get_redis: Any,
    ) -> None:
        fake_redis = FakeRedisClient()
        mock_get_redis.return_value = fake_redis

        push_dead_letter("ndvi_ingestion", 123)
        report = replay_dead_letters()
        assert report["failed"] == 1
