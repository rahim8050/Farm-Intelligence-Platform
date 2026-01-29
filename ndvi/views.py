"""NDVI API endpoints.

Authentication: JWT or API key (global defaults).
All successful responses use `config.api.responses.success_response`
with the standard envelope:

    {"status": 0, "message": "<str>", "data": <object|null>, "errors": null}
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import date
from typing import Any, Final, cast

from django.conf import settings
from django.core.cache import caches
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    OpenApiTypes,
    extend_schema,
    inline_serializer,
)
from rest_framework import serializers, status
from rest_framework.exceptions import Throttled, ValidationError
from rest_framework.negotiation import BaseContentNegotiation
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import BaseRenderer, JSONRenderer
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from config.api.openapi import (
    error_envelope_serializer,
    success_envelope_serializer,
)
from config.api.responses import JSONValue, error_response, success_response
from farms.models import Farm

from .metrics import ndvi_farms_stale_total
from .models import NdviJob, NdviObservation, NdviRasterArtifact
from .raster.registry import resolve_raster_engine_name
from .serializers import (
    LatestRequestSerializer,
    NdviJobSerializer,
    NdviObservationSerializer,
    RasterPngRequestSerializer,
    TimeseriesRequestSerializer,
)
from .services import (
    LatestParams,
    TimeseriesParams,
    cache_latest_response,
    cache_timeseries_response,
    detect_gaps,
    enforce_quota,
    enqueue_job,
    expected_buckets,
    get_cached_latest_response,
    get_cached_timeseries_response,
    get_default_lookback_days,
    get_default_max_cloud,
    hash_request,
    is_stale,
    normalize_bbox,
    resolve_ndvi_engine_name,
)
from .tasks import run_ndvi_job

logger = logging.getLogger(__name__)

ndvi_error_response = error_envelope_serializer("NdviErrorResponse")
RASTER_NOT_FOUND_MESSAGE: Final[str] = "Raster not found"
RASTER_NOT_FOUND_CODE: Final[str] = "raster_not_found"
RASTER_NOT_FOUND_REASONS: Final[tuple[str, ...]] = (
    "no_items",
    "no_best_item",
    "missing_assets",
)


def _extract_raster_not_found_reason(
    last_error: str | None,
) -> str | None:
    if not last_error:
        return None
    try:
        payload = json.loads(last_error)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        reason = payload.get("reason")
        if isinstance(reason, str):
            return reason
    for reason in RASTER_NOT_FOUND_REASONS:
        if reason in last_error:
            return reason
    return None


def _lookup_raster_not_found_reason(
    *,
    owner_id: int,
    farm_id: int,
    engine: str,
    raster_date: date,
    size: int,
    max_cloud: int,
) -> str | None:
    request_hash = hash_request(
        engine=engine,
        owner_id=owner_id,
        farm_id=farm_id,
        params={
            "start": raster_date,
            "end": raster_date,
            "step_days": size,
            "max_cloud": max_cloud,
        },
    )
    job = (
        NdviJob.objects.filter(
            owner_id=owner_id,
            farm_id=farm_id,
            engine=engine,
            request_hash=request_hash,
            status=NdviJob.JobStatus.FAILED,
        )
        .order_by("-created_at")
        .first()
    )
    return _extract_raster_not_found_reason(job.last_error if job else None)


def _build_raster_not_found_errors(
    reason: str | None,
) -> dict[str, JSONValue]:
    return {
        "detail": RASTER_NOT_FOUND_MESSAGE,
        "code": RASTER_NOT_FOUND_CODE,
        "reason": reason,
    }


ndvi_observation_schema = NdviObservationSerializer()
timeseries_data_schema = inline_serializer(
    name="NdviTimeseriesData",
    fields={
        "observations": NdviObservationSerializer(many=True),
        "engine": serializers.CharField(),
        "start": serializers.DateField(),
        "end": serializers.DateField(),
        "step_days": serializers.IntegerField(),
        "max_cloud": serializers.IntegerField(),
        "is_partial": serializers.BooleanField(),
        "missing_buckets_count": serializers.IntegerField(),
    },
)
timeseries_success_response = success_envelope_serializer(
    "NdviTimeseriesSuccess", data=timeseries_data_schema
)

latest_data_schema = inline_serializer(
    name="NdviLatestData",
    fields={
        "observation": NdviObservationSerializer(allow_null=True),
        "engine": serializers.CharField(),
        "lookback_days": serializers.IntegerField(),
        "max_cloud": serializers.IntegerField(),
        "stale": serializers.BooleanField(),
    },
)
latest_success_response = success_envelope_serializer(
    "NdviLatestSuccess", data=latest_data_schema
)

job_success_response = success_envelope_serializer(
    "NdviJobSuccess",
    data=NdviJobSerializer(),
)

refresh_success_response = success_envelope_serializer(
    "NdviRefreshSuccess",
    data=inline_serializer(
        name="NdviRefreshData",
        fields={"job_id": serializers.IntegerField()},
    ),
)

raster_queue_success_response = success_envelope_serializer(
    "NdviRasterQueueSuccess",
    data=inline_serializer(
        name="NdviRasterQueueData",
        fields={"job_id": serializers.IntegerField()},
    ),
)

engine_query_param = OpenApiParameter(
    name="engine",
    type=OpenApiTypes.STR,
    location=OpenApiParameter.QUERY,
    required=False,
    description="Override NDVI engine (sentinelhub or stac).",
)

timeseries_query_params = [
    OpenApiParameter(
        name="start",
        type=OpenApiTypes.DATE,
        location=OpenApiParameter.QUERY,
        required=True,
    ),
    OpenApiParameter(
        name="end",
        type=OpenApiTypes.DATE,
        location=OpenApiParameter.QUERY,
        required=True,
    ),
    OpenApiParameter(
        name="step_days",
        type=OpenApiTypes.INT,
        location=OpenApiParameter.QUERY,
        required=False,
        description="Days per bucket (1-30)",
    ),
    OpenApiParameter(
        name="max_cloud",
        type=OpenApiTypes.INT,
        location=OpenApiParameter.QUERY,
        required=False,
        description="Maximum cloud coverage percent (0-100)",
    ),
    engine_query_param,
]

latest_query_params = [
    OpenApiParameter(
        name="lookback_days",
        type=OpenApiTypes.INT,
        location=OpenApiParameter.QUERY,
        required=False,
    ),
    OpenApiParameter(
        name="max_cloud",
        type=OpenApiTypes.INT,
        location=OpenApiParameter.QUERY,
        required=False,
    ),
    engine_query_param,
]

raster_query_params = [
    OpenApiParameter(
        name="date",
        type=OpenApiTypes.DATE,
        location=OpenApiParameter.QUERY,
        required=True,
    ),
    OpenApiParameter(
        name="size",
        type=OpenApiTypes.INT,
        location=OpenApiParameter.QUERY,
        required=False,
    ),
    OpenApiParameter(
        name="max_cloud",
        type=OpenApiTypes.INT,
        location=OpenApiParameter.QUERY,
        required=False,
    ),
    engine_query_param,
]


class IgnoreAcceptHeaderNegotiation(BaseContentNegotiation):
    """Force JSON negotiation so Accept headers never block PNG responses."""

    def select_renderer(
        self,
        request: Request,
        renderers: Iterable[BaseRenderer],
        format_suffix: str | None = None,
    ) -> tuple[BaseRenderer, str]:
        renderer_list = list(renderers)
        renderer = next(
            (item for item in renderer_list if isinstance(item, JSONRenderer)),
            renderer_list[0],
        )
        return renderer, renderer.media_type


class BaseFarmView(APIView):
    """Shared helpers for NDVI farm endpoints.

    Auth: IsAuthenticated.
    Permissions: owner-only enforced per farm lookup.
    Response envelope: `success_response`.
    """

    permission_classes = [IsAuthenticated]

    def finalize_response(
        self,
        request: Request,
        response: Response,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        response = super().finalize_response(
            request, response, *args, **kwargs
        )
        request_id = (
            request.headers.get("X-Request-Id")
            or request.META.get("HTTP_X_REQUEST_ID")
            or "-"
        )
        logger.info(
            "ndvi request: method=%s path=%s status=%s request_id=%s",
            request.method,
            request.path,
            response.status_code,
            request_id,
        )
        return response

    def _get_farm(self, farm_id: int, user_id: int) -> Farm:
        return get_object_or_404(
            Farm, id=farm_id, owner_id=user_id, is_active=True
        )


class NdviTimeseriesView(BaseFarmView):
    """Serve NDVI time series for a farm.

    Enqueues gap-fill jobs when buckets are missing.
    """

    @extend_schema(
        parameters=timeseries_query_params,
        responses={
            200: timeseries_success_response,
            400: ndvi_error_response,
            404: ndvi_error_response,
        },
    )
    def get(self, request: Request, farm_id: int) -> Response:
        """Return NDVI observations for the requested range.

        Query params: start, end, optional step_days, optional max_cloud,
        optional engine.
        Success: envelope containing observations + metadata
        (is_partial, missing_buckets_count).
        Side effects: schedules gap-fill job when buckets are missing.
        """

        farm = self._get_farm(farm_id, cast(int, request.user.id))
        engine_override = request.query_params.get("engine")
        try:
            engine_name = resolve_ndvi_engine_name(engine_override)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        serializer = TimeseriesRequestSerializer(
            data=request.query_params, context={"engine": engine_name}
        )
        serializer.is_valid(raise_exception=True)
        params = TimeseriesParams(**serializer.validated_data)

        bbox = normalize_bbox(farm)
        enforce_quota(farm, bbox)

        cached = get_cached_timeseries_response(
            owner_id=cast(int, request.user.id),
            farm_id=farm.id,
            engine=engine_name,
            params=params,
        )
        if cached:
            return success_response(
                cached, message="NDVI time series (cached)"
            )

        observations = list(
            NdviObservation.objects.filter(
                farm=farm,
                engine=engine_name,
                bucket_date__gte=params.start,
                bucket_date__lte=params.end,
            ).order_by("bucket_date")
        )
        serialized = NdviObservationSerializer(observations, many=True).data
        existing_dates = {obs.bucket_date for obs in observations}
        expected = expected_buckets(
            params.start,
            params.end,
            params.step_days,
        )
        missing = detect_gaps(existing_dates, expected)

        if missing:
            job = enqueue_job(
                owner_id=cast(int, request.user.id),
                farm=farm,
                engine_name=engine_override,
                job_type=NdviJob.JobType.GAP_FILL,
                params={
                    "start": params.start,
                    "end": params.end,
                    "step_days": params.step_days,
                    "max_cloud": params.max_cloud,
                },
            )
            run_ndvi_job.delay(job.id)

        payload: dict[str, Any] = {
            "observations": serialized,
            "engine": engine_name,
            "start": params.start,
            "end": params.end,
            "step_days": params.step_days,
            "max_cloud": params.max_cloud,
            "is_partial": bool(missing),
            "missing_buckets_count": len(missing),
        }
        cache_timeseries_response(
            owner_id=cast(int, request.user.id),
            farm_id=farm.id,
            engine=engine_name,
            params=params,
            payload=payload,
        )
        return success_response(payload, message="NDVI time series")


class NdviLatestView(BaseFarmView):
    """Return the latest NDVI observation and enqueue a refresh if stale."""

    @extend_schema(
        parameters=latest_query_params,
        responses={
            200: latest_success_response,
            400: ndvi_error_response,
            404: ndvi_error_response,
        },
    )
    def get(self, request: Request, farm_id: int) -> Response:
        """Return the most recent NDVI observation if present.

        Query params: lookback_days (optional), max_cloud (optional),
        engine (optional).
        Success: envelope with `observation` or null, plus stale flag.
        Side effects: enqueues refresh_latest job when missing/stale.
        """

        farm = self._get_farm(farm_id, cast(int, request.user.id))
        engine_override = request.query_params.get("engine")
        try:
            engine_name = resolve_ndvi_engine_name(engine_override)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        serializer = LatestRequestSerializer(
            data=request.query_params, context={"engine": engine_name}
        )
        serializer.is_valid(raise_exception=True)
        params = LatestParams(**serializer.validated_data)

        bbox = normalize_bbox(farm)
        enforce_quota(farm, bbox)

        cached = get_cached_latest_response(
            owner_id=cast(int, request.user.id),
            farm_id=farm.id,
            engine=engine_name,
            params=params,
        )
        if cached:
            return success_response(cached, message="NDVI latest (cached)")

        observation = (
            NdviObservation.objects.filter(farm=farm, engine=engine_name)
            .order_by("-bucket_date")
            .first()
        )

        stale = is_stale(observation, params.lookback_days)
        if stale:
            ndvi_farms_stale_total.labels(engine=engine_name).set(1)
            job = enqueue_job(
                owner_id=cast(int, request.user.id),
                farm=farm,
                engine_name=engine_override,
                job_type=NdviJob.JobType.REFRESH_LATEST,
                params={
                    "lookback_days": params.lookback_days,
                    "max_cloud": params.max_cloud,
                },
            )
            run_ndvi_job.delay(job.id)
        else:
            ndvi_farms_stale_total.labels(engine=engine_name).set(0)

        payload: dict[str, Any] = {
            "observation": (
                NdviObservationSerializer(observation).data
                if observation
                else None
            ),
            "engine": engine_name,
            "lookback_days": params.lookback_days,
            "max_cloud": params.max_cloud,
            "stale": stale,
        }
        cache_latest_response(
            owner_id=cast(int, request.user.id),
            farm_id=farm.id,
            engine=engine_name,
            params=params,
            payload=payload,
        )
        return success_response(payload, message="Latest NDVI")


class NdviRasterPngView(BaseFarmView):
    """Serve NDVI raster PNG for a farm (owner-only)."""

    renderer_classes = [JSONRenderer]
    content_negotiation_class = IgnoreAcceptHeaderNegotiation

    @extend_schema(
        parameters=raster_query_params,
        responses={
            (200, "image/png"): OpenApiTypes.BINARY,
            304: OpenApiResponse(response=None, description="Not Modified"),
            400: ndvi_error_response,
            401: ndvi_error_response,
            403: ndvi_error_response,
            404: ndvi_error_response,
        },
    )
    def get(self, request: Request, farm_id: int) -> HttpResponse | Response:
        """Return a cached NDVI raster PNG or 404 if missing.

        Query params: date (required), optional size, max_cloud, engine.
        Success: binary image/png with ETag + Cache-Control.
        Errors: standard error envelope.
        """

        farm = self._get_farm(farm_id, cast(int, request.user.id))
        engine_override = request.query_params.get("engine")
        engine_name = resolve_raster_engine_name(engine_override)
        serializer = RasterPngRequestSerializer(
            data=request.query_params, context={"engine": engine_name}
        )
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data

        bbox = normalize_bbox(farm)
        enforce_quota(farm, bbox)

        cache_key = (
            f"ndvi:raster:ptr:{farm.id}:{engine_name}:"
            f"{params['date']}:{params['size']}:{params['max_cloud']}"
        )
        cache = caches["default"]
        artifact_id = cache.get(cache_key)
        artifact: NdviRasterArtifact | None = None
        if artifact_id:
            artifact = NdviRasterArtifact.objects.filter(
                id=cast(int, artifact_id)
            ).first()
        if artifact is None:
            artifact = NdviRasterArtifact.objects.filter(
                farm=farm,
                engine=engine_name,
                date=params["date"],
                size=params["size"],
                max_cloud=params["max_cloud"],
            ).first()
            if artifact:
                cache.set(
                    cache_key,
                    artifact.id,
                    getattr(settings, "NDVI_RASTER_CACHE_TTL_SECONDS", 86400),
                )
        if artifact is None:
            reason = _lookup_raster_not_found_reason(
                owner_id=cast(int, request.user.id),
                farm_id=farm.id,
                engine=engine_name,
                raster_date=cast(date, params["date"]),
                size=cast(int, params["size"]),
                max_cloud=cast(int, params["max_cloud"]),
            )
            return error_response(
                RASTER_NOT_FOUND_MESSAGE,
                errors=_build_raster_not_found_errors(reason),
                status_code=status.HTTP_404_NOT_FOUND,
            )

        etag = artifact.content_hash
        client_etag = request.headers.get("If-None-Match")
        if client_etag and client_etag.strip('"') == etag:
            not_modified = HttpResponse(status=status.HTTP_304_NOT_MODIFIED)
            not_modified["ETag"] = etag
            return not_modified

        artifact.image.open("rb")
        content = artifact.image.read()
        artifact.image.close()
        if not content:
            reason = _lookup_raster_not_found_reason(
                owner_id=cast(int, request.user.id),
                farm_id=farm.id,
                engine=engine_name,
                raster_date=cast(date, params["date"]),
                size=cast(int, params["size"]),
                max_cloud=cast(int, params["max_cloud"]),
            )
            return error_response(
                RASTER_NOT_FOUND_MESSAGE,
                errors=_build_raster_not_found_errors(reason),
                status_code=status.HTTP_404_NOT_FOUND,
            )
        response = HttpResponse(content, content_type="image/png")
        response["ETag"] = etag
        ttl = int(getattr(settings, "NDVI_RASTER_CACHE_TTL_SECONDS", 86400))
        response["Cache-Control"] = f"public, max-age={ttl}"
        return response


class NdviRasterQueueView(BaseFarmView):
    """Queue NDVI raster rendering with cooldown."""

    throttle_cooldown = int(
        getattr(
            settings,
            "NDVI_RASTER_MANUAL_QUEUE_COOLDOWN_SECONDS",
            900,
        )
    )

    @extend_schema(
        request=RasterPngRequestSerializer,
        parameters=[engine_query_param],
        responses={
            202: raster_queue_success_response,
            400: ndvi_error_response,
            404: ndvi_error_response,
            429: ndvi_error_response,
        },
    )
    def post(self, request: Request, farm_id: int) -> Response:
        """Enqueue a raster render job for the specified date.

        Query params: optional engine override.
        """

        farm = self._get_farm(farm_id, cast(int, request.user.id))
        engine_override = request.query_params.get("engine")
        engine_name = resolve_raster_engine_name(engine_override)
        serializer = RasterPngRequestSerializer(
            data=request.data, context={"engine": engine_name}
        )
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data

        bbox = normalize_bbox(farm)
        enforce_quota(farm, bbox)

        throttle_cache = caches["default"]
        key = f"ndvi:raster:queue:{request.user.id}:{farm.id}"
        if throttle_cache.get(key):
            raise Throttled(detail="Raster already queued recently.")
        throttle_cache.set(key, "1", self.throttle_cooldown)

        job = enqueue_job(
            owner_id=cast(int, request.user.id),
            farm=farm,
            engine_name=engine_override,
            job_type=NdviJob.JobType.RASTER_PNG,
            params={
                "start": params["date"],
                "end": params["date"],
                "step_days": params["size"],
                "max_cloud": params["max_cloud"],
            },
        )
        run_ndvi_job.delay(job.id)

        return success_response(
            {"job_id": job.id},
            message="Raster render queued",
            status_code=status.HTTP_202_ACCEPTED,
        )


class NdviRefreshView(BaseFarmView):
    """Manual NDVI refresh trigger with throttling."""

    throttle_cooldown = int(
        getattr(
            settings,
            "NDVI_MANUAL_REFRESH_COOLDOWN_SECONDS",
            900,
        )
    )

    @extend_schema(
        request=None,
        responses={
            202: refresh_success_response,
            400: ndvi_error_response,
            404: ndvi_error_response,
            429: ndvi_error_response,
        },
    )
    def post(self, request: Request, farm_id: int) -> Response:
        """Enqueue a refresh_latest job if not recently triggered."""

        farm = self._get_farm(farm_id, cast(int, request.user.id))
        bbox = normalize_bbox(farm)
        enforce_quota(farm, bbox)

        throttle_cache = caches["default"]
        key = f"ndvi:refresh:throttle:{request.user.id}:{farm.id}"
        if throttle_cache.get(key):
            raise Throttled(detail="Refresh already triggered recently.")
        throttle_cache.set(key, "1", self.throttle_cooldown)

        job = enqueue_job(
            owner_id=cast(int, request.user.id),
            farm=farm,
            engine_name=None,
            job_type=NdviJob.JobType.REFRESH_LATEST,
            params={
                "lookback_days": get_default_lookback_days(),
                "max_cloud": get_default_max_cloud(),
            },
        )
        run_ndvi_job.delay(job.id)

        return success_response(
            {"job_id": job.id},
            message="Refresh queued",
            status_code=status.HTTP_202_ACCEPTED,
        )


class NdviJobStatusView(APIView):
    """Inspect NDVI job status for the authenticated user."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={
            200: job_success_response,
            404: ndvi_error_response,
        }
    )
    def get(self, request: Request, job_id: int) -> Response:
        """Return the status of an NDVI job."""

        job = get_object_or_404(
            NdviJob.objects.select_related("farm"),
            id=job_id,
            owner_id=cast(int, request.user.id),
        )
        return success_response(
            NdviJobSerializer(job).data, message="Job status"
        )
