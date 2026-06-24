from __future__ import annotations

import hashlib
import json
import logging
import math
import secrets
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Any

from django.conf import settings
from django.core.cache import caches
from django.db import IntegrityError, models, transaction
from django.db.models import QuerySet
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from farms.models import Farm

from .engines.base import BBox, NDVIEngine, NdviPoint
from .engines.sentinelhub import SentinelHubEngine
from .metrics import (
    ndvi_append_only_writes_total,
    ndvi_cache_hit_total,
    ndvi_constraint_collision_total,
    ndvi_idempotent_hit_total,
    ndvi_jobs_total,
    ndvi_lock_contention_total,
    ndvi_long_lock_wait_total,
    ndvi_recompute_failure_total,
    ndvi_retry_classification_total,
    ndvi_retry_storm_window_total,
    ndvi_starvation_events_total,
    ndvi_supersession_total,
    ndvi_transaction_duration_seconds,
)
from .models import NdviJob, NdviObservation
from .raster.base import ColormapNormalization
from .raster.registry import resolve_raster_engine_name

logger = logging.getLogger(__name__)

SUPPORTED_ENGINES = ("gee", "sentinelhub", "stac", "landsat", "modis")

_CB_STATES = ("closed", "open", "half_open")

_ALLOWED_JSON_TYPES = (dict, list, str, int, float, bool, type(None))

_PRERELEASE_ORDER = {"alpha": 0, "beta": 1, "rc": 2, "release": 3}


@dataclass(frozen=True)
class NdviVersion:
    """Parsed NDVI version with semantic comparison support.

    Supports:
    - Core versions: v1.0, v2.1.3, v10.0.1
    - Prerelease tags: v2.1-alpha, v2.1-beta, v2.1-rc1
    - Hotfix suffixes: v2.1.1-hotfix, v2.1.1-hotfix2
    - Date-based versions: v2025.03.15, v20250315

    Comparison rules:
    - Core version numbers compared numerically (major.minor.patch)
    - Prerelease versions are less than release versions
    - Prerelease order: alpha < beta < rc < release
    - Hotfix suffixes are ignored for comparison (same as base version)
    - Date-based versions are compared as numeric tuples
    """

    major: int
    minor: int
    patch: int
    prerelease: str = "release"
    prerelease_num: int = 0
    is_hotfix: bool = False
    hotfix_num: int = 0
    raw: str = ""

    def __lt__(self, other: NdviVersion) -> bool:
        if (self.major, self.minor, self.patch) != (
            other.major,
            other.minor,
            other.patch,
        ):
            return (self.major, self.minor, self.patch) < (
                other.major,
                other.minor,
                other.patch,
            )
        self_prerelease = _PRERELEASE_ORDER.get(self.prerelease, -1)
        other_prerelease = _PRERELEASE_ORDER.get(other.prerelease, -1)
        if self_prerelease != other_prerelease:
            return self_prerelease < other_prerelease
        if self.prerelease_num != other.prerelease_num:
            return self.prerelease_num < other.prerelease_num
        return False

    def __le__(self, other: NdviVersion) -> bool:
        return self == other or self < other

    def __gt__(self, other: NdviVersion) -> bool:
        return not self <= other

    def __ge__(self, other: NdviVersion) -> bool:
        return not self < other

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, NdviVersion):
            return NotImplemented
        return (
            self.major == other.major
            and self.minor == other.minor
            and self.patch == other.patch
            and self.prerelease == other.prerelease
            and self.prerelease_num == other.prerelease_num
        )

    def normalized(self) -> str:
        """Return normalized version string."""
        core = f"v{self.major}.{self.minor}.{self.patch}"
        if self.prerelease != "release":
            core += f"-{self.prerelease}"
            if self.prerelease_num > 1:
                core += str(self.prerelease_num)
        if self.is_hotfix:
            core += "-hotfix"
            if self.hotfix_num > 1:
                core += str(self.hotfix_num)
        return core


def _parse_prerelease(part: str) -> tuple[str, int]:
    """Parse prerelease tag like 'alpha', 'beta1', 'rc2'."""
    for tag in ("alpha", "beta", "rc"):
        if part.startswith(tag):
            num_str = part[len(tag) :]
            num = int(num_str) if num_str else 1
            return tag, num
    return "release", 0


def parse_version(version: str) -> tuple[int, ...]:
    """Parse a version string into a tuple of ints for comparison.

    Supports:
    - Core versions: v1.0, v2.1.3, v10.0.1
    - Prerelease tags: v2.1-alpha, v2.1-beta, v2.1-rc1
    - Hotfix suffixes: v2.1.1-hotfix, v2.1.1-hotfix2
    - Date-based versions: v2025.03.15, v20250315

    Returns a tuple of integers suitable for lexicographic comparison.
    Prerelease and hotfix metadata are stripped for simple comparison.
    For full semantic comparison, use parse_ndvi_version().

    Examples:
        'v1.0' -> (1, 0)
        'v2.1.3' -> (2, 1, 3)
        'v10.0' -> (10, 0)
        '1.2.3' -> (1, 2, 3)
        'v2.1-beta' -> (2, 1)
        'v2.1.1-hotfix' -> (2, 1, 1)
    """
    cleaned = version.strip()
    if cleaned and cleaned[0] in ("v", "V"):
        cleaned = cleaned[1:]

    # Strip hotfix suffix
    cleaned = cleaned.split("-hotfix")[0]

    # Strip prerelease tag
    for tag in ("-alpha", "-beta", "-rc"):
        if tag in cleaned:
            cleaned = cleaned.split(tag)[0]
            break

    parts = cleaned.split(".")
    result: list[int] = []
    for part in parts:
        try:
            result.append(int(part))
        except ValueError as exc:
            raise ValueError(
                f"Invalid version component '{part}' in '{version}'. "
                f"Expected numeric components separated by dots."
            ) from exc
    return tuple(result)


def parse_ndvi_version(version: str) -> NdviVersion:
    """Parse a version string into a full NdviVersion object.

    Supports prerelease tags, hotfix suffixes, and date-based versions.
    Use this for full semantic comparison including prerelease ordering.

    Examples:
        'v2.1-beta' -> NdviVersion(2, 1, 0, prerelease='beta')
        'v2.1.1-hotfix' -> NdviVersion(2, 1, 1, is_hotfix=True)
        'v2025.03.15' -> NdviVersion(2025, 3, 15)
    """
    cleaned = version.strip()
    if cleaned and cleaned[0] in ("v", "V"):
        cleaned = cleaned[1:]

    is_hotfix = False
    hotfix_num = 0
    if "-hotfix" in cleaned:
        is_hotfix = True
        hotfix_part = cleaned.split("-hotfix")[1]
        hotfix_num = int(hotfix_part) if hotfix_part else 1
        cleaned = cleaned.split("-hotfix")[0]

    prerelease = "release"
    prerelease_num = 0
    for tag in ("-alpha", "-beta", "-rc"):
        if tag in cleaned:
            tag_name = tag.lstrip("-")
            suffix = cleaned.split(tag)[1]
            prerelease_num = int(suffix) if suffix else 1
            prerelease = tag_name
            cleaned = cleaned.split(tag)[0]
            break

    parts = cleaned.split(".")
    major = int(parts[0]) if len(parts) > 0 else 0
    minor = int(parts[1]) if len(parts) > 1 else 0
    patch = int(parts[2]) if len(parts) > 2 else 0

    return NdviVersion(
        major=major,
        minor=minor,
        patch=patch,
        prerelease=prerelease,
        prerelease_num=prerelease_num,
        is_hotfix=is_hotfix,
        hotfix_num=hotfix_num,
        raw=version,
    )


def version_gte(version: str, min_version: str) -> bool:
    """Return True if version >= min_version using semantic comparison.

    Handles versions like 'v1.0', 'v2.1.3', 'v10.0' correctly,
    avoiding the lexicographic trap where 'v10' < 'v2'.

    For prerelease-aware comparison, use parse_ndvi_version() directly.
    """
    return parse_version(version) >= parse_version(min_version)


def normalize_version(version: str) -> str:
    """Normalize a version string to canonical form.

    Rules:
    - Always prefixed with 'v'
    - At least major.minor (patch defaults to 0)
    - Prerelease tags lowercase: -alpha, -beta, -rc
    - Hotfix suffix: -hotfix (with number if > 1)

    Examples:
        '1.0' -> 'v1.0.0'
        'V2.1-beta' -> 'v2.1.0-beta'
        'v2.1.1-hotfix2' -> 'v2.1.1-hotfix2'
    """
    parsed = parse_ndvi_version(version)
    return parsed.normalized()


def is_valid_ndvi_version(version: str) -> bool:
    """Check if a version string is a valid NDVI version.

    Valid formats:
    - v{major}.{minor}[.{patch}][-alpha[n]|-beta[n]|-rc[n]]
    - v{major}.{minor}[.{patch}][-hotfix[n]]
    - Date-based: v{YYYY}.{MM}.{DD} or v{YYYYMMDD}

    Returns True if the version can be parsed, False otherwise.
    """
    try:
        parse_ndvi_version(version)
        return True
    except (ValueError, IndexError):
        return False


