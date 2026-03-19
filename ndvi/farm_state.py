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
    enforce_quota,
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


def _compute_coverage_pct(
    *,
    farm: Farm,
    engine: str,
    observations: list[NdviObservation],
) -> float | None:
    if engine != "stac":
        return None
    if not observations:
        return None
    try:
        bbox = normalize_bbox(farm)
        enforce_quota(farm, bbox)
    except ValidationError:
        return None
    max_cloud = _default_max_cloud_for_engine(engine)
    threshold = get_coverage_threshold()
    target_date = observations[-1].bucket_date
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
    resolved_engine = (
        engine if engine is not None else get_default_ndvi_engine_name()
    )
    window_days = get_trend_window_days()
    end = date.today()
    start = end - timedelta(days=window_days)

    observations = list(
        NdviObservation.objects.filter(
            farm=farm,
            engine=resolved_engine,
            bucket_date__gte=start,
            bucket_date__lte=end,
        ).order_by("bucket_date")
    )

    mean_ndvi = _compute_mean_ndvi(observations)
    max_ndvi = _compute_max_ndvi(observations)
    trend = _compute_trend_slope(observations)
    coverage_pct = _compute_coverage_pct(
        farm=farm, engine=resolved_engine, observations=observations
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
