"""NDMI farm state metrics and classification.

NDMI farm state classifies fields by moisture content using the
Normalized Difference Moisture Index. States are based on mean
NDMI thresholds:
- DRY: mean NDMI < -0.2 (low moisture / water stress)
- MOIST: mean NDMI between -0.2 and 0.2 (moderate moisture)
- SATURATED: mean NDMI > 0.2 (high moisture)
- WATER: mean NDMI > 0.3 (very wet / saturated soil)
- DECLINING: trend is negative from a previously moist state
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
        "Low moisture content detected - field is dry.",
        "Irrigation may be needed if crops are present.",
    ),
    STATE_MOIST: (
        "Moderate moisture levels detected.",
        "Continue monitoring moisture trend.",
    ),
    STATE_SATURATED: (
        "High moisture detected.",
        "Monitor drainage and avoid over-irrigation.",
    ),
    STATE_WATER: (
        "Very wet / saturated soil detected on field.",
        "Check for flooding or ponding.",
    ),
    STATE_DECLINING: (
        "Moisture is declining from previously wetter state.",
        "Consider irrigation scheduling if trend continues.",
    ),
    STATE_UNKNOWN: (
        "Insufficient NDMI data to classify field moisture.",
        "Collect additional observations and retry.",
    ),
}


@dataclass(frozen=True)
class NdmiFarmStateResult:
    farm_id: int
    mean_ndmi: float | None
    max_ndmi: float | None
    min_ndmi: float | None
    trend: float | None
    state: str
    interpretation: str
    action: str


def _get_trend_window_days() -> int:
    return int(getattr(settings, "NDMI_TREND_WINDOW_DAYS", 30))


def _get_dry_threshold() -> float:
    return float(getattr(settings, "NDMI_DRY_THRESHOLD", -0.2))


def _get_saturated_threshold() -> float:
    return float(getattr(settings, "NDMI_SATURATED_THRESHOLD", 0.2))


def _get_water_threshold() -> float:
    return float(getattr(settings, "NDMI_WATER_THRESHOLD", 0.3))


def _classify_ndmi_state(
    mean_ndmi: float | None,
    trend: float | None,
) -> tuple[str, str, str]:
    if mean_ndmi is None:
        state = STATE_UNKNOWN
    elif mean_ndmi > _get_water_threshold():
        state = STATE_WATER
    elif mean_ndmi > _get_saturated_threshold():
        state = STATE_SATURATED
    elif mean_ndmi < _get_dry_threshold():
        state = STATE_DRY
    elif trend is not None and trend < -0.01:
        state = STATE_DECLINING
    else:
        state = STATE_MOIST
    interpretation, action = STATE_METADATA[state]
    return state, interpretation, action


def compute_ndmi_farm_state(
    *,
    farm: Farm,
    engine: str | None = None,
) -> NdmiFarmStateResult:
    """Compute farm state from NDMI observations.

    Args:
        farm: The farm to compute state for.
        engine: Optional engine name. Defaults to stac.

    Returns:
        NdmiFarmStateResult with moisture classification.
    """
    resolved_engine = engine or "stac"
    window_days = _get_trend_window_days()
    end = date.today()
    start = end - timedelta(days=window_days)

    observations = list(
        NdviObservation.objects.filter(
            farm=farm,
            engine=resolved_engine,
            index_type="NDMI",
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
        max_cloud=int(getattr(settings, "NDMI_DEFAULT_MAX_CLOUD", 30)),
    )

    vals = [o.mean for o in observations if o.mean is not None]
    mean_ndmi = float(sum(vals) / len(vals)) if vals else None
    max_ndmi = max(vals) if vals else None
    min_ndmi = min(vals) if vals else None

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

    state, interpretation, action = _classify_ndmi_state(mean_ndmi, trend)

    return NdmiFarmStateResult(
        farm_id=farm.id,
        mean_ndmi=mean_ndmi,
        max_ndmi=max_ndmi,
        min_ndmi=min_ndmi,
        trend=trend,
        state=state,
        interpretation=interpretation,
        action=action,
    )