def _cb_cache_key(engine: str) -> str:
    return f"ndvi:cb:{engine}"


def _check_retry_circuit_breaker(engine: str) -> bool:
    """Check if the circuit breaker is open for upsert retries.

    Uses Django cache (Redis in production) for global coordination
    across all workers. States: closed, open, half_open.

    Returns True if retries should be suppressed (circuit open).
    In half_open state, allows limited retries to test recovery.

    Failure semantics:
    - Redis unavailable: fail-open (return False, allow retries)
    - Stale cache: handled by TTL and cutoff logic
    - Partial network partition: fail-open for reads
    - Cache failover: relies on Django cache backend retry logic

    This ensures that cache failures do not block all retries,
    which would be worse than allowing uncoordinated retries.
    """
    from django.core.cache import caches

    window = get_ndvi_retry_circuit_breaker_window()
    max_failures = get_ndvi_retry_circuit_breaker_max_failures()
    half_open_max = get_ndvi_retry_circuit_breaker_half_open_max()
    cache = caches["default"]
    key = _cb_cache_key(engine)
    now = time.monotonic()

    try:
        state_data = cache.get(key)
    except Exception as exc:
        logger.warning(
            "ndvi.circuit_breaker.cache_unavailable fail_open "
            "engine=%s error=%s",
            engine,
            exc,
        )
        return False

    if state_data is None:
        state_data = {
            "state": "closed",
            "failures": [],
            "half_open_attempts": 0,
            "last_failure": 0,
        }
        try:
            cache.set(key, state_data, timeout=window * 2)
        except Exception as exc:
            logger.debug(
                "ndvi.circuit_breaker.cache_set_failed engine=%s error=%s",
                engine,
                exc,
            )

    cutoff = now - window
    recent = [t for t in state_data["failures"] if t > cutoff]
    state_data["failures"] = recent

    current_state = state_data["state"]

    if current_state == "closed":
        if len(recent) >= max_failures:
            state_data["state"] = "open"
            state_data["half_open_attempts"] = 0
            state_data["last_failure"] = now
            try:
                cache.set(key, state_data, timeout=window * 2)
            except Exception as exc:
                logger.debug(
                    "ndvi.circuit_breaker.cache_set_failed engine=%s error=%s",
                    engine,
                    exc,
                )
            logger.warning(
                "ndvi.upsert.circuit_breaker_open engine=%s "
                "failures=%d window=%ds",
                engine,
                len(recent),
                window,
            )
        return False

    if current_state == "open":
        if state_data["last_failure"] < cutoff:
            state_data["state"] = "half_open"
            state_data["half_open_attempts"] = 0
            try:
                cache.set(key, state_data, timeout=window * 2)
            except Exception as exc:
                logger.debug(
                    "ndvi.circuit_breaker.cache_set_failed engine=%s error=%s",
                    engine,
                    exc,
                )
            logger.info(
                "ndvi.upsert.circuit_breaker_half_open engine=%s",
                engine,
            )
            return False
        return True

    if current_state == "half_open":
        if state_data["half_open_attempts"] >= half_open_max:
            state_data["state"] = "open"
            state_data["last_failure"] = now
            try:
                cache.set(key, state_data, timeout=window * 2)
            except Exception as exc:
                logger.debug(
                    "ndvi.circuit_breaker.cache_set_failed engine=%s error=%s",
                    engine,
                    exc,
                )
            logger.warning(
                "ndvi.upsert.circuit_breaker_reopened engine=%s "
                "half_open_attempts=%d",
                engine,
                half_open_max,
            )
            return True
        return False

    return False


def _record_upsert_failure(engine: str) -> None:
    """Record an upsert failure for circuit breaker tracking.

    Uses Django cache for global coordination across workers.
    Fails silently if cache is unavailable to avoid blocking upserts.
    """
    from django.core.cache import caches

    now = time.monotonic()
    cache = caches["default"]
    key = _cb_cache_key(engine)
    window = get_ndvi_retry_circuit_breaker_window()

    try:
        state_data = cache.get(key)
    except Exception as exc:
        logger.warning(
            "ndvi.circuit_breaker.record_failure_cache_error "
            "engine=%s error=%s",
            engine,
            exc,
        )
        return

    if state_data is None:
        state_data = {
            "state": "closed",
            "failures": [],
            "half_open_attempts": 0,
            "last_failure": 0,
        }

    state_data["failures"].append(now)
    state_data["last_failure"] = now

    if state_data["state"] == "half_open":
        state_data["half_open_attempts"] += 1
        state_data["state"] = "open"
        logger.warning(
            "ndvi.upsert.circuit_breaker_half_open_failed engine=%s",
            engine,
        )

    try:
        cache.set(key, state_data, timeout=window * 2)
    except Exception as exc:
        logger.warning(
            "ndvi.circuit_breaker.record_failure_cache_set_error "
            "engine=%s error=%s",
            engine,
            exc,
        )


def _record_upsert_success(engine: str) -> None:
    """Record an upsert success to reset circuit breaker.

    If in half_open state, a success closes the circuit.
    Fails silently if cache is unavailable.
    """
    from django.core.cache import caches

    cache = caches["default"]
    key = _cb_cache_key(engine)
    window = get_ndvi_retry_circuit_breaker_window()

    try:
        state_data = cache.get(key)
    except Exception as exc:
        logger.warning(
            "ndvi.circuit_breaker.record_success_cache_error "
            "engine=%s error=%s",
            engine,
            exc,
        )
        return

    if state_data is None:
        return

    if state_data["state"] == "half_open":
        state_data["state"] = "closed"
        state_data["failures"] = []
        state_data["half_open_attempts"] = 0
        try:
            cache.set(key, state_data, timeout=window * 2)
        except Exception as exc:
            logger.warning(
                "ndvi.circuit_breaker.record_success_cache_set_error "
                "engine=%s error=%s",
                engine,
                exc,
            )
        logger.info(
            "ndvi.upsert.circuit_breaker_closed engine=%s",
            engine,
        )


def get_ndvi_retry_circuit_breaker_half_open_max() -> int:
    return _get_int_setting(
        "NDVI_RETRY_CIRCUIT_BREAKER_HALF_OPEN_MAX",
        DEFAULT_NDVI_RETRY_CIRCUIT_BREAKER_HALF_OPEN_MAX,
    )


DEFAULT_NDVI_ENGINE_NAME = "sentinelhub"
DEFAULT_NDVI_QUEUE_BACKEND = "celery"
DEFAULT_NDVI_VERSION = "v1-legacy"
DEFAULT_NDVI_APPEND_ONLY = False
DEFAULT_NDVI_RECOMPUTE_MAX_WINDOW_DAYS = 90
DEFAULT_NDVI_RECOMPUTE_CHUNK_SIZE = 50
DEFAULT_NDVI_RECOMPUTE_BACKPRESSURE_THRESHOLD = 1000
DEFAULT_NDVI_ANOMALY_THRESHOLD = 0.30
DEFAULT_NDVI_UPSERT_MAX_RETRIES = 3
DEFAULT_NDVI_UPSERT_RETRY_DELAY = 0.1
DEFAULT_NDVI_UPSERT_RETRY_JITTER = 0.05
DEFAULT_NDVI_RETRY_CIRCUIT_BREAKER_WINDOW = 300
DEFAULT_NDVI_RETRY_CIRCUIT_BREAKER_MAX_FAILURES = 10
DEFAULT_NDVI_RETRY_CIRCUIT_BREAKER_HALF_OPEN_MAX = 3
DEFAULT_NDVI_QUEUE_INGESTION = "ndvi_ingestion"
DEFAULT_NDVI_QUEUE_RECOMPUTE = "ndvi_recompute"
DEFAULT_NDVI_QUEUE_ANALYSIS = "ndvi_analysis"
DEFAULT_NDVI_ENFORCE_QUEUE_ISOLATION = False
DEFAULT_NDVI_QUEUE_ISOLATION_MODE = "single"
DEFAULT_MAX_AREA_KM2 = 5000.0
DEFAULT_MAX_DATERANGE_DAYS = 370
DEFAULT_STEP_DAYS = 7
DEFAULT_MAX_CLOUD = 30
DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_LOCK_TIMEOUT_SECONDS = 60
DEFAULT_CACHE_TTL_TIMESERIES_SECONDS = 86400
DEFAULT_CACHE_TTL_LATEST_SECONDS = 21600
DEFAULT_COLORMAP_NORMALIZATION = "histogram"
DEFAULT_VALID_PIXEL_THRESHOLD = 0.30
NDVI_TIMESERIES_CACHE_VERSION = 2
NDVI_LATEST_CACHE_VERSION = 2


def _get_int_setting(name: str, default: int) -> int:
    return int(getattr(settings, name, default))


def _get_float_setting(name: str, default: float) -> float:
    return float(getattr(settings, name, default))


