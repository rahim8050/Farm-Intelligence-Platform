"""Farm state metrics and classification services.

Derived from NDVI observations stored in the system. This module performs
read-only aggregation and classification; it does not mutate state.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
from django.conf import settings
from django.core.cache import caches
from rest_framework.exceptions import ValidationError

from config.api.responses import JSONValue
from farms.models import Farm
from ndvi.engines.base import BBox
from ndvi.engines.stac import (
    get_default_asset_nir,
    get_default_asset_red,
    get_default_date_window_days,
    get_default_timeout_seconds,
)
from ndvi.models import NdviObservation
from ndvi.services import (
    dispatch_farm_state_coverage,
    enforce_quota,
    filter_observations_by_cloud,
    get_default_max_cloud,
    get_default_ndvi_engine_name,
    normalize_bbox,
)
from ndvi.stac_client import (
    DEFAULT_STATS_SAMPLE_SIZE,
    StacClient,
    StacDependencyError,
    StacError,
    StacProcessingError,
    StacUpstreamError,
    build_asset_candidates,
    load_ndvi_array,
    resolve_asset_href_candidates,
    select_best_item,
)

logger = logging.getLogger(__name__)

DEFAULT_TREND_WINDOW_DAYS = 30
MIN_TREND_WINDOW_DAYS = 30
MAX_TREND_WINDOW_DAYS = 60

DEFAULT_COVERAGE_TTL_SECONDS = 21600
DEFAULT_COVERAGE_LOCK_SECONDS = 600
DEFAULT_FARM_STATE_TTL_SECONDS = 21600
DEFAULT_FARM_STATE_LOCK_SECONDS = 30

DEFAULT_ESTABLISHMENT_MEAN_THRESHOLD = 0.25
DEFAULT_ESTABLISHMENT_MAX_THRESHOLD = 0.4
DEFAULT_FULL_CANOPY_MEAN_THRESHOLD = 0.4
DEFAULT_COVERAGE_THRESHOLD = 0.3

STATE_ESTABLISHMENT = "establishment"
STATE_FULL_CANOPY = "full_canopy"
STATE_DECLINE = "decline"
STATE_GROWTH = "growth"
STATE_UNKNOWN = "unknown"

STATE_METADATA: dict[str, tuple[str, str]] = {
    STATE_ESTABLISHMENT: (
        "Sparse but healthy vegetation detected.",
        "Continue monitoring crop establishment.",
    ),
    STATE_FULL_CANOPY: (
        "Dense canopy detected with strong vegetation vigor.",
        "Maintain current management and monitor for stress.",
    ),
    STATE_DECLINE: (
        "NDVI trend is declining, indicating potential stress.",
        "Inspect the field and address potential stress factors.",
    ),
    STATE_GROWTH: (
        "Vegetation growth is progressing steadily.",
        "Continue regular monitoring and management.",
    ),
    STATE_UNKNOWN: (
        "Insufficient NDVI data to classify farm state.",
        "Collect additional NDVI observations and retry.",
    ),
}


@dataclass(frozen=True)
class FarmStateResult:
    farm_id: int
    mean_ndvi: float | None
    max_ndvi: float | None
    coverage_pct: float | None
    trend: float | None
    state: str
    interpretation: str
    action: str

    def as_payload(self) -> dict[str, JSONValue]:
        return {
            "farm_id": self.farm_id,
            "mean_ndvi": self.mean_ndvi,
            "max_ndvi": self.max_ndvi,
            "coverage_pct": self.coverage_pct,
            "trend": self.trend,
            "state": self.state,
            "interpretation": self.interpretation,
            "action": self.action,
        }


def get_trend_window_days() -> int:
    raw = int(
        getattr(
            settings,
            "FARM_STATE_TREND_WINDOW_DAYS",
            DEFAULT_TREND_WINDOW_DAYS,
        )
    )
    return max(MIN_TREND_WINDOW_DAYS, min(raw, MAX_TREND_WINDOW_DAYS))


def get_coverage_cache_ttl_seconds() -> int:
    return int(
        getattr(
            settings,
            "FARM_STATE_COVERAGE_TTL_SECONDS",
            DEFAULT_COVERAGE_TTL_SECONDS,
        )
    )


def get_coverage_lock_seconds() -> int:
    return int(
        getattr(
            settings,
            "FARM_STATE_COVERAGE_LOCK_SECONDS",
            DEFAULT_COVERAGE_LOCK_SECONDS,
        )
    )


def get_farm_state_cache_ttl_seconds() -> int:
    return int(
        getattr(
            settings,
            "FARM_STATE_CACHE_TTL_SECONDS",
            DEFAULT_FARM_STATE_TTL_SECONDS,
        )
    )


def get_farm_state_lock_seconds() -> int:
    return int(
        getattr(
            settings,
            "FARM_STATE_LOCK_SECONDS",
            DEFAULT_FARM_STATE_LOCK_SECONDS,
        )
    )


def get_establishment_mean_threshold() -> float:
    return float(
        getattr(
            settings,
            "FARM_STATE_ESTABLISHMENT_MEAN_THRESHOLD",
            DEFAULT_ESTABLISHMENT_MEAN_THRESHOLD,
        )
    )


def get_establishment_max_threshold() -> float:
    return float(
        getattr(
            settings,
            "FARM_STATE_ESTABLISHMENT_MAX_THRESHOLD",
            DEFAULT_ESTABLISHMENT_MAX_THRESHOLD,
        )
    )


def get_full_canopy_mean_threshold() -> float:
    return float(
        getattr(
            settings,
            "FARM_STATE_FULL_CANOPY_MEAN_THRESHOLD",
            DEFAULT_FULL_CANOPY_MEAN_THRESHOLD,
        )
    )


def get_coverage_threshold() -> float:
    raw = float(
        getattr(
            settings,
            "FARM_STATE_COVERAGE_THRESHOLD",
            DEFAULT_COVERAGE_THRESHOLD,
        )
    )
    return max(0.0, min(raw, 1.0))


def _default_max_cloud_for_engine(engine: str) -> int:
    if engine == "stac":
        return int(getattr(settings, "NDVI_STAC_MAX_CLOUD_DEFAULT", 30))
    return get_default_max_cloud()


def _coverage_cache_key(
    *, farm_id: int, engine: str, target_date: date, threshold: float
) -> str:
    threshold_key = f"{threshold:.3f}"
    return (
        f"farm_state:coverage:{farm_id}:{engine}:{target_date}:{threshold_key}"
    )


def _coverage_lock_key(
    *, farm_id: int, engine: str, target_date: date, threshold: float
) -> str:
    threshold_key = f"{threshold:.3f}"
    return (
        "farm_state:coverage:lock:"
        f"{farm_id}:{engine}:{target_date}:{threshold_key}"
    )


def _farm_state_cache_key(*, farm_id: int, engine: str) -> str:
    return f"farm_state:{farm_id}:{engine}"


def _get_cached_coverage(
    *, farm_id: int, engine: str, target_date: date, threshold: float
) -> tuple[bool, float | None]:
    cache = caches["default"]
    cached = cache.get(
        _coverage_cache_key(
            farm_id=farm_id,
            engine=engine,
            target_date=target_date,
            threshold=threshold,
        )
    )
    if isinstance(cached, dict) and "value" in cached:
        return True, cached["value"]
    return False, None


def _set_cached_coverage(
    *,
    farm_id: int,
    engine: str,
    target_date: date,
    threshold: float,
    value: float | None,
) -> None:
    cache = caches["default"]
    cache.set(
        _coverage_cache_key(
            farm_id=farm_id,
            engine=engine,
            target_date=target_date,
            threshold=threshold,
        ),
        {"value": value},
        timeout=get_coverage_cache_ttl_seconds(),
    )


def _acquire_coverage_lock(
    *, farm_id: int, engine: str, target_date: date, threshold: float
) -> bool:
    cache = caches["default"]
    return bool(
        cache.add(
            _coverage_lock_key(
                farm_id=farm_id,
                engine=engine,
                target_date=target_date,
                threshold=threshold,
            ),
            "1",
            timeout=get_coverage_lock_seconds(),
        )
    )


def _enqueue_coverage_compute(
    *, farm_id: int, engine: str, target_date: date, threshold: float
) -> None:
    dispatch_farm_state_coverage(
        farm_id=farm_id,
        engine=engine,
        target_date=target_date,
        threshold=threshold,
    )


def _compute_mean_ndvi(
    observations: Iterable[NdviObservation],
) -> float | None:
    values = [obs.mean for obs in observations]
    if not values:
        return None
    return sum(values) / len(values)


def _compute_max_ndvi(observations: Iterable[NdviObservation]) -> float | None:
    values = [
        obs.max if obs.max is not None else obs.mean for obs in observations
    ]
    if not values:
        return None
    return max(values)


def _compute_trend_slope(
    observations: list[NdviObservation],
) -> float | None:
    if len(observations) < 2:
        return None
    xs = [obs.bucket_date.toordinal() for obs in observations]
    ys = [obs.mean for obs in observations]
    count = len(xs)
    mean_x = sum(xs) / count
    mean_y = sum(ys) / count
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    numer = sum(
        (x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)
    )
    return numer / denom


def _coverage_pct_from_ndvi_array(
    ndvi: np.ndarray, *, threshold: float
) -> float | None:
    if ndvi.size == 0:
        return None
    valid_mask = np.isfinite(ndvi)
    total = int(np.count_nonzero(valid_mask))
    if total == 0:
        return None
    above = int(np.count_nonzero((ndvi > threshold) & valid_mask))
    return (above / total) * 100.0


def _compute_stac_coverage_pct(
    *,
    farm_id: int,
    bbox: BBox,
    target_date: date,
    max_cloud: int,
    threshold: float,
) -> float | None:
    client = StacClient(timeout_seconds=get_default_timeout_seconds())
    window_days = get_default_date_window_days()
    search_start = target_date - timedelta(days=window_days)
    search_end = target_date + timedelta(days=window_days)
    items = client.search(
        bbox=bbox,
        start=search_start,
        end=search_end,
        max_cloud=max_cloud,
        farm_id=farm_id,
    )
    item = select_best_item(
        items,
        target_date=target_date,
        window_days=window_days,
    )
    if not item:
        return None
    red_candidates = build_asset_candidates(get_default_asset_red())
    nir_candidates = build_asset_candidates(get_default_asset_nir())
    red_href = resolve_asset_href_candidates(item, red_candidates)
    nir_href = resolve_asset_href_candidates(item, nir_candidates)
    if not red_href or not nir_href:
        return None
    ndvi = load_ndvi_array(
        red_href=red_href,
        nir_href=nir_href,
        bbox=bbox,
        size=DEFAULT_STATS_SAMPLE_SIZE,
        timeout_seconds=get_default_timeout_seconds(),
    )
    return _coverage_pct_from_ndvi_array(ndvi, threshold=threshold)


def _compute_coverage_value(
    *,
    farm: Farm,
    engine: str,
    target_date: date,
    threshold: float,
) -> float | None:
    try:
        bbox = normalize_bbox(farm)
        enforce_quota(farm, bbox)
    except ValidationError:
        return None
    max_cloud = _default_max_cloud_for_engine(engine)
    try:
        return _compute_stac_coverage_pct(
            farm_id=farm.id,
            bbox=bbox,
            target_date=target_date,
            max_cloud=max_cloud,
            threshold=threshold,
        )
    except (
        StacDependencyError,
        StacProcessingError,
        StacUpstreamError,
        ValueError,
    ) as exc:
        logger.warning(
            "farm_state.coverage_stac_error farm_id=%s error=%s",
            farm.id,
            exc,
        )
        return None
    except StacError as exc:
        logger.debug(
            "farm_state.coverage_stac_skipped farm_id=%s error=%s",
            farm.id,
            exc,
        )
        return None


def _compute_coverage_pct(
    *,
    farm: Farm,
    engine: str,
    observations: list[NdviObservation],
) -> float | None:
    """Compute coverage percentage for a farm.

    Coverage is cached for FARM_STATE_COVERAGE_TTL_SECONDS (default: 6h).
    When the cache misses, this function returns ``None`` rather than
    dispatching a Celery task. Coverage is pre-computed daily by Celery
    Beat (``farm-state-daily-coverage`` at 03:45) which populates the
    cache before users poll the endpoint.

    This ensures the GET endpoint is strictly read-only with no side effects.
    """
    if engine != "stac":
        return None
    if not observations:
        return None
    target_date = observations[-1].bucket_date
    threshold = get_coverage_threshold()
    cached, value = _get_cached_coverage(
        farm_id=farm.id,
        engine=engine,
        target_date=target_date,
        threshold=threshold,
    )
    if cached:
        return value
    return None


def compute_coverage_for_farm(
    *, farm: Farm, engine: str, target_date: date, threshold: float
) -> float | None:
    return _compute_coverage_value(
        farm=farm, engine=engine, target_date=target_date, threshold=threshold
    )


def cache_coverage_for_farm(
    *,
    farm_id: int,
    engine: str,
    target_date: date,
    threshold: float,
    value: float | None,
) -> None:
    _set_cached_coverage(
        farm_id=farm_id,
        engine=engine,
        target_date=target_date,
        threshold=threshold,
        value=value,
    )


def get_cached_coverage_for_farm(
    *, farm_id: int, engine: str, target_date: date, threshold: float
) -> tuple[bool, float | None]:
    return _get_cached_coverage(
        farm_id=farm_id,
        engine=engine,
        target_date=target_date,
        threshold=threshold,
    )


def classify_farm_state(
    *,
    mean_ndvi: float | None,
    max_ndvi: float | None,
    trend: float | None,
) -> tuple[str, str, str]:
    if mean_ndvi is None or max_ndvi is None:
        state = STATE_UNKNOWN
    else:
        if mean_ndvi < get_establishment_mean_threshold() and max_ndvi > (
            get_establishment_max_threshold()
        ):
            state = STATE_ESTABLISHMENT
        elif mean_ndvi > get_full_canopy_mean_threshold():
            state = STATE_FULL_CANOPY
        elif trend is not None and trend < 0:
            state = STATE_DECLINE
        else:
            state = STATE_GROWTH
    interpretation, action = STATE_METADATA[state]
    return state, interpretation, action


def build_farm_state(
    *,
    farm: Farm,
    engine: str | None = None,
) -> FarmStateResult:
    """Build and cache the derived farm state.

    On cache hit, returns the cached result without any database queries.
    On cache miss, uses a lightweight distributed lock to prevent
    cache stampede (only one caller recomputes; others fall through
    to compute without caching on lock contention).

    TTL: ``FARM_STATE_CACHE_TTL_SECONDS`` (default: 6h).
    Lock: ``cache.add`` with 30s auto-expiry (prevents deadlocks).
    """
    resolved_engine = (
        engine if engine is not None else get_default_ndvi_engine_name()
    )
    cache_key = _farm_state_cache_key(farm_id=farm.id, engine=resolved_engine)
    cached = caches["default"].get(cache_key)
    if isinstance(cached, dict) and "farm_id" in cached:
        return FarmStateResult(**cached)

    # Cache miss — try to acquire a short-lived compute lock
    lock_key = f"{cache_key}:lock"
    acquired = caches["default"].add(
        lock_key, "1", timeout=get_farm_state_lock_seconds()
    )
    if acquired:
        # We won the lock — compute and cache
        result = _compute_farm_state(farm=farm, engine=resolved_engine)
        caches["default"].set(
            cache_key,
            result.as_payload(),
            timeout=get_farm_state_cache_ttl_seconds(),
        )
        caches["default"].delete(lock_key)
        return result

    # Lock held by another request — try cache again briefly
    cached = caches["default"].get(cache_key)
    if isinstance(cached, dict) and "farm_id" in cached:
        return FarmStateResult(**cached)

    # Still no cache value — compute without caching to avoid stale data
    return _compute_farm_state(farm=farm, engine=resolved_engine)


def _compute_farm_state(
    *,
    farm: Farm,
    engine: str,
) -> FarmStateResult:
    """Compute farm state from observations (no cache logic).

    Phase 4: Only reads FINAL, latest observations.
    """
    from ndvi.models import NdviObservation

    window_days = get_trend_window_days()
    end = date.today()
    start = end - timedelta(days=window_days)

    observations = list(
        NdviObservation.objects.filter(
            farm=farm,
            engine=engine,
            bucket_date__gte=start,
            bucket_date__lte=end,
            is_latest=True,
            state=NdviObservation.ObservationState.FINAL,
        ).order_by("bucket_date")
    )
    observations = filter_observations_by_cloud(
        observations,
        max_cloud=_default_max_cloud_for_engine(engine),
    )

    mean_ndvi = _compute_mean_ndvi(observations)
    max_ndvi = _compute_max_ndvi(observations)
    trend = _compute_trend_slope(observations)
    coverage_pct = _compute_coverage_pct(
        farm=farm, engine=engine, observations=observations
    )

    state, interpretation, action = classify_farm_state(
        mean_ndvi=mean_ndvi,
        max_ndvi=max_ndvi,
        trend=trend,
    )

    return FarmStateResult(
        farm_id=farm.id,
        mean_ndvi=mean_ndvi,
        max_ndvi=max_ndvi,
        coverage_pct=coverage_pct,
        trend=trend,
        state=state,
        interpretation=interpretation,
        action=action,
    )


def invalidate_farm_state_cache(*, farm_id: int, engine: str) -> None:
    """Delete the cached farm state result.

    Called after the coverage Celery task completes so the next GET
    returns fresh data with the updated coverage_pct.
    """
    caches["default"].delete(
        _farm_state_cache_key(farm_id=farm_id, engine=engine)
    )
