from __future__ import annotations

import io
import logging
from datetime import timedelta
from typing import Final, Literal, NoReturn

import numpy as np
from django.conf import settings
from PIL import Image
from rest_framework.exceptions import ValidationError

from ndvi.stac_client import (
    StacClient,
    StacItem,
    build_asset_candidates,
    compute_ndvi_stats,
    load_ndvi_array,
    resolve_asset_href_candidates,
    select_best_item,
)

from .base import NdviRasterEngine, RasterRequest

DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
DEFAULT_DATE_WINDOW_DAYS: Final[int] = 3
DEFAULT_ASSET_RED: Final[str] = "B04"
DEFAULT_ASSET_NIR: Final[str] = "B08"
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
    bbox = request.bbox
    bbox_values = (bbox.south, bbox.west, bbox.north, bbox.east)
    logger.warning(
        "ndvi.raster.not_found reason=%s job_id=%s farm_id=%s bbox=%s "
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
        item = select_best_item(
            items,
            target_date=request.date,
            window_days=self.date_window_days,
        )
        if item is None:
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

        red_href = resolve_asset_href_candidates(item, red_candidates)
        nir_href = resolve_asset_href_candidates(item, nir_candidates)
        if not red_href or not nir_href:
            _raise_raster_not_found(
                reason="missing_assets",
                request=request,
                window_days=self.date_window_days,
                item=item,
                items_count=len(items),
                collections=collections,
                item_collection=item.collection,
                available_assets=sorted(item.assets.keys()),
                expected_assets={
                    "red": red_candidates,
                    "nir": nir_candidates,
                },
            )

        ndvi = load_ndvi_array(
            red_href=red_href,
            nir_href=nir_href,
            bbox=request.bbox,
            size=request.size,
            timeout_seconds=self.timeout_seconds,
        )

        if compute_ndvi_stats(ndvi) is None:
            _raise_raster_not_found(
                reason="missing_assets",
                request=request,
                window_days=self.date_window_days,
                item=item,
                items_count=len(items),
                collections=collections,
                item_collection=item.collection,
                available_assets=sorted(item.assets.keys()),
                expected_assets={
                    "red": red_candidates,
                    "nir": nir_candidates,
                },
            )

        return self._encode_png(ndvi)

    def _encode_png(self, ndvi: np.ndarray) -> bytes:
        clamped = np.clip(ndvi, -1.0, 1.0)
        normalized = ((clamped + 1.0) / 2.0) * 255.0
        channel = np.nan_to_num(normalized, nan=0.0).astype(np.uint8)
        alpha = np.where(np.isfinite(clamped), 255, 0).astype(np.uint8)
        rgba = np.stack([channel, channel, channel, alpha], axis=-1)
        image = Image.fromarray(rgba, mode="RGBA")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
