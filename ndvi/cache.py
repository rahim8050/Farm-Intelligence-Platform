"""Multi-level caching (L1, L2, L3, L4) for spectral index data.

Levels:
  - L1: In-process ``functools.lru_cache`` on engine factory and
    registry lookups.
  - L2: Redis (Django cache framework) for observation lists and
    PNG byte blobs.
  - L3: S3/MinIO for versioned computed COGs (immutable, keyed by
    provenance hash).
  - L4: Provider retrieval (external API — no caching logic needed).

Usage::

    from ndvi.cache import cache_manager

    # Get from L2 cache
    data = cache_manager.get(
        "spectral:NDMI:cache:42:stac:2025-06-01:512", level=2
    )

    # Set in L2 cache
    cache_manager.set(
        "spectral:NDMI:cache:42:stac:2025-06-01:512",
        data, level=2, ttl=900,
    )

    # Invalidate farm cache
    cache_manager.invalidate_farm(farm_id=42, index_type="NDMI")
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any

from django.core.cache import caches

from ndvi.logging import StructuredLogger
from ndvi.metrics import ndmi_cache_hit_ratio

logger = logging.getLogger(__name__)
slog = StructuredLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CACHE_LEVEL_L1 = 1  # In-process (lru_cache) — no methods needed
CACHE_LEVEL_L2 = 2  # Redis / Django cache framework
CACHE_LEVEL_L3 = 3  # S3/MinIO COG storage
CACHE_LEVEL_L4 = 4  # Provider retrieval (external) — no-op

# Default TTLs in seconds
DEFAULT_L2_TTL_SECONDS = 900  # 15 minutes
STALE_TTL_SECONDS = 86400  # 24 hours (for stale-serving)
L3_TTL_SECONDS = 86400 * 365  # 1 year (immutable COGs)

# L2 key pattern: spectral:{index_type}:cache:{farm_id}:{engine}:{date}:{size}
L2_KEY_PATTERN = "spectral:{index_type}:cache:{farm_id}:{engine}:{date}:{size}"

# L3 key pattern (S3/MinIO COG object key)
L3_KEY_PATTERN = (
    "spectral/cogs/{index_type}/{provenance_hash}/{farm_id}/{date}.tif"
)


# ---------------------------------------------------------------------------
# S3/MinIO client helper (lazy init)
# ---------------------------------------------------------------------------


def _get_s3_client() -> Any:
    """Return a boto3 S3 client configured from env vars."""
    import boto3  # type: ignore[import-untyped]

    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("AWS_S3_ENDPOINT_URL"),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        region_name=os.environ.get("AWS_S3_REGION_NAME", "us-east-1"),
    )


def _get_s3_bucket() -> str:
    return os.environ.get("AWS_STORAGE_BUCKET_NAME", "spectral-cogs")


# ---------------------------------------------------------------------------
# CacheManager
# ---------------------------------------------------------------------------


class CacheManager:
    """Multi-level cache manager for spectral index data.

    Provides a unified interface for L2 (Redis) and L3 (S3/MinIO) caching
    with stale-serving support for L2 and immutable versioned objects for L3.
    L1 is handled implicitly via ``@lru_cache`` on engine factories and
    registry lookups (no explicit get/set methods needed).
    L4 is external provider retrieval — no caching logic.
    """

    def __init__(self) -> None:
        self._cache = caches["default"]

    # ------------------------------------------------------------------
    # L2 — Redis / Django cache
    # ------------------------------------------------------------------

    def get_l2(self, key: str) -> tuple[Any, bool]:
        """Get from L2 cache.

        Returns ``(value, is_stale)`` where ``is_stale`` is ``True``
        when the cached entry is older than the primary TTL (15 min)
        but still within the stale-serving window (24 h).

        Sets ``Warning: stale`` semantics when ``is_stale`` is true.
        """
        raw = self._cache.get(key)
        if raw is None:
            slog.debug(
                "cache.miss",
                "L2 cache miss",
                cache_level=2,
                cache_key=key,
            )
            return None, False

        # Try to decode a tuple of (value, timestamp)
        if isinstance(raw, tuple) and len(raw) == 2:
            value, cached_at = raw
            age = time.monotonic() - cached_at
            if age < DEFAULT_L2_TTL_SECONDS:
                ndmi_cache_hit_ratio.labels(level="l2_fresh").inc()
                slog.debug(
                    "cache.hit",
                    "L2 cache fresh hit",
                    cache_level=2,
                    cache_key=key,
                    age_ms=age * 1000,
                )
                return value, False
            if age < STALE_TTL_SECONDS:
                ndmi_cache_hit_ratio.labels(level="l2_stale").inc()
                slog.debug(
                    "cache.stale_hit",
                    "L2 cache stale hit",
                    cache_level=2,
                    cache_key=key,
                    age_ms=age * 1000,
                )
                return value, True
            # Beyond stale window — treat as miss
            slog.debug(
                "cache.expired",
                "L2 cache expired",
                cache_level=2,
                cache_key=key,
            )
            return None, False

        # Legacy format (no timestamp) — treat as fresh
        ndmi_cache_hit_ratio.labels(level="l2_fresh").inc()
        slog.debug(
            "cache.hit",
            "L2 cache hit (legacy format)",
            cache_level=2,
            cache_key=key,
        )
        return raw, False

    def set_l2(
        self,
        key: str,
        value: Any,
        ttl: int = DEFAULT_L2_TTL_SECONDS,
    ) -> None:
        """Set L2 cache with a monotonic timestamp for stale detection."""
        payload = (value, time.monotonic())
        self._cache.set(key, payload, STALE_TTL_SECONDS)
        slog.debug(
            "cache.set",
            "L2 cache set",
            cache_level=2,
            cache_key=key,
            ttl=ttl,
        )

    def delete_l2(self, key: str) -> None:
        """Delete a single L2 key."""
        self._cache.delete(key)

    # ------------------------------------------------------------------
    # L3 — S3/MinIO versioned COGs
    # ------------------------------------------------------------------

    def get_l3(self, key: str) -> bytes | None:
        """Get a versioned COG from S3/MinIO by L3 key."""
        try:
            client = _get_s3_client()
            bucket = _get_s3_bucket()
            response = client.get_object(Bucket=bucket, Key=key)
            ndmi_cache_hit_ratio.labels(level="l3_hit").inc()
            return response["Body"].read()
        except client.exceptions.NoSuchKey:
            ndmi_cache_hit_ratio.labels(level="l3_miss").inc()
            return None
        except Exception:
            logger.exception("L3 get failed key=%s", key)
            return None

    def set_l3(self, key: str, data: bytes) -> None:
        """Store a versioned COG in S3/MinIO.

        The object is immutable once written (keyed by provenance hash).
        """
        try:
            client = _get_s3_client()
            bucket = _get_s3_bucket()
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=data,
                ContentType="image/tiff",
            )
            logger.debug("L3 set key=%s size=%d", key, len(data))
        except Exception:
            logger.exception("L3 set failed key=%s", key)

    def delete_l3(self, key: str) -> None:
        """Delete an L3 object from S3/MinIO."""
        try:
            client = _get_s3_client()
            bucket = _get_s3_bucket()
            client.delete_object(Bucket=bucket, Key=key)
        except Exception:
            logger.exception("L3 delete failed key=%s", key)

    # ------------------------------------------------------------------
    # Unified get/set across levels
    # ------------------------------------------------------------------

    def get(self, key: str, level: int = CACHE_LEVEL_L2) -> Any | None:
        """Get a value from the specified cache level.

        Args:
            key: Cache key.
            level: Cache level (2 or 3). L1 is implicit, L4 is no-op.

        Returns:
            The cached value, or ``None`` if not found.
        """
        if level == CACHE_LEVEL_L2:
            value, _ = self.get_l2(key)
            return value
        if level == CACHE_LEVEL_L3:
            return self.get_l3(key)
        if level == CACHE_LEVEL_L1:
            logger.debug("L1 lookup is implicit via @lru_cache — key=%s", key)
            return None
        if level == CACHE_LEVEL_L4:
            logger.debug("L4 (provider) has no cache — key=%s", key)
            return None
        logger.warning("Unknown cache level %d — key=%s", level, key)
        return None

    def set(
        self,
        key: str,
        value: Any,
        level: int = CACHE_LEVEL_L2,
        ttl: int | None = None,
    ) -> None:
        """Set a value at the specified cache level.

        Args:
            key: Cache key.
            value: Value to cache.
            level: Cache level (2 or 3).
            ttl: TTL in seconds (L2 default 900s, L3 uses 1 year).
        """
        resolved_ttl = ttl if ttl is not None else DEFAULT_L2_TTL_SECONDS
        if level == CACHE_LEVEL_L2:
            self.set_l2(key, value, resolved_ttl)
        elif level == CACHE_LEVEL_L3:
            if isinstance(value, bytes):
                self.set_l3(key, value)
            else:
                logger.warning(
                    "L3 set requires bytes — got %s key=%s",
                    type(value).__name__,
                    key,
                )
        else:
            logger.debug("Cache set skipped for level %d — key=%s", level, key)

    # ------------------------------------------------------------------
    # Invalidation
    # ------------------------------------------------------------------

    def invalidate(self, pattern: str) -> None:
        """Invalidate cache keys matching a pattern.

        Note: Redis wildcard deletion is O(n) on key space. Use specific
        patterns and avoid broad wildcards in production.

        Args:
            pattern: Glob-style pattern like ``"spectral:NDMI:cache:42:*"``.
        """
        try:
            client = getattr(self._cache, "client", None)
            if client and hasattr(client, "get_client"):
                redis_client = client.get_client(write=True)
                keys = redis_client.keys(pattern)
                if keys:
                    redis_client.delete(*keys)
                    logger.info(
                        "L2 invalidated %d keys for pattern=%s",
                        len(keys),
                        pattern,
                    )
        except Exception:
            logger.exception("L2 invalidation failed pattern=%s", pattern)

    def invalidate_farm(
        self,
        farm_id: int,
        index_type: str,
    ) -> None:
        """Invalidate all L2 cache entries for a farm and index type.

        Args:
            farm_id: Farm ID.
            index_type: Spectral index type (NDVI, NDWI, NDMI).
        """
        pattern = f"spectral:{index_type}:cache:{farm_id}:*"
        self.invalidate(pattern)

        # Also invalidate the traditional per-index cache keys
        lower_index = index_type.lower()
        ts_pattern = f"{lower_index}:cache:v*:ts:*:{farm_id}:*"
        latest_pattern = f"{lower_index}:cache:v*:latest:*:{farm_id}:*"
        self.invalidate(ts_pattern)
        self.invalidate(latest_pattern)

    # ------------------------------------------------------------------
    # Key builders
    # ------------------------------------------------------------------

    @staticmethod
    def build_l2_key(
        index_type: str,
        farm_id: int,
        engine: str,
        date_str: str,
        size: int = 512,
    ) -> str:
        """Build a standard L2 cache key.

        Pattern:
        ``spectral:{index_type}:cache:{farm_id}:{engine}:{date}:{size}``
        """
        return (
            f"spectral:{index_type}:cache:{farm_id}:{engine}:{date_str}:{size}"
        )

    @staticmethod
    def build_l3_key(
        index_type: str,
        provenance_hash: str,
        farm_id: int,
        date_str: str,
    ) -> str:
        """Build an L3 S3/MinIO object key.

        Pattern: ``spectral/cogs/{index_type}/{provenance_hash}/
        {farm_id}/{date}.tif``
        """
        return (
            f"spectral/cogs/{index_type}/{provenance_hash}/"
            f"{farm_id}/{date_str}.tif"
        )

    @staticmethod
    def build_l3_key_from_provenance(
        index_type: str,
        farm_id: int,
        date_str: str,
        provenance: dict[str, Any],
    ) -> str:
        """Build an L3 key deterministically from provenance data.

        The hash is computed from a canonical JSON representation of
        the provenance dict, ensuring immutability.
        """
        raw = json.dumps(provenance, sort_keys=True, default=str)
        hash_hex = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return (
            f"spectral/cogs/{index_type}/{hash_hex}/{farm_id}/{date_str}.tif"
        )

    # ------------------------------------------------------------------
    # Stale-serving helper
    # ------------------------------------------------------------------

    def get_with_stale(
        self, key: str
    ) -> tuple[Any | None, dict[str, str | None]]:
        """Get from L2 with stale-serving headers metadata.

        Returns ``(value, headers)`` where ``headers`` contains
        a ``Warning`` key set to ``"stale"`` if the entry is stale.

        Usage in DRF views::

            data, headers = cache_manager.get_with_stale(key)
            response = Response(data)
            if headers.get("Warning"):
                response["Warning"] = headers["Warning"]
            return response
        """
        value, is_stale = self.get_l2(key)
        headers: dict[str, str | None] = {}
        if is_stale:
            headers["Warning"] = "299 - stale"
        return value, headers


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

cache_manager = CacheManager()


# ---------------------------------------------------------------------------
# L1 helpers — functools.lru_cache wrappers for engine factories & registry
# ---------------------------------------------------------------------------

# These are already applied in services.py
# (e.g. @lru_cache on _build_stac_engine).
# We provide re-export targets here for convenience.


def clear_l1_caches() -> None:
    """Clear all L1 lru_caches on engine factories.

    Call this during testing or when configuration changes.
    """
    # Import here to avoid circular imports
    import ndvi.services as svc  # noqa: PLC0415

    for name in dir(svc):
        obj = getattr(svc, name)
        if hasattr(obj, "cache_clear"):
            obj.cache_clear()  # type: ignore[union-attr]

    # Also clear formula registry LRU caches if any
    from science.formulas.registry import FORMULA_REGISTRY  # noqa: PLC0415

    for _key, entry in FORMULA_REGISTRY.items():
        formatter = entry.get("formatter")
        if formatter is not None and hasattr(formatter, "cache_clear"):
            formatter.cache_clear()  # type: ignore[union-attr]