def get_default_ndvi_engine_name() -> str:
    return str(
        getattr(settings, "NDVI_ENGINE", DEFAULT_NDVI_ENGINE_NAME)
    ).lower()


def get_ndvi_queue_backend() -> str:
    return str(
        getattr(settings, "NDVI_QUEUE_BACKEND", DEFAULT_NDVI_QUEUE_BACKEND)
    ).lower()


def get_ndvi_version() -> str:
    return str(getattr(settings, "NDVI_VERSION", DEFAULT_NDVI_VERSION))


def get_ndvi_append_only() -> bool:
    return bool(
        getattr(settings, "NDVI_APPEND_ONLY", DEFAULT_NDVI_APPEND_ONLY)
    )


def get_ndvi_recompute_max_window_days() -> int:
    return _get_int_setting(
        "NDVI_RECOMPUTE_MAX_WINDOW_DAYS",
        DEFAULT_NDVI_RECOMPUTE_MAX_WINDOW_DAYS,
    )


def get_ndvi_recompute_chunk_size() -> int:
    return _get_int_setting(
        "NDVI_RECOMPUTE_CHUNK_SIZE",
        DEFAULT_NDVI_RECOMPUTE_CHUNK_SIZE,
    )


def get_ndvi_recompute_backpressure_threshold() -> int:
    return _get_int_setting(
        "NDVI_RECOMPUTE_BACKPRESSURE_THRESHOLD",
        DEFAULT_NDVI_RECOMPUTE_BACKPRESSURE_THRESHOLD,
    )


def get_ndvi_anomaly_threshold() -> float:
    return float(
        getattr(
            settings,
            "NDVI_ANOMALY_THRESHOLD",
            DEFAULT_NDVI_ANOMALY_THRESHOLD,
        )
    )


def get_ndvi_upsert_max_retries() -> int:
    return _get_int_setting(
        "NDVI_UPSERT_MAX_RETRIES",
        DEFAULT_NDVI_UPSERT_MAX_RETRIES,
    )


def get_ndvi_upsert_retry_delay() -> float:
    return float(
        getattr(
            settings,
            "NDVI_UPSERT_RETRY_DELAY",
            DEFAULT_NDVI_UPSERT_RETRY_DELAY,
        )
    )


def get_ndvi_upsert_retry_jitter() -> float:
    return float(
        getattr(
            settings,
            "NDVI_UPSERT_RETRY_JITTER",
            DEFAULT_NDVI_UPSERT_RETRY_JITTER,
        )
    )


def get_ndvi_retry_circuit_breaker_window() -> int:
    return _get_int_setting(
        "NDVI_RETRY_CIRCUIT_BREAKER_WINDOW",
        DEFAULT_NDVI_RETRY_CIRCUIT_BREAKER_WINDOW,
    )


def get_ndvi_retry_circuit_breaker_max_failures() -> int:
    return _get_int_setting(
        "NDVI_RETRY_CIRCUIT_BREAKER_MAX_FAILURES",
        DEFAULT_NDVI_RETRY_CIRCUIT_BREAKER_MAX_FAILURES,
    )


def get_ndvi_queue_name(queue_type: str) -> str:
    """Get the Celery queue name for a given NDVI queue type.

    Queue types: ingestion, recompute, analysis.
    """
    setting_map = {
        "ingestion": "NDVI_QUEUE_INGESTION",
        "recompute": "NDVI_QUEUE_RECOMPUTE",
        "analysis": "NDVI_QUEUE_ANALYSIS",
    }
    default_map = {
        "ingestion": DEFAULT_NDVI_QUEUE_INGESTION,
        "recompute": DEFAULT_NDVI_QUEUE_RECOMPUTE,
        "analysis": DEFAULT_NDVI_QUEUE_ANALYSIS,
    }
    return str(
        getattr(
            settings,
            setting_map.get(queue_type, "NDVI_QUEUE_INGESTION"),
            default_map.get(queue_type, DEFAULT_NDVI_QUEUE_INGESTION),
        )
    )


def get_ndvi_enforce_queue_isolation() -> bool:
    return bool(
        getattr(
            settings,
            "NDVI_ENFORCE_QUEUE_ISOLATION",
            DEFAULT_NDVI_ENFORCE_QUEUE_ISOLATION,
        )
    )


def get_ndvi_queue_isolation_mode() -> str:
    """Get the queue isolation mode.

    Modes:
    - "single": Exactly one NDVI queue (strict isolation)
    - "subset": Allowed subset of NDVI queues (flexible)
    - "all": All NDVI queues (legacy behavior)

    Default is "single" for strict workload isolation.
    """
    mode = str(
        getattr(
            settings,
            "NDVI_QUEUE_ISOLATION_MODE",
            DEFAULT_NDVI_QUEUE_ISOLATION_MODE,
        )
    ).lower()
    if mode not in ("single", "subset", "all"):
        logger.warning(
            "Invalid NDVI_QUEUE_ISOLATION_MODE '%s', using default 'single'",
            mode,
        )
        return "single"
    return mode


def validate_queue_isolation(
    worker_queues: list[str] | None = None,
) -> bool:
    """Validate worker queue config matches isolation policy.

    When NDVI_ENFORCE_QUEUE_ISOLATION=True, this function checks that
    the Celery worker's configured queues match the expected set based
    on NDVI_QUEUE_ISOLATION_MODE.

    Modes:
    - "single": Exactly one NDVI queue (strict isolation).
      Recommended for production workload isolation.
    - "subset": Non-empty subset of NDVI queues (flexible).
      Allows multi-queue workers while preventing non-NDVI mixing.
    - "all": All NDVI queues (legacy behavior).

    Fail-closed in production: returns False on validation errors
    to prevent silent bypass of isolation guarantees.
    Fail-open in DEBUG mode to avoid blocking local development.

    Should be called at worker startup to prevent accidental
    multi-queue workers from processing mixed workloads.

    Args:
        worker_queues: Optional list of queue names the worker is
            consuming from. If None, detects from Celery config.
    """
    if not get_ndvi_enforce_queue_isolation():
        return True

    isolation_mode = get_ndvi_queue_isolation_mode()
    ndvi_queues = [
        get_ndvi_queue_name("ingestion"),
        get_ndvi_queue_name("recompute"),
        get_ndvi_queue_name("analysis"),
    ]

    try:
        if worker_queues is None:
            from celery import current_app

            worker_queues = list(current_app.conf.task_queues.keys())

        configured = set(worker_queues)
        ndvi_set = set(ndvi_queues)
        overlap = configured & ndvi_set

        if isolation_mode == "single":
            if len(overlap) != 1:
                logger.error(
                    "ndvi.queue_isolation_violation mode=single "
                    "expected_exactly_one_ndvi_queue got=%s configured=%s",
                    sorted(overlap),
                    sorted(configured),
                )
                return False

        elif isolation_mode == "subset":
            if not overlap:
                logger.error(
                    "ndvi.queue_isolation_violation mode=subset "
                    "expected_at_least_one_ndvi_queue got=%s configured=%s",
                    sorted(overlap),
                    sorted(configured),
                )
                return False

        elif isolation_mode == "all":
            if not ndvi_set.issubset(configured):
                logger.error(
                    "ndvi.queue_isolation_violation mode=all "
                    "expected=%s configured=%s",
                    sorted(ndvi_set),
                    sorted(configured),
                )
                return False

        return True

    except Exception as exc:
        is_debug = getattr(settings, "DEBUG", False)
        if is_debug:
            logger.warning(
                "ndvi.queue_isolation_check_failed fail_open_debug error=%s",
                exc,
            )
            return True
        logger.error(
            "ndvi.queue_isolation_check_failed fail_closed_production "
            "error=%s",
            exc,
        )
        return False


def get_max_area_km2() -> float:
    return _get_float_setting("NDVI_MAX_AREA_KM2", DEFAULT_MAX_AREA_KM2)


def get_max_daterange_days() -> int:
    return _get_int_setting(
        "NDVI_MAX_DATERANGE_DAYS", DEFAULT_MAX_DATERANGE_DAYS
    )


def get_default_step_days() -> int:
    return _get_int_setting("NDVI_DEFAULT_STEP_DAYS", DEFAULT_STEP_DAYS)


def get_default_max_cloud() -> int:
    return _get_int_setting("NDVI_DEFAULT_MAX_CLOUD", DEFAULT_MAX_CLOUD)


def get_default_lookback_days() -> int:
    return _get_int_setting(
        "NDVI_DEFAULT_LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS
    )


def get_default_colormap_normalization() -> ColormapNormalization:
    """Get the default colormap normalization mode from settings."""
    mode_str = str(
        getattr(
            settings,
            "NDVI_COLORMAP_NORMALIZATION",
            DEFAULT_COLORMAP_NORMALIZATION,
        )
    ).lower()
    try:
        return ColormapNormalization(mode_str)
    except ValueError:
        logger.warning(
            "Invalid NDVI_COLORMAP_NORMALIZATION '%s', "
            "using default 'histogram'",
            mode_str,
        )
        return ColormapNormalization.HISTOGRAM


