from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Final, Literal, NoReturn

import numpy as np
from django.conf import settings
from rest_framework.exceptions import ValidationError

from ndvi.stac_client import (
    StacClient,
    StacItem,
    StacProcessingError,
    StacUpstreamError,
    build_asset_candidates,
    compute_ndvi_stats,
    load_ndvi_array,
    normalize_stac_bbox,
    resolve_asset_href_candidates,
)

from .base import NdviRasterEngine, RasterRequest
from .png import ndvi_to_png_bytes

DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
DEFAULT_DATE_WINDOW_DAYS: Final[int] = 3
DEFAULT_ASSET_RED: Final[str] = "B04_10m"
DEFAULT_ASSET_NIR: Final[str] = "B08_10m"
RASTER_NOT_FOUND_MESSAGE: Final[str] = "Raster not found"
RASTER_NOT_FOUND_CODE: Final[str] = "raster_not_found"
RasterNotFoundReason = Literal["no_items", "no_best_item", "missing_assets"]

logger = logging.getLogger(__name__)


def get_default_timeout_seconds() -> float:
    return float(
        getattr(settings, "NDVI_STAC_TIMEOUT_SECS", DEFAULT_TIMEOUT_SECONDS)
    )


def get_default_date_window_days() -> int:
    return int(
        getattr(
            settings, "NDVI_STAC_DATE_WINDOW_DAYS", DEFAULT_DATE_WINDOW_DAYS
        )
    )


def get_default_asset_red() -> str:
    return str(getattr(settings, "NDVI_STAC_ASSET_RED", DEFAULT_ASSET_RED))


def get_default_asset_nir() -> str:
    return str(getattr(settings, "NDVI_STAC_ASSET_NIR", DEFAULT_ASSET_NIR))


def _raise_raster_not_found(
    *,
    reason: RasterNotFoundReason,
    request: RasterRequest,
    window_days: int,
    item: StacItem | None,
    items_count: int | None = None,
    collections: list[str] | None = None,
    item_collection: str | None = None,
    available_assets: list[str] | None = None,
    expected_assets: dict[str, list[str]] | None = None,
) -> NoReturn:
    window = timedelta(days=window_days)
    bbox_values = normalize_stac_bbox(
        request.bbox,
        farm_id=request.farm_id,
        job_id=request.job_id,
        log_on_swap=False,
    )
    logger.warning(
        "ndvi.raster.not_found reason=%s job_id=%s farm_id=%s bbox_stac=%s "
        "start=%s end=%s max_cloud=%s window_days=%s item_id=%s "
        "item_collection=%s items_count=%s collections=%s "
        "available_assets=%s expected_assets=%s",
        reason,
        request.job_id if request.job_id is not None else "-",
        request.farm_id if request.farm_id is not None else "-",
        bbox_values,
        request.date - window,
        request.date + window,
        request.max_cloud,
        window_days,
        item.id if item is not None else "-",
        item_collection or "-",
        items_count if items_count is not None else "-",
        collections if collections is not None else "-",
        available_assets if available_assets is not None else "-",
        expected_assets if expected_assets is not None else "-",
    )
    raise ValidationError(
        {
            "detail": RASTER_NOT_FOUND_MESSAGE,
            "code": RASTER_NOT_FOUND_CODE,
            "reason": reason,
        }
    )


def _rank_candidate_items(
    items: list[StacItem],
    *,
    target_date: date,
    window_days: int,
) -> list[StacItem]:
    ranked: list[tuple[float, int, datetime, StacItem]] = []
    for item in items:
        delta_days = abs((item.date - target_date).days)
        if delta_days > window_days:
            continue
        cloud_rank = (
            item.cloud_cover if item.cloud_cover is not None else 101.0
        )
        ranked.append((cloud_rank, delta_days, item.datetime, item))
    ranked.sort(key=lambda row: (row[0], row[1], row[2]))
    return [row[3] for row in ranked]


def _summarize_processing_failures(
    failures: list[tuple[StacItem, str]],
    *,
    limit: int = 3,
) -> str:
    snippets: list[str] = []
    for item, message in failures[:limit]:
        snippets.append(f"{item.id}: {message}")
    remaining = len(failures) - limit
    if remaining > 0:
        snippets.append(f"+{remaining} more")
    return "; ".join(snippets)


