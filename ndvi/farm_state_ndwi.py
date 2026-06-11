"""NDWI farm state metrics and classification.

NDWI farm state classifies fields by water content rather than vegetation
vigor. States are based on mean NDWI thresholds:
- DRY: mean NDWI < -0.15 (low water content)
- MOIST: mean NDWI between -0.15 and 0.15 (moderate moisture)
- SATURATED: mean NDWI > 0.15 (high water / surface moisture)
- WATER: mean NDWI > 0.3 (open water detected)
- DECLINING: trend is negative from a previously wet state
- UNKNOWN: insufficient data
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from django.conf import settings

from farms.models import Farm
from ndvi.models import NdviObservation
from ndvi.services import filter_observations_by_cloud

logger = logging.getLogger(__name__)

STATE_DRY = "dry"
STATE_MOIST = "moist"
STATE_SATURATED = "saturated"
STATE_WATER = "water"
STATE_DECLINING = "declining"
STATE_UNKNOWN = "unknown"

STATE_METADATA: dict[str, tuple[str, str]] = {
    STATE_DRY: (
        "Low water content detected - field is dry.",
        "Irrigation may be needed if crops are present.",
    ),
    STATE_MOIST: (
        "Moderate moisture levels detected.",
        "Continue monitoring moisture trend.",
    ),
    STATE_SATURATED: (
        "High moisture / surface water detected.",
        "Monitor drainage and avoid over-irrigation.",
    ),
    STATE_WATER: (
        "Open water detected on field.",
        "Check for flooding or ponding.",
    ),
    STATE_DECLINING: (
        "Moisture is declining from previously wetter state.",
        "Consider irrigation scheduling if trend continues.",
    ),
    STATE_UNKNOWN: (
        "Insufficient NDWI data to classify field moisture.",
        "Collect additional observations and retry.",
    ),
}


@dataclass(frozen=True)
class NdwiFarmStateResult:
    farm_id: int
    mean_ndwi: float | None
    max_ndwi: float | None
    min_ndwi: float | None
    trend: float | None
    state: str
    interpretation: str
    action: str


def _get_trend_window_days() -> int:
    return int(getattr(settings, "NDWI_TREND_WINDOW_DAYS", 30))


def _get_dry_threshold() -> float:
    return float(getattr(settings, "NDWI_DRY_THRESHOLD", -0.15))


def _get_saturated_threshold() -> float:
    return float(getattr(settings, "NDWI_SATURATED_THRESHOLD", 0.15))


def _get_water_threshold() -> float:
    return float(getattr(settings, "NDWI_WATER_THRESHOLD", 0.3))


def _classify_ndwi_state(
    mean_ndwi: float | None,
    trend: float | None,
) -> tuple[str, str, str]:
    if mean_ndwi is None:
        state = STATE_UNKNOWN
    elif mean_ndwi > _get_water_threshold():
        state = STATE_WATER
    elif mean_ndwi > _get_saturated_threshold():
        state = STATE_SATURATED
    elif mean_ndwi < _get_dry_threshold():
        state = STATE_DRY
    elif trend is not None and trend < -0.01:
        state = STATE_DECLINING
    else:
        state = STATE_MOIST
    interpretation, action = STATE_METADATA[state]
    return state, interpretation, action


def compute_ndwi_farm_state(
    *,
    farm: Farm,
    engine: str | None = None,
) -> NdwiFarmStateResult:
    """Compute farm state from NDWI observations.

    Args:
        farm: The farm to compute state for.
        engine: Optional engine name. Defaults to stac.

    Returns:
        NdwiFarmStateResult with moisture classification.
    """
    resolved_engine = engine or "stac"
    window_days = _get_trend_window_days()
    end = date.today()
    start = end - timedelta(days=window_days)

    observations = list(
        NdviObservation.objects.filter(
            farm=farm,
            engine=resolved_engine,
            index_type="NDWI",
            bucket_date__gte=start,
            bucket_date__lte=end,
            is_latest=True,
            state=NdviObservation.ObservationState.FINAL,
        )
        .exclude(mean__isnull=True)
        .order_by("bucket_date")
    )
    observations = filter_observations_by_cloud(
        observations,
        max_cloud=int(getattr(settings, "NDWI_DEFAULT_MAX_CLOUD", 30)),
    )

    vals = [o.mean for o in observations if o.mean is not None]
    mean_ndwi = float(sum(vals) / len(vals)) if vals else None
    max_ndwi = max(vals) if vals else None
    min_ndwi = min(vals) if vals else None

    trend: float | None = None
    if len(vals) >= 2:
        xs = list(range(len(vals)))
        n = len(xs)
        sx = sum(xs)
        sy = sum(vals)
        sxx = sum(x * x for x in xs)
        sxy = sum(x * y for x, y in zip(xs, vals, strict=False))
        denom = n * sxx - sx * sx
        if denom != 0:
            trend = (n * sxy - sx * sy) / denom

    state, interpretation, action = _classify_ndwi_state(mean_ndwi, trend)

    return NdwiFarmStateResult(
        farm_id=farm.id,
        mean_ndwi=mean_ndwi,
        max_ndwi=max_ndwi,
        min_ndwi=min_ndwi,
        trend=trend,
        state=state,
        interpretation=interpretation,
        action=action,
    )