def get_valid_pixel_threshold() -> float:
    """Get the minimum valid pixel fraction for observation acceptance."""
    return float(
        getattr(
            settings,
            "NDVI_VALID_PIXEL_THRESHOLD",
            DEFAULT_VALID_PIXEL_THRESHOLD,
        )
    )


def normalize_cloud_fraction(cloud_fraction: float | None) -> float | None:
    """Normalize cloud cover values to a 0.0-1.0 ratio when possible."""

    if cloud_fraction is None:
        return None
    value = float(cloud_fraction)
    if value > 1.0:
        return value / 100.0
    return value


def cloud_fraction_is_within_limit(
    cloud_fraction: float | None,
    max_cloud: int,
) -> bool:
    """Return True when a point is clean enough for the requested limit."""

    normalized = normalize_cloud_fraction(cloud_fraction)
    if normalized is None:
        return True
    # Cap the effective threshold so overly permissive settings do not let
    # obviously cloudy scenes leak into stored observations or derived reads.
    effective_max_cloud = min(max_cloud, DEFAULT_MAX_CLOUD)
    return normalized <= (effective_max_cloud / 100.0)


def filter_observations_by_cloud(
    observations: Iterable[NdviObservation],
    *,
    max_cloud: int,
) -> list[NdviObservation]:
    """Filter stored observations to those within the cloud threshold."""

    return [
        observation
        for observation in observations
        if cloud_fraction_is_within_limit(
            observation.cloud_fraction, max_cloud
        )
    ]


def get_lock_timeout_seconds() -> int:
    return _get_int_setting(
        "NDVI_LOCK_TIMEOUT_SECONDS", DEFAULT_LOCK_TIMEOUT_SECONDS
    )


def get_cache_ttl_timeseries() -> int:
    return _get_int_setting(
        "NDVI_CACHE_TTL_TIMESERIES_SECONDS",
        DEFAULT_CACHE_TTL_TIMESERIES_SECONDS,
    )


def get_cache_ttl_latest() -> int:
    return _get_int_setting(
        "NDVI_CACHE_TTL_LATEST_SECONDS",
        DEFAULT_CACHE_TTL_LATEST_SECONDS,
    )


def _build_sentinelhub_engine() -> NDVIEngine:
    return SentinelHubEngine()


@lru_cache(maxsize=1)
def _build_stac_engine() -> NDVIEngine:
    from .engines.stac import StacEngine

    return StacEngine()


@lru_cache(maxsize=1)
def _build_gee_engine() -> NDVIEngine:
    from .engines.gee import GeeEngine

    return GeeEngine()


@lru_cache(maxsize=1)
def _build_landsat_engine() -> NDVIEngine:
    from .engines.landsat import LandsatEngine

    return LandsatEngine()


def _build_ndwi_sentinelhub_engine() -> NDVIEngine:
    return SentinelHubEngine(index_type="NDWI")


@lru_cache(maxsize=1)
def _build_ndwi_stac_engine() -> NDVIEngine:
    from .engines.stac import StacEngine

    return StacEngine(index_type="NDWI")


@lru_cache(maxsize=1)
def _build_ndwi_gee_engine() -> NDVIEngine:
    from .engines.gee import GeeEngine

    return GeeEngine(index_type="NDWI")


@lru_cache(maxsize=1)
def _build_ndwi_landsat_engine() -> NDVIEngine:
    from .engines.landsat import LandsatEngine

    return LandsatEngine(index_type="NDWI")


@lru_cache(maxsize=1)
def _build_ndwi_modis_engine() -> NDVIEngine:
    from .engines.modis import ModisEngine

    return ModisEngine(index_type="NDWI")


@lru_cache(maxsize=1)
def _build_modis_engine() -> NDVIEngine:
    from .engines.modis import ModisEngine

    return ModisEngine()


def _build_ndmi_sentinelhub_engine() -> NDVIEngine:
    return SentinelHubEngine(index_type="NDMI")


@lru_cache(maxsize=1)
def _build_ndmi_stac_engine() -> NDVIEngine:
    from .engines.stac import StacEngine

    return StacEngine(index_type="NDMI")


@lru_cache(maxsize=1)
def _build_ndmi_gee_engine() -> NDVIEngine:
    from .engines.gee import GeeEngine

    return GeeEngine(index_type="NDMI")


@lru_cache(maxsize=1)
def _build_ndmi_landsat_engine() -> NDVIEngine:
    from .engines.landsat import LandsatEngine

    return LandsatEngine(index_type="NDMI")


@lru_cache(maxsize=1)
def _build_ndmi_modis_engine() -> NDVIEngine:
    from .engines.modis import ModisEngine

    return ModisEngine(index_type="NDMI")


ENGINE_FACTORIES: dict[str, Callable[[], NDVIEngine]] = {
    "gee": _build_gee_engine,
    "sentinelhub": _build_sentinelhub_engine,
    "stac": _build_stac_engine,
    "landsat": _build_landsat_engine,
    "modis": _build_modis_engine,
    "ndwi_gee": _build_ndwi_gee_engine,
    "ndwi_sentinelhub": _build_ndwi_sentinelhub_engine,
    "ndwi_stac": _build_ndwi_stac_engine,
    "ndwi_landsat": _build_ndwi_landsat_engine,
    "ndwi_modis": _build_ndwi_modis_engine,
    "ndmi_gee": _build_ndmi_gee_engine,
    "ndmi_sentinelhub": _build_ndmi_sentinelhub_engine,
    "ndmi_stac": _build_ndmi_stac_engine,
    "ndmi_landsat": _build_ndmi_landsat_engine,
    "ndmi_modis": _build_ndmi_modis_engine,
}


def get_engine(
    engine_name: str | None = None, *, index_type: str = "NDVI"
) -> NDVIEngine:
    engine = resolve_ndvi_engine_name(engine_name)
    prefix = {"NDWI": "ndwi", "NDMI": "ndmi"}.get(index_type)
    factory_key = f"{prefix}_{engine}" if prefix else engine
    factory = ENGINE_FACTORIES.get(factory_key)
    if not factory:
        raise ValueError(f"Unsupported {index_type} engine: {engine}")
    from ndvi.metrics import ndvi_source_usage_total

    ndvi_source_usage_total.labels(source=engine, endpoint="get_engine").inc()
    return factory()


@dataclass(frozen=True)
class TimeseriesParams:
    start: date
    end: date
    step_days: int
    max_cloud: int


@dataclass(frozen=True)
class LatestParams:
    lookback_days: int
    max_cloud: int


def resolve_ndvi_engine_name(
    engine_name: str | None,
    *,
    default_engine: str | None = None,
) -> str:
    resolved = (
        engine_name
        if engine_name is not None
        else default_engine or get_default_ndvi_engine_name()
    )
    engine = str(resolved).lower()
    if engine not in SUPPORTED_ENGINES:
        raise ValueError(f"Unsupported NDVI engine: {engine}")
    return engine


def normalize_bbox(farm: Farm) -> BBox:
    if (
        farm.bbox_south is None
        or farm.bbox_west is None
        or farm.bbox_north is None
        or farm.bbox_east is None
    ):
        raise ValidationError("Farm must include a bounding box for NDVI.")
    bbox = BBox(
        south=Decimal(farm.bbox_south),
        west=Decimal(farm.bbox_west),
        north=Decimal(farm.bbox_north),
        east=Decimal(farm.bbox_east),
    )
    if bbox.west >= bbox.east or bbox.south >= bbox.north:
        raise ValidationError(
            "Farm bounding box must have west < east and south < north."
        )
    return bbox


def _approx_area_km2(bbox: BBox) -> float:
    mean_lat = (bbox.north + bbox.south) / Decimal(2)
    lat_km = (bbox.north - bbox.south) * Decimal("111.32")
    lon_km = (
        (bbox.east - bbox.west)
        * Decimal(math.cos(math.radians(float(mean_lat))))
        * Decimal("111.32")
    )
    area = abs(lat_km * lon_km)
    return float(area)


def normalize_timeseries_params(
    start: date,
    end: date,
    step_days: int | None,
    max_cloud: int | None,
    *,
    default_max_cloud: int | None = None,
) -> TimeseriesParams:
    if start > end:
        raise ValidationError("start must be on or before end.")

    delta_days = (end - start).days
    if delta_days > get_max_daterange_days():
        raise ValidationError(
            "Requested date range exceeds NDVI_MAX_DATERANGE_DAYS."
        )

    step = step_days or get_default_step_days()
    step = max(1, min(step, 30))

    cloud_default = (
        default_max_cloud
        if default_max_cloud is not None
        else get_default_max_cloud()
    )
    cloud = max_cloud if max_cloud is not None else cloud_default
    cloud = max(0, min(cloud, 100))

    return TimeseriesParams(
        start=start, end=end, step_days=step, max_cloud=cloud
    )