class StacComputeRasterEngine(NdviRasterEngine):
    """Render NDVI rasters by fetching STAC COG assets."""

    engine_name: Final[str] = "stac"

    def __init__(
        self,
        *,
        client: StacClient | None = None,
        timeout_seconds: float | None = None,
        date_window_days: int | None = None,
        asset_red: str | None = None,
        asset_nir: str | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds or get_default_timeout_seconds()
        self.date_window_days = (
            date_window_days or get_default_date_window_days()
        )
        self.asset_red = asset_red or get_default_asset_red()
        self.asset_nir = asset_nir or get_default_asset_nir()
        self.client = client or StacClient(
            timeout_seconds=self.timeout_seconds
        )

    def render_png(self, request: RasterRequest) -> bytes:
        red_candidates = build_asset_candidates(self.asset_red)
        nir_candidates = build_asset_candidates(self.asset_nir)
        collections: list[str] | None = None
        client_collection = getattr(self.client, "collection", None)
        if client_collection:
            collections = [str(client_collection)]
        window = timedelta(days=self.date_window_days)
        if isinstance(self.client, StacClient):
            items = self.client.search(
                bbox=request.bbox,
                start=request.date - window,
                end=request.date + window,
                max_cloud=request.max_cloud,
                farm_id=request.farm_id,
                job_id=request.job_id,
            )
        else:
            items = self.client.search(
                bbox=request.bbox,
                start=request.date - window,
                end=request.date + window,
                max_cloud=request.max_cloud,
            )
        if not items:
            _raise_raster_not_found(
                reason="no_items",
                request=request,
                window_days=self.date_window_days,
                item=None,
                items_count=0,
                collections=collections,
                expected_assets={
                    "red": red_candidates,
                    "nir": nir_candidates,
                },
            )
        candidate_items = _rank_candidate_items(
            items,
            target_date=request.date,
            window_days=self.date_window_days,
        )
        if not candidate_items:
            _raise_raster_not_found(
                reason="no_best_item",
                request=request,
                window_days=self.date_window_days,
                item=None,
                items_count=len(items),
                collections=collections,
                expected_assets={
                    "red": red_candidates,
                    "nir": nir_candidates,
                },
            )

        processing_failures: list[tuple[StacItem, str]] = []
        stats_failures: list[StacItem] = []
        representative_item = candidate_items[0]

        for candidate in candidate_items:
            red_href = resolve_asset_href_candidates(candidate, red_candidates)
            nir_href = resolve_asset_href_candidates(candidate, nir_candidates)
            if not red_href or not nir_href:
                logger.warning(
                    "ndvi.raster.skip_item reason=missing_assets job_id=%s "
                    "farm_id=%s item_id=%s available_assets=%s "
                    "expected_assets=%s",
                    request.job_id if request.job_id is not None else "-",
                    request.farm_id if request.farm_id is not None else "-",
                    candidate.id,
                    sorted(candidate.assets.keys()),
                    {
                        "red": red_candidates,
                        "nir": nir_candidates,
                    },
                )
                continue

            try:
                ndvi = load_ndvi_array(
                    red_href=red_href,
                    nir_href=nir_href,
                    bbox=request.bbox,
                    size=request.size,
                    timeout_seconds=self.timeout_seconds,
                )
            except StacProcessingError as exc:
                processing_failures.append((candidate, str(exc)))
                logger.warning(
                    "ndvi.raster.skip_item reason=processing_failed "
                    "job_id=%s farm_id=%s item_id=%s err=%s",
                    request.job_id if request.job_id is not None else "-",
                    request.farm_id if request.farm_id is not None else "-",
                    candidate.id,
                    exc,
                )
                continue

            if compute_ndvi_stats(ndvi) is None:
                stats_failures.append(candidate)
                logger.warning(
                    "ndvi.raster.skip_item reason=empty_stats job_id=%s "
                    "farm_id=%s item_id=%s",
                    request.job_id if request.job_id is not None else "-",
                    request.farm_id if request.farm_id is not None else "-",
                    candidate.id,
                )
                continue

            self._log_ndvi_distribution(ndvi)
            return self._encode_png(ndvi)

        if processing_failures:
            summary = _summarize_processing_failures(processing_failures)
            raise StacUpstreamError(
                (
                    "Raster processing failed for all candidate STAC items. "
                    f"{summary}"
                ),
                retryable=True,
            )

        failed_item = (
            stats_failures[0] if stats_failures else representative_item
        )
        _raise_raster_not_found(
            reason="missing_assets",
            request=request,
            window_days=self.date_window_days,
            item=failed_item,
            items_count=len(items),
            collections=collections,
            item_collection=failed_item.collection,
            available_assets=sorted(failed_item.assets.keys()),
            expected_assets={
                "red": red_candidates,
                "nir": nir_candidates,
            },
        )

    def _log_ndvi_distribution(self, ndvi: np.ndarray) -> None:
        clean = ndvi.astype(np.float64)
        ndvi_min = float(np.nanmin(clean))
        ndvi_max = float(np.nanmax(clean))
        ndvi_mean = float(np.nanmean(clean))
        logger.info(
            "NDVI stats | min=%s max=%s mean=%s",
            ndvi_min,
            ndvi_max,
            ndvi_mean,
        )
        p2, p98 = np.nanpercentile(clean, (2, 98))
        logger.info(
            "NDVI percentiles | p2=%s p98=%s",
            float(p2),
            float(p98),
        )

    def _encode_png(self, ndvi: np.ndarray) -> bytes:
        return ndvi_to_png_bytes(ndvi)
