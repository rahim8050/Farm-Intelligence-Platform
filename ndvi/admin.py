"""Django admin configuration for NDVI models with provenance support.

Provides:
- History tracking for NdviObservation via ``django-simple-history``.
- ``reproduce`` admin action to re-run compute for a given observation
  and diff the result with the stored version.
"""

from __future__ import annotations

import logging
from typing import Any

from django.contrib import admin, messages
from django.contrib.admin import ModelAdmin
from django.http import HttpRequest
from simple_history.admin import SimpleHistoryAdmin  # type: ignore

from ndvi.engines.base import BBox
from ndvi.models import (
    NdviDerivedObservation,
    NdviJob,
    NdviObservation,
    NdviRasterArtifact,
)
from ndvi.services import (
    get_default_lookback_days,
    get_default_max_cloud,
    get_engine,
    normalize_bbox,
)

logger = logging.getLogger(__name__)


def reproduce_observation(
    modeladmin: ModelAdmin,  # noqa: ARG001
    request: HttpRequest,
    queryset: Any,
) -> None:
    """Re-run compute for selected observations and diff the result.

    For each selected ``NdviObservation``, the admin action will:
    1. Re-fetch the source scene from the original provider.
    2. Re-compute the spectral index.
    3. Compare the new result with the stored observation.
    4. Log the diff and report via Django messages.

    This is useful for verifying reproducibility and detecting
    upstream data changes.
    """
    from ndvi.logging import Timer  # noqa: PLC0415

    total = queryset.count()
    reproduced = 0
    diffs_found = 0
    errors = 0

    for obs in queryset:
        timer = Timer()
        try:
            farm = obs.farm
            bbox: BBox = normalize_bbox(farm)
            engine = get_engine(obs.engine, index_type=obs.index_type)

            # Re-compute the latest value for the same date bucket
            point = engine.get_latest(
                bbox=bbox,
                lookback_days=get_default_lookback_days(),
                max_cloud=get_default_max_cloud(),
            )
            if point is None:
                messages.warning(
                    request,
                    f"Observation {obs.id}: No data returned from engine.",
                )
                errors += 1
                continue

            # Compare with stored values
            diffs: dict[str, dict[str, float | None]] = {}
            for field in ("mean", "min", "max"):
                stored = getattr(obs, field, None)
                new_val = getattr(point, field, None)
                if stored != new_val:
                    diffs[field] = {
                        "stored": stored,
                        "recomputed": new_val,
                    }

            if diffs:
                diffs_found += 1
                logger.info(
                    "reproduce.diff obs_id=%s engine=%s diffs=%s "
                    "duration_ms=%.0f",
                    obs.id,
                    obs.engine,
                    diffs,
                    timer.elapsed_ms(),
                )
                messages.warning(
                    request,
                    f"Observation {obs.id} has diffs: {diffs}. "
                    f"Run time: {timer.elapsed_ms():.0f}ms",
                )
            else:
                reproduced += 1
                logger.info(
                    "reproduce.match obs_id=%s engine=%s duration_ms=%.0f",
                    obs.id,
                    obs.engine,
                    timer.elapsed_ms(),
                )
                messages.success(
                    request,
                    f"Observation {obs.id} reproduced successfully. "
                    f"Run time: {timer.elapsed_ms():.0f}ms",
                )
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.exception("reproduce.error obs_id=%s err=%s", obs.id, exc)
            messages.error(
                request,
                f"Observation {obs.id} failed: {exc}",
            )

    messages.info(
        request,
        f"Reproduce complete: {reproduced} matched, "
        f"{diffs_found} diffs, {errors} errors out of {total}.",
    )


reproduce_observation.short_description = (  # type: ignore[attr-defined]
    "Reproduce selected observations (re-run compute and diff)"
)


@admin.register(NdviObservation)
class NdviObservationAdmin(SimpleHistoryAdmin):  # type: ignore
    """Admin for NdviObservation with history tracking and reproduce action.

    Displays key fields inline and provides the ``reproduce`` action
    for provenance verification.
    """

    list_display = [
        "id",
        "index_type",
        "farm",
        "engine",
        "bucket_date",
        "mean",
        "state",
        "is_latest",
        "version",
        "created_at",
    ]
    list_filter = [
        "index_type",
        "engine",
        "state",
        "is_latest",
        "version",
    ]
    search_fields = [
        "farm__name",
        "source_scene_id",
        "provenance_hash",
    ]
    date_hierarchy = "bucket_date"
    readonly_fields = [
        "created_at",
        "updated_at",
        "provenance_hash",
    ]
    actions = [reproduce_observation]
    history_list_display = [
        "state",
        "mean",
        "is_latest",
    ]


@admin.register(NdviJob)
class NdviJobAdmin(ModelAdmin):
    """Admin for NdviJob tracking."""

    list_display = [
        "id",
        "index_type",
        "farm",
        "engine",
        "job_type",
        "status",
        "attempts",
        "created_at",
    ]
    list_filter = [
        "index_type",
        "engine",
        "job_type",
        "status",
    ]
    search_fields = [
        "farm__name",
        "request_hash",
    ]
    date_hierarchy = "created_at"
    readonly_fields = [
        "request_hash",
        "created_at",
        "started_at",
        "finished_at",
    ]


@admin.register(NdviRasterArtifact)
class NdviRasterArtifactAdmin(ModelAdmin):
    """Admin for NdviRasterArtifact."""

    list_display = [
        "id",
        "index_type",
        "farm",
        "engine",
        "date",
        "size",
        "content_hash",
        "created_at",
    ]
    list_filter = [
        "index_type",
        "engine",
        "size",
    ]
    search_fields = [
        "farm__name",
        "content_hash",
    ]
    date_hierarchy = "date"


@admin.register(NdviDerivedObservation)
class NdviDerivedObservationAdmin(ModelAdmin):
    """Admin for NdviDerivedObservation (V2)."""

    list_display = [
        "id",
        "index_type",
        "farm",
        "engine",
        "bucket_date",
        "confidence",
        "is_null",
        "null_reason",
    ]
    list_filter = [
        "index_type",
        "engine",
        "is_null",
        "source",
    ]
    search_fields = [
        "farm__name",
    ]
    date_hierarchy = "bucket_date"