def normalize_latest_params(
    lookback_days: int | None,
    max_cloud: int | None,
    *,
    default_max_cloud: int | None = None,
) -> LatestParams:
    lookback = lookback_days or get_default_lookback_days()
    lookback = max(1, min(lookback, get_max_daterange_days()))

    cloud_default = (
        default_max_cloud
        if default_max_cloud is not None
        else get_default_max_cloud()
    )
    cloud = max_cloud if max_cloud is not None else cloud_default
    cloud = max(0, min(cloud, 100))

    return LatestParams(lookback_days=lookback, max_cloud=cloud)


def hash_request(
    *,
    engine: str,
    owner_id: int,
    farm_id: int,
    params: dict[str, Any],
    index_type: str = "NDVI",
) -> str:
    normalized = json.dumps(
        {
            "engine": engine,
            "owner": owner_id,
            "farm": farm_id,
            "params": params,
            "index_type": index_type,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def expected_buckets(start: date, end: date, step_days: int) -> list[date]:
    buckets: list[date] = []
    cursor = start
    while cursor <= end:
        buckets.append(cursor)
        cursor = cursor + timedelta(days=step_days)
    return buckets


def detect_gaps(
    existing_dates: set[date], expected: Iterable[date]
) -> list[date]:
    missing: list[date] = []
    for bucket in expected:
        if bucket not in existing_dates:
            missing.append(bucket)
    return missing


def acquire_lock(
    request_hash: str, *, timeout: int | None = None
) -> str | None:
    ttl = timeout or get_lock_timeout_seconds()
    cache = caches["default"]
    key = f"ndvi:lock:{request_hash}"
    token = str(uuid.uuid4())
    if cache.add(key, token, ttl):
        return token
    return None


def release_lock(request_hash: str, token: str) -> None:
    cache = caches["default"]
    key = f"ndvi:lock:{request_hash}"

    # Lua script for atomic compare-and-delete
    lua_script = """
    if redis.call("get", KEYS[1]) == ARGV[1] then
        return redis.call("del", KEYS[1])
    else
        return 0
    end
    """

    try:
        client = getattr(cache, "client", None)
        if client and hasattr(client, "get_client"):
            redis_client = client.get_client()
        else:
            redis_client = None

        # Check if it's a real Redis client
        if redis_client and hasattr(redis_client, "eval"):
            redis_client.eval(lua_script, 1, key, token)
        else:
            # Fallback for LocMemCache (tests) or if client is not Redis
            if cache.get(key) == token:
                cache.delete(key)
    except Exception as exc:
        logger.warning("Error releasing lock (token failure): %s", exc)


def cache_timeseries_response(
    owner_id: int,
    farm_id: int,
    engine: str,
    params: TimeseriesParams,
    payload: dict[str, Any],
) -> None:
    cache = caches["default"]
    key = (
        f"ndvi:cache:v{NDVI_TIMESERIES_CACHE_VERSION}:ts:{owner_id}:"
        f"{farm_id}:{engine}:{params.start}:{params.end}:"
        f"{params.step_days}:{params.max_cloud}"
    )
    cache.set(key, payload, get_cache_ttl_timeseries())


def get_cached_timeseries_response(
    owner_id: int,
    farm_id: int,
    engine: str,
    params: TimeseriesParams,
) -> dict[str, Any] | None:
    cache = caches["default"]
    key = (
        f"ndvi:cache:v{NDVI_TIMESERIES_CACHE_VERSION}:ts:{owner_id}:"
        f"{farm_id}:{engine}:{params.start}:{params.end}:"
        f"{params.step_days}:{params.max_cloud}"
    )
    cached = cache.get(key)
    if cached:
        ndvi_cache_hit_total.labels(layer="timeseries").inc()
    return cached


def cache_latest_response(
    owner_id: int,
    farm_id: int,
    engine: str,
    params: LatestParams,
    payload: dict[str, Any],
) -> None:
    cache = caches["default"]
    key = (
        f"ndvi:cache:v{NDVI_LATEST_CACHE_VERSION}:latest:{owner_id}:"
        f"{farm_id}:{engine}:{params.lookback_days}:{params.max_cloud}"
    )
    cache.set(key, payload, get_cache_ttl_latest())


def get_cached_latest_response(
    owner_id: int,
    farm_id: int,
    engine: str,
    params: LatestParams,
) -> dict[str, Any] | None:
    cache = caches["default"]
    key = (
        f"ndvi:cache:v{NDVI_LATEST_CACHE_VERSION}:latest:{owner_id}:"
        f"{farm_id}:{engine}:{params.lookback_days}:{params.max_cloud}"
    )
    cached = cache.get(key)
    if cached:
        ndvi_cache_hit_total.labels(layer="latest").inc()
    return cached


def cache_ndwi_timeseries_response(
    owner_id: int,
    farm_id: int,
    engine: str,
    params: TimeseriesParams,
    payload: dict[str, Any],
) -> None:
    cache = caches["default"]
    key = (
        f"ndwi:cache:v{NDVI_TIMESERIES_CACHE_VERSION}:ts:{owner_id}:"
        f"{farm_id}:{engine}:{params.start}:{params.end}:"
        f"{params.step_days}:{params.max_cloud}"
    )
    cache.set(key, payload, get_cache_ttl_timeseries())


def get_cached_ndwi_timeseries_response(
    owner_id: int,
    farm_id: int,
    engine: str,
    params: TimeseriesParams,
) -> dict[str, Any] | None:
    cache = caches["default"]
    key = (
        f"ndwi:cache:v{NDVI_TIMESERIES_CACHE_VERSION}:ts:{owner_id}:"
        f"{farm_id}:{engine}:{params.start}:{params.end}:"
        f"{params.step_days}:{params.max_cloud}"
    )
    cached = cache.get(key)
    if cached:
        ndvi_cache_hit_total.labels(layer="ndwi_timeseries").inc()
    return cached


def cache_ndwi_latest_response(
    owner_id: int,
    farm_id: int,
    engine: str,
    params: LatestParams,
    payload: dict[str, Any],
) -> None:
    cache = caches["default"]
    key = (
        f"ndwi:cache:v{NDVI_LATEST_CACHE_VERSION}:latest:{owner_id}:"
        f"{farm_id}:{engine}:{params.lookback_days}:{params.max_cloud}"
    )
    cache.set(key, payload, get_cache_ttl_latest())


def get_cached_ndwi_latest_response(
    owner_id: int,
    farm_id: int,
    engine: str,
    params: LatestParams,
) -> dict[str, Any] | None:
    cache = caches["default"]
    key = (
        f"ndwi:cache:v{NDVI_LATEST_CACHE_VERSION}:latest:{owner_id}:"
        f"{farm_id}:{engine}:{params.lookback_days}:{params.max_cloud}"
    )
    cached = cache.get(key)
    if cached:
        ndvi_cache_hit_total.labels(layer="ndwi_latest").inc()
    return cached


def cache_ndmi_timeseries_response(
    owner_id: int,
    farm_id: int,
    engine: str,
    params: TimeseriesParams,
    payload: dict[str, Any],
) -> None:
    cache = caches["default"]
    key = (
        f"ndmi:cache:v{NDVI_TIMESERIES_CACHE_VERSION}:ts:{owner_id}:"
        f"{farm_id}:{engine}:{params.start}:{params.end}:"
        f"{params.step_days}:{params.max_cloud}"
    )
    cache.set(key, payload, get_cache_ttl_timeseries())


def get_cached_ndmi_timeseries_response(
    owner_id: int,
    farm_id: int,
    engine: str,
    params: TimeseriesParams,
) -> dict[str, Any] | None:
    cache = caches["default"]
    key = (
        f"ndmi:cache:v{NDVI_TIMESERIES_CACHE_VERSION}:ts:{owner_id}:"
        f"{farm_id}:{engine}:{params.start}:{params.end}:"
        f"{params.step_days}:{params.max_cloud}"
    )
    cached = cache.get(key)
    if cached:
        ndvi_cache_hit_total.labels(layer="ndmi_timeseries").inc()
    return cached


def cache_ndmi_latest_response(
    owner_id: int,
    farm_id: int,
    engine: str,
    params: LatestParams,
    payload: dict[str, Any],
) -> None:
    cache = caches["default"]
    key = (
        f"ndmi:cache:v{NDVI_LATEST_CACHE_VERSION}:latest:{owner_id}:"
        f"{farm_id}:{engine}:{params.lookback_days}:{params.max_cloud}"
    )
    cache.set(key, payload, get_cache_ttl_latest())


def get_cached_ndmi_latest_response(
    owner_id: int,
    farm_id: int,
    engine: str,
    params: LatestParams,
) -> dict[str, Any] | None:
    cache = caches["default"]
    key = (
        f"ndmi:cache:v{NDVI_LATEST_CACHE_VERSION}:latest:{owner_id}:"
        f"{farm_id}:{engine}:{params.lookback_days}:{params.max_cloud}"
    )
    cached = cache.get(key)
    if cached:
        ndvi_cache_hit_total.labels(layer="ndmi_latest").inc()
    return cached


def enforce_quota(farm: Farm, bbox: BBox) -> None:
    area_km2 = _approx_area_km2(bbox)
    if area_km2 > get_max_area_km2():
        raise ValidationError("Requested area exceeds NDVI_MAX_AREA_KM2.")


VALID_PROVENANCE_KEYS = frozenset(
    {
        "engine_version",
        "scl_mask",
        "cloud_mask",
        "resolution",
        "quality_profile",
        "fusion_mode",
        "schema_version",
    }
)


def _validate_json_types(obj: Any, path: str = "root") -> None:
    """Recursively validate JSON-serializable types.

    Raises ValueError if unsupported types are found, with a clear
    path to the offending value.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            _validate_json_types(value, path=f"{path}.{key}")
    elif isinstance(obj, list):
        for idx, value in enumerate(obj):
            _validate_json_types(value, path=f"{path}[{idx}]")
    elif not isinstance(obj, _ALLOWED_JSON_TYPES):
        raise ValueError(
            f"Unsupported type {type(obj).__name__} at {path}. "
            f"Provenance data must contain only JSON-serializable types "
            f"(dict, list, str, int, float, bool, None)."
        )


def compute_provenance_hash(provenance: dict[str, Any]) -> str:
    """Compute a deterministic hash of provenance data for idempotency.

    Uses strict canonical JSON serialization:
    - sorted keys
    - no whitespace (separators=(',', ':'))
    - ensure_ascii=True

    Rejects unsupported types instead of silently coercing them with
    default=str, preventing silent identity drift from unexpected
    serialization behavior.

    Raises:
        ValueError: If provenance contains non-JSON-serializable types.
    """
    _validate_json_types(provenance)
    canonical = json.dumps(
        provenance,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def validate_provenance(provenance: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize provenance data.

    Raises:
        ValueError: If provenance contains unrecognized keys
            or invalid schema_version.
    """
    if not provenance:
        return {}

    unknown = set(provenance.keys()) - VALID_PROVENANCE_KEYS
    if unknown:
        raise ValueError(
            f"Unrecognized provenance keys: {sorted(unknown)}. "
            f"Allowed: {sorted(VALID_PROVENANCE_KEYS)}"
        )

    schema_version = provenance.get("schema_version", "1")
    if schema_version != "1":
        raise ValueError(
            f"Unsupported provenance schema_version: {schema_version}"
        )

    return dict(provenance)


def _determine_observation_state(
    cloud_fraction: float | None,
    *,
    max_cloud: int,
    valid_pixel_fraction: float | None = None,
) -> str:
    """Determine the lifecycle state for an observation.

    Phase 1: Observations with valid_pixel_fraction below the threshold
    are rejected. cloud_fraction=None cannot be FINAL.
    Observations with unknown cloud quality are kept as RAW only.
    """
    from .models import NdviObservation

    threshold = get_valid_pixel_threshold()
    if valid_pixel_fraction is not None and valid_pixel_fraction < threshold:
        return NdviObservation.ObservationState.REJECTED

    if cloud_fraction is None:
        return NdviObservation.ObservationState.RAW

    if not cloud_fraction_is_within_limit(cloud_fraction, max_cloud):
        return NdviObservation.ObservationState.RAW

    return NdviObservation.ObservationState.FINAL


def upsert_observations(
    *,
    farm: Farm,
    engine: str,
    max_cloud: int,
    points: Iterable[NdviPoint],
    source_scene_ids: dict[date, str] | None = None,
    provenance: dict[str, Any] | None = None,
    index_type: str = "NDVI",
) -> list[NdviObservation]:
    """Upsert NDVI/NDWI observations with full transactional guarantees.

    Transaction scope (all inside one atomic block):
        1. select_for_update on existing latest rows
        2. Idempotency check (scene_id + provenance_hash)
        3. Supersession of previous latest rows
        4. New observation creation
        5. State transition validation

    On IntegrityError from concurrent inserts, retries with
    exponential backoff up to NDVI_UPSERT_MAX_RETRIES.

    Records transaction duration and lock contention metrics
    for observability.
    """
    saved: list[NdviObservation] = []
    version = get_ndvi_version()
    append_only = get_ndvi_append_only()
    now = timezone.now()
    max_retries = get_ndvi_upsert_max_retries()
    base_delay = get_ndvi_upsert_retry_delay()

    validated_provenance = validate_provenance(provenance or {})
    prov_hash = (
        compute_provenance_hash(validated_provenance)
        if validated_provenance
        else None
    )

    tx_start = time.monotonic()
    for attempt in range(max_retries):
        try:
            with transaction.atomic():
                for point in points:
                    if not cloud_fraction_is_within_limit(
                        point.cloud_fraction,
                        max_cloud,
                    ):
                        logger.info(
                            "ndvi.observation.skipped_cloudy "
                            "farm_id=%s engine=%s date=%s "
                            "cloud_fraction=%s max_cloud=%s",
                            farm.id,
                            engine,
                            point.date,
                            point.cloud_fraction,
                            max_cloud,
                        )
                        continue

                    valid_threshold = get_valid_pixel_threshold()
                    if (
                        point.valid_pixel_fraction is not None
                        and point.valid_pixel_fraction < valid_threshold
                    ):
                        logger.info(
                            "ndvi.observation.rejected_low_valid_pixels "
                            "farm_id=%s engine=%s date=%s "
                            "valid_pixel_fraction=%s threshold=%s",
                            farm.id,
                            engine,
                            point.date,
                            point.valid_pixel_fraction,
                            valid_threshold,
                        )
                        continue

                    state = _determine_observation_state(
                        point.cloud_fraction,
                        max_cloud=max_cloud,
                        valid_pixel_fraction=point.valid_pixel_fraction,
                    )
                    quality_flags = point.quality_flags or {}
                    scene_id = (
                        source_scene_ids.get(point.date)
                        if source_scene_ids
                        else None
                    )

                    if append_only:
                        existing_latest = list(
                            NdviObservation.objects.filter(
                                farm=farm,
                                engine=engine,
                                bucket_date=point.date,
                                is_latest=True,
                                index_type=index_type,
                            ).select_for_update()
                        )

                        if scene_id and prov_hash:
                            idempotent = NdviObservation.objects.filter(
                                farm=farm,
                                engine=engine,
                                index_type=index_type,
                                source_scene_id=scene_id,
                                provenance_hash=prov_hash,
                            ).first()
                        elif scene_id:
                            idempotent = NdviObservation.objects.filter(
                                farm=farm,
                                engine=engine,
                                index_type=index_type,
                                source_scene_id=scene_id,
                            ).first()
                        else:
                            idempotent = NdviObservation.objects.filter(
                                farm=farm,
                                engine=engine,
                                index_type=index_type,
                                bucket_date=point.date,
                                version=version,
                            ).first()

                        if idempotent:
                            ndvi_idempotent_hit_total.labels(
                                engine=engine
                            ).inc()
                            saved.append(idempotent)
                            continue

                        for row in existing_latest:
                            if row.can_transition_to(
                                NdviObservation.ObservationState.SUPERSEDED
                            ):
                                row.is_latest = False
                                row.state = (
                                    NdviObservation.ObservationState.SUPERSEDED
                                )
                                row.save(
                                    update_fields=[
                                        "is_latest",
                                        "state",
                                        "updated_at",
                                    ]
                                )
                                ndvi_supersession_total.labels(
                                    engine=engine
                                ).inc()

                        obj = NdviObservation.objects.create(
                            farm=farm,
                            engine=engine,
                            bucket_date=point.date,
                            mean=point.mean,
                            min=point.min,
                            max=point.max,
                            sample_count=point.sample_count,
                            cloud_fraction=point.cloud_fraction,
                            valid_pixel_fraction=point.valid_pixel_fraction,
                            quality_flags=quality_flags,
                            version=version,
                            state=state,
                            is_latest=True,
                            acquired_at=now,
                            computed_at=now,
                            ingested_at=now,
                            source_scene_id=scene_id,
                            provenance=validated_provenance,
                            provenance_hash=prov_hash,
                            index_type=index_type,
                        )
                        ndvi_append_only_writes_total.labels(
                            engine=engine
                        ).inc()
                    else:
                        defaults: dict[str, Any] = {
                            "mean": point.mean,
                            "min": point.min,
                            "max": point.max,
                            "sample_count": point.sample_count,
                            "cloud_fraction": point.cloud_fraction,
                            "valid_pixel_fraction": point.valid_pixel_fraction,
                            "quality_flags": quality_flags,
                            "version": version,
                            "state": state,
                            "is_latest": True,
                            "acquired_at": now,
                            "computed_at": now,
                            "ingested_at": now,
                        }
                        if scene_id:
                            defaults["source_scene_id"] = scene_id
                        if validated_provenance:
                            defaults["provenance"] = validated_provenance
                        if prov_hash:
                            defaults["provenance_hash"] = prov_hash

                        defaults["index_type"] = index_type
                        obj, _ = NdviObservation.objects.update_or_create(
                            farm=farm,
                            engine=engine,
                            index_type=index_type,
                            bucket_date=point.date,
                            defaults=defaults,
                        )
                    saved.append(obj)
            tx_duration = time.monotonic() - tx_start
            ndvi_transaction_duration_seconds.labels(
                operation="upsert"
            ).observe(tx_duration)
            return saved

        except IntegrityError as exc:
            if "unique constraint" not in str(exc).lower():
                raise
            _record_upsert_failure(engine)
            ndvi_lock_contention_total.labels(
                operation="upsert",
                reason="constraint_collision",
            ).inc()
            ndvi_retry_classification_total.labels(
                operation="upsert",
                class_="constraint_collision",
            ).inc()
            ndvi_retry_storm_window_total.labels(operation="upsert").inc()
            if _check_retry_circuit_breaker(engine):
                logger.error(
                    "ndvi.upsert.circuit_breaker_suppressing_retry "
                    "farm_id=%s engine=%s",
                    farm.id,
                    engine,
                )
                ndvi_recompute_failure_total.labels(
                    engine=engine,
                    reason="circuit_breaker",
                ).inc()
                ndvi_retry_classification_total.labels(
                    operation="upsert",
                    class_="circuit_breaker_suppressed",
                ).inc()
                tx_duration = time.monotonic() - tx_start
                ndvi_transaction_duration_seconds.labels(
                    operation="upsert"
                ).observe(tx_duration)
                raise
            if attempt < max_retries - 1:
                delay = base_delay * (2**attempt)
                jitter = (
                    secrets.randbelow(
                        int(get_ndvi_upsert_retry_jitter() * 1000)
                    )
                    / 1000.0
                )
                total_delay = delay + jitter
                logger.warning(
                    "ndvi.upsert.constraint_collision attempt=%d/%d "
                    "farm_id=%s engine=%s base_delay=%.3f "
                    "jitter=%.3f total_delay=%.3f",
                    attempt + 1,
                    max_retries,
                    farm.id,
                    engine,
                    delay,
                    jitter,
                    total_delay,
                )
                ndvi_constraint_collision_total.labels(
                    engine=engine,
                    constraint="upsert",
                ).inc()
                time.sleep(total_delay)
            else:
                logger.error(
                    "ndvi.upsert.constraint_collision_exhausted "
                    "farm_id=%s engine=%s",
                    farm.id,
                    engine,
                )
                ndvi_constraint_collision_total.labels(
                    engine=engine,
                    constraint="upsert",
                ).inc()
                ndvi_retry_classification_total.labels(
                    operation="upsert",
                    class_="retry_exhausted",
                ).inc()
                tx_duration = time.monotonic() - tx_start
                ndvi_transaction_duration_seconds.labels(
                    operation="upsert"
                ).observe(tx_duration)
                raise

    tx_duration = time.monotonic() - tx_start
    ndvi_transaction_duration_seconds.labels(operation="upsert").observe(
        tx_duration
    )

    # Starvation detection: transactions beyond 10 seconds
    if tx_duration > 10.0:
        ndvi_starvation_events_total.labels(operation="upsert").inc()
        logger.warning(
            "ndvi.upsert.starvation_detected duration=%.2fs "
            "farm_id=%s engine=%s",
            tx_duration,
            farm.id,
            engine,
        )

    # Long lock wait detection: beyond P95 threshold (2 seconds)
    if tx_duration > 2.0:
        ndvi_long_lock_wait_total.labels(
            operation="upsert",
            threshold_seconds="2",
        ).inc()

    return saved


def enqueue_job(
    *,
    owner_id: int,
    farm: Farm,
    engine_name: str | None = None,
    job_type: str,
    params: dict[str, Any],
    index_type: str = "NDVI",
) -> NdviJob:
    if job_type == NdviJob.JobType.RASTER_PNG:
        metrics_engine = resolve_ndvi_engine_name(None)
        resolved_engine = resolve_raster_engine_name(engine_name)
        raster_engine = resolved_engine
    else:
        resolved_engine = resolve_ndvi_engine_name(engine_name)
        metrics_engine = resolved_engine
        raster_engine = "-"
    engine_source = "override" if engine_name is not None else "default"
    request_hash = hash_request(
        engine=resolved_engine,
        owner_id=owner_id,
        farm_id=farm.id,
        params=params,
        index_type=index_type,
    )
    existing = NdviJob.objects.filter(
        owner_id=owner_id,
        farm=farm,
        engine=resolved_engine,
        request_hash=request_hash,
        index_type=index_type,
        status__in=[NdviJob.JobStatus.QUEUED, NdviJob.JobStatus.RUNNING],
    ).first()
    if existing:
        return existing

    job = NdviJob.objects.create(
        owner_id=owner_id,
        farm=farm,
        engine=resolved_engine,
        job_type=job_type,
        request_hash=request_hash,
        status=NdviJob.JobStatus.QUEUED,
        start=params.get("start"),
        end=params.get("end"),
        step_days=params.get("step_days"),
        max_cloud=params.get("max_cloud"),
        lookback_days=params.get("lookback_days"),
        index_type=index_type,
    )
    ndvi_jobs_total.labels(
        status=job.status, type=job_type, engine=resolved_engine
    ).inc()
    logger.info(
        "ndvi.job.enqueued job_id=%s type=%s metrics_engine=%s "
        "raster_engine=%s engine_source=%s",
        job.id,
        job_type,
        metrics_engine,
        raster_engine,
        engine_source,
    )
    return job


def dispatch_ndvi_job(job: NdviJob | int, *, queue: str | None = None) -> None:
    """Dispatch an NDVI job to the configured queue backend.

    When NDVI_QUEUE_BACKEND="stream", publishes to Redis stream.
    When NDVI_QUEUE_BACKEND="celery" (default), enqueues to Celery.

    Args:
        job: NdviJob instance or job ID to dispatch.
        queue: Optional Celery queue override. Defaults to
            ndvi_ingestion for normal jobs.
    """
    backend = get_ndvi_queue_backend()

    if backend == "stream":
        from .streams import publish_ndvi_job

        job_obj = (
            job
            if isinstance(job, NdviJob)
            else NdviJob.objects.select_related("farm", "owner").get(id=job)
        )
        publish_ndvi_job(job_obj)
        return

    from .tasks import run_ndvi_job, run_ndwi_job

    job_id = job.id if isinstance(job, NdviJob) else int(job)
    target_queue = queue or get_ndvi_queue_name("ingestion")
    task = (
        run_ndwi_job
        if isinstance(job, NdviJob) and job.index_type == "NDWI"
        else run_ndvi_job
    )
    task.apply_async(args=[job_id], queue=target_queue)


def dispatch_farm_state_coverage(
    *,
    farm_id: int,
    engine: str | None = None,
    target_date: date,
    threshold: float,
    queue: str | None = None,
) -> None:
    """Dispatch a farm state coverage job to the configured queue backend.

    Args:
        farm_id: ID of the farm to compute coverage for.
        engine: NDVI engine to use (default from settings).
        target_date: Target date for coverage computation.
        threshold: Coverage threshold for state classification.
        queue: Optional Celery queue override. Defaults to
            ndvi_analysis for coverage jobs.
    """
    backend = get_ndvi_queue_backend()

    if backend == "stream":
        from .streams import publish_farm_state_coverage as _publish_coverage

        _publish_coverage(
            farm_id=farm_id,
            engine=engine,
            target_date=target_date,
            threshold=threshold,
        )
        return

    from .tasks import compute_farm_state_coverage

    target_queue = queue or get_ndvi_queue_name("analysis")
    compute_farm_state_coverage.apply_async(
        kwargs={
            "farm_id": farm_id,
            "engine": engine,
            "target_date": target_date.isoformat(),
            "threshold": threshold,
        },
        queue=target_queue,
    )


def is_stale(observation: NdviObservation | None, lookback_days: int) -> bool:
    if observation is None:
        return True
    today = date.today()
    return (today - observation.bucket_date).days > lookback_days


def is_analytically_valid(
    observation: NdviObservation,
    *,
    min_version: str | None = None,
    allowed_engines: list[str] | None = None,
) -> bool:
    """Determine if an observation is analytically valid for computation.

    Canonical rule: an observation is valid if and only if:
    - state is FINAL (not RAW, SUPERSEDED, INVALIDATED, or REJECTED)
    - is_latest is True
    - mean is not None
    - version >= min_version (if specified, using semantic comparison)
    - engine in allowed_engines (if specified)

    This is the single source of truth for what counts as valid NDVI
    data across all read paths, recomputation, and chaining.
    """
    if observation.state != NdviObservation.ObservationState.FINAL:
        return False
    if not observation.is_latest:
        return False
    if observation.mean is None:
        return False
    if min_version and not version_gte(observation.version, min_version):
        return False
    if allowed_engines and observation.engine not in allowed_engines:
        return False
    return True


def get_valid_observations_qs(
    *,
    farm: Farm | None = None,
    engine: str | None = None,
    start: date | None = None,
    end: date | None = None,
    max_cloud: int | None = None,
    min_version: str | None = None,
    allowed_engines: list[str] | None = None,
) -> QuerySet[NdviObservation]:
    """Get a queryset of analytically valid observations.

    Centralized read path — all NDVI consumers MUST use this function
    instead of building their own observation queries. This prevents
    filter drift across services and ensures consistent validity rules.

    Excludes INVALIDATED, REJECTED, SUPERSEDED, and RAW rows.
    Only returns is_latest=True, state=FINAL rows with non-null mean.
    Optionally filters by min_version and allowed_engines.

    Note: min_version filtering uses DB string comparison which may
    have edge cases for versions like 'v10.0' vs 'v2.0'. For strict
    semantic version comparison, filter results in Python using
    is_analytically_valid(obs, min_version=...).
    """
    qs = (
        NdviObservation.objects.filter(
            is_latest=True,
            state=NdviObservation.ObservationState.FINAL,
        )
        .exclude(mean__isnull=True)
        .order_by("bucket_date")
    )

    if farm:
        qs = qs.filter(farm=farm)
    if engine:
        qs = qs.filter(engine=engine)
    if start:
        qs = qs.filter(bucket_date__gte=start)
    if end:
        qs = qs.filter(bucket_date__lte=end)
    if max_cloud is not None:
        qs = qs.filter(
            models.Q(cloud_fraction__isnull=True)
            | models.Q(cloud_fraction__lte=max_cloud / 100.0)
        )
    if min_version:
        qs = qs.filter(version__gte=min_version)
    if allowed_engines:
        qs = qs.filter(engine__in=allowed_engines)
    return qs


def get_latest_observations(
    *,
    farm: Farm,
    engine: str,
    start: date | None = None,
    end: date | None = None,
) -> list[NdviObservation]:
    """Get latest FINAL observations with deterministic ordering.

    Orders by bucket_date ascending, then by computed_at descending
    for same-date tie-breaking.
    """
    from ndvi.models import NdviObservation

    qs = NdviObservation.objects.filter(
        farm=farm,
        engine=engine,
        is_latest=True,
        state=NdviObservation.ObservationState.FINAL,
    ).order_by("bucket_date", "-computed_at")

    if start:
        qs = qs.filter(bucket_date__gte=start)
    if end:
        qs = qs.filter(bucket_date__lte=end)

    return list(qs)


def detect_anomalies(
    observations: list[NdviObservation],
    *,
    threshold: float | None = None,
) -> list[tuple[NdviObservation, str, float]]:
    """Detect NDVI anomalies using rolling median deviation.

    OPERATIONAL TELEMETRY ONLY — not user-facing agronomic logic.

    NDVI naturally shifts due to rainfall, harvesting, seasonal cycles,
    crop rotation, and drought. Static thresholds will generate false
    positives. For production agronomic use, this function needs:
    - seasonal baselines
    - crop-aware thresholds
    - historical trend windows
    - confidence scoring

    Returns list of (observation, reason, deviation) tuples.
    """
    if threshold is None:
        threshold = get_ndvi_anomaly_threshold()

    anomalies: list[tuple[NdviObservation, str, float]] = []
    values = [obs.mean for obs in observations if obs.mean is not None]

    if len(values) < 3:
        return anomalies

    sorted_vals = sorted(values)
    mid = len(sorted_vals) // 2
    rolling_median = float(sorted_vals[mid])

    for obs in observations:
        if obs.mean is None:
            continue
        deviation = abs(obs.mean - rolling_median)
        if deviation >= threshold:
            reason = "spike" if obs.mean > rolling_median else "drop"
            anomalies.append((obs, reason, deviation))

    return anomalies


def get_ndvi_version_registry() -> list[dict[str, str]]:
    """Return the structured version registry from settings.

    Each entry has: version, description, release_date, author.
    Falls back to deriving from NDVI_VERSION if registry not configured.
    """
    registry = getattr(settings, "NDVI_VERSION_REGISTRY", None)
    if registry:
        return registry
    return [
        {
            "version": get_ndvi_version(),
            "description": "Current NDVI computation version",
            "release_date": date.today().isoformat(),
            "author": "system",
        },
    ]


def _get_global_recompute_queue_depth() -> int:
    """Get the total queued recompute jobs from the database.

    Uses DB-level query as the authoritative source. Celery inspect
    is unreliable under load (workers may not respond, connections
    may timeout, etc.), so we depend on the database which is the
    single source of truth for job state.

    This is safe because NdviJob status is updated within
    transaction.atomic() blocks, so the DB count is always consistent.
    """
    return _get_db_recompute_queue_depth()


def _get_db_recompute_queue_depth() -> int:
    """Count queued/recompute jobs from the database.

    Returns total of QUEUED + RUNNING backfill jobs.
    For detailed breakdown, use get_recompute_queue_breakdown().
    """
    return NdviJob.objects.filter(
        status__in=[NdviJob.JobStatus.QUEUED, NdviJob.JobStatus.RUNNING],
        job_type=NdviJob.JobType.BACKFILL,
    ).count()


def get_recompute_queue_breakdown() -> dict[str, int]:
    """Get a detailed breakdown of recompute queue state.

    Returns dict with:
    - queued: jobs waiting to be picked up
    - running: jobs currently executing
    - stuck: running jobs past their time limit
    - total: queued + running

    Stuck detection uses task_time_limit (300s default) as threshold.
    """
    from django.utils import timezone

    time_limit = int(getattr(settings, "CELERY_TASK_TIME_LIMIT", 300))
    stuck_threshold = timezone.now() - timedelta(seconds=time_limit)

    queued = NdviJob.objects.filter(
        status=NdviJob.JobStatus.QUEUED,
        job_type=NdviJob.JobType.BACKFILL,
    ).count()
    running = NdviJob.objects.filter(
        status=NdviJob.JobStatus.RUNNING,
        job_type=NdviJob.JobType.BACKFILL,
    ).count()
    stuck = NdviJob.objects.filter(
        status=NdviJob.JobStatus.RUNNING,
        job_type=NdviJob.JobType.BACKFILL,
        started_at__lt=stuck_threshold,
    ).count()

    return {
        "queued": queued,
        "running": running,
        "stuck": stuck,
        "total": queued + running,
    }


def recompute_stale_observations(
    *,
    engine: str,
    start_date: date,
    end_date: date,
    chunk_size: int | None = None,
    max_window_days: int | None = None,
    target_version: str | None = None,
    provenance_schema_version: str | None = None,
    engine_config_hash: str | None = None,
) -> list[dict[str, Any]]:
    """Find observations that need recomputation.

    Returns list of dicts with farm_id, bucket_date, current_version,
    ready for dispatch via dispatch_ndvi_job().

    Implements bounded windows, chunking, and backpressure controls.
    Backpressure uses DB-level query (not Celery inspect) as the
    authoritative source.

    Idempotency model:
    - Intent identity: (farm_id, engine, bucket_date, target_version,
      provenance_schema_version, engine_config_hash) defines WHAT should
      be computed. Same intent = same result.
    - Execution identity: dispatch_key is derived from intent identity.
      Same dispatch_key → same NdviJob.request_hash → no duplicates.
    - Execution parameters (chunk_size, max_window_days) do NOT affect
      identity — they only control HOW the recompute is performed.
    """
    max_days = max_window_days or get_ndvi_recompute_max_window_days()
    if (end_date - start_date).days > max_days:
        raise ValueError(
            f"Recompute window {start_date}..{end_date} exceeds "
            f"max {max_days} days"
        )

    chunk = chunk_size or get_ndvi_recompute_chunk_size()
    target_ver = target_version or get_ndvi_version()
    schema_ver = provenance_schema_version or "1"
    config_hash = engine_config_hash or ""

    stale_qs = (
        NdviObservation.objects.filter(
            engine=engine,
            bucket_date__gte=start_date,
            bucket_date__lte=end_date,
            is_latest=True,
        )
        .exclude(version=target_ver)
        .values("farm_id", "bucket_date", "version", "id")
        .order_by("bucket_date", "farm_id")[: chunk * 10]
    )

    queued_jobs = _get_global_recompute_queue_depth()

    backpressure_limit = get_ndvi_recompute_backpressure_threshold()
    if queued_jobs >= backpressure_limit:
        logger.warning(
            "ndvi.recompute.backpressure queued=%d limit=%d",
            queued_jobs,
            backpressure_limit,
        )
        return []

    results: list[dict[str, Any]] = []
    for row in stale_qs[:chunk]:
        dispatch_key = hashlib.sha256(
            f"recompute:{row['farm_id']}:{engine}:"
            f"{row['bucket_date']}:{target_ver}:"
            f"{schema_ver}:{config_hash}".encode()
        ).hexdigest()[:16]
        results.append(
            {
                "farm_id": row["farm_id"],
                "bucket_date": row["bucket_date"],
                "current_version": row["version"],
                "target_version": target_ver,
                "observation_id": row["id"],
                "dispatch_key": dispatch_key,
                "provenance_schema_version": schema_ver,
                "engine_config_hash": config_hash,
            }
        )

    logger.info(
        "ndvi.recompute.stale_found engine=%s window=%s..%s count=%d",
        engine,
        start_date,
        end_date,
        len(results),
    )
    return results
