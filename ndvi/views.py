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
from uuid import UUID

from django.conf import settings
from django.core.cache import caches
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    OpenApiTypes,
    extend_schema,
    inline_serializer,
)
from rest_framework import serializers, status
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import (
    PermissionDenied,
    Throttled,
    ValidationError,
)
from rest_framework.negotiation import BaseContentNegotiation
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.renderers import BaseRenderer, JSONRenderer
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from config.api.openapi import (
    error_envelope_serializer,
    success_envelope_serializer,
)
from config.api.responses import JSONValue, error_response, success_response
from farms.authentication import FarmObservationAuthentication
from farms.models import Farm
from integrations.authentication import (
    IntegrationJWTAuthentication,
    IntegrationTokenUser,
)

from .farm_state import build_farm_state
from .metrics import ndvi_farms_stale_total
from .models import (
    NdviDerivedObservation,
    NdviJob,
    NdviObservation,
    NdviRasterArtifact,
)
from .raster.registry import resolve_raster_engine_name
from .serializers import (
    FarmStateSerializer,
    LatestRequestSerializer,
    NdviDerivedObservationSerializer,
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
    dispatch_ndvi_job,
    enforce_quota,
    enqueue_job,
    expected_buckets,
    filter_observations_by_cloud,
    get_cached_latest_response,
    get_cached_timeseries_response,
    get_default_lookback_days,
    get_default_max_cloud,
    get_default_ndvi_engine_name,
    hash_request,
    is_stale,
    normalize_bbox,
    resolve_ndvi_engine_name,
)

logger = logging.getLogger(__name__)


def _auth_type(request: Request) -> str:
    api_key = request.META.get("HTTP_X_API_KEY")
    if api_key:
        return "api_key"
    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    if isinstance(auth_header, str) and auth_header.lower().startswith(
        "bearer "
    ):
        return "jwt_bearer"
    if auth_header:
        return "authorization"
    return "unknown"


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
ndvi_v2_observation_schema = NdviDerivedObservationSerializer()

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
        "v2_observations": serializers.ListField(
            child=NdviDerivedObservationSerializer(allow_null=True),
            required=False,
            allow_null=True,
        ),
        "representation": serializers.CharField(
            required=False, allow_null=True
        ),
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
        "v2_observation": NdviDerivedObservationSerializer(
            allow_null=True, required=False
        ),
        "representation": serializers.CharField(
            required=False, allow_null=True
        ),
    },
)
latest_success_response = success_envelope_serializer(
    "NdviLatestSuccess", data=latest_data_schema
)

farm_state_success_response = success_envelope_serializer(
    "FarmStateSuccess", data=FarmStateSerializer()
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

representation_query_param = OpenApiParameter(
    name="representation",
    type=OpenApiTypes.STR,
    location=OpenApiParameter.QUERY,
    required=False,
    description="Response representation version (v1 or v2).",
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
    representation_query_param,
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
    representation_query_param,
]

external_farm_id_query_param = OpenApiParameter(
    name="external_farm_id",
    type=OpenApiTypes.UUID,
    location=OpenApiParameter.QUERY,
    required=False,
    description="Resolve farm by external_farm_id (integration tokens).",
)

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
    external_farm_id_query_param,
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

    authentication_classes: tuple[type[BaseAuthentication], ...] = (
        FarmObservationAuthentication,
        IntegrationJWTAuthentication,
    )
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
        from integrations.authentication import IntegrationTokenUser

        user = self.request.user
        logger.info(
            "ndvi._get_farm farm_id=%s user_type=%s user_id=%s",
            farm_id,
            type(user).__name__,
            getattr(user, "id", None),
        )
        if isinstance(user, IntegrationTokenUser):
            client_id = user.client_id
            logger.info(
                "ndvi._get_farm integration client_id=%s",
                client_id,
            )
            try:
                return get_object_or_404(
                    Farm,
                    id=farm_id,
                    integration_access__client_id=client_id,
                    integration_access__is_active=True,
                    is_active=True,
                )
            except Http404:
                logger.debug(
                    "ndvi.farm.not_found farm_id=%s client_id=%s"
                    " auth=%s path=%s",
                    farm_id,
                    client_id,
                    _auth_type(self.request),
                    getattr(self.request, "path", ""),
                )
                raise

        try:
            return get_object_or_404(
                Farm,
                Q(id=farm_id, owner_id=user_id, is_active=True)
                | Q(
                    id=farm_id,
                    integration_access__is_active=True,
                    is_active=True,
                ),
            )
        except Http404:
            logger.debug(
                "ndvi.farm.not_found farm_id=%s user_id=%s auth=%s path=%s",
                farm_id,
                user_id,
                _auth_type(self.request),
                getattr(self.request, "path", ""),
            )
            raise


class FarmStateView(BaseFarmView):
    """Summarize NDVI-derived farm state for a farm.

    Auth: API key, user JWT, or integration JWT.
    Permissions: owner-only for user/API key requests.
    Integration access: allow-listed per farm via FarmIntegrationAccess.
    Integration scope: read.
    Response data: farm_id, mean_ndvi, max_ndvi, coverage_pct, trend,
    state, interpretation, action.
    """

    authentication_classes = (FarmObservationAuthentication,)
    permission_classes = [IsAuthenticated]

    def _integration_scopes(self, request: Request) -> set[str]:
        auth_obj: Any = getattr(request, "auth", None)

        def _claim(key: str) -> str:
            if isinstance(auth_obj, dict):
                return str(auth_obj.get(key, "") or "")
            getter = getattr(auth_obj, "get", None)
            if callable(getter):
                try:
                    value = getter(key)
                except Exception:
                    value = None
                else:
                    if value is not None:
                        return str(value or "")
            try:
                return str(auth_obj[key] or "")
            except Exception:
                return ""

        scope = _claim("scope")
        if not scope:
            return set()
        normalized = scope.replace(",", " ")
        return {item for item in normalized.split() if item}

    def _enforce_integration_scope(
        self, request: Request, *, write: bool
    ) -> None:
        if not isinstance(request.user, IntegrationTokenUser):
            return
        scopes = self._integration_scopes(request)
        if not scopes:
            raise PermissionDenied("Integration token scope missing.")
        read_scopes = {"read", "write", "admin"}
        write_scopes = {"write", "admin"}
        allowed = write_scopes if write else read_scopes
        if not scopes.intersection(allowed):
            raise PermissionDenied("Integration token scope not permitted.")

    def _get_farm_for_request(self, request: Request, farm_id: int) -> Farm:
        if isinstance(request.user, IntegrationTokenUser):
            return get_object_or_404(
                Farm,
                id=farm_id,
                is_active=True,
                integration_access__client_id=request.user.client_id,
                integration_access__is_active=True,
            )

        user_id = getattr(request.user, "id", None)
        if user_id is None:
            raise Http404

        return self._get_farm(farm_id, cast(int, user_id))

    @extend_schema(
        auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
        operation_id="v1_farm_state_retrieve",
        parameters=[representation_query_param],
        responses={
            200: farm_state_success_response,
            401: ndvi_error_response,
            403: ndvi_error_response,
            404: ndvi_error_response,
        },
    )
    def get(self, request: Request, farm_id: int) -> Response:
        """Return the derived farm state for the requested farm.

        Inputs: path param farm_id.
        Query params: optional representation.
        Output: success envelope with NDVI metrics + classification.
        When representation=v2, payload includes v2_observation.
        Side effects: none.
        """

        self._enforce_integration_scope(request, write=False)
        farm = self._get_farm_for_request(request, farm_id)
        result = build_farm_state(farm=farm)
        payload = result.as_payload()

        representation = request.query_params.get("representation", "v1")
        if representation == "v2":
            default_engine = get_default_ndvi_engine_name()
            v2_obj = (
                NdviDerivedObservation.objects.filter(
                    farm=farm, engine=default_engine
                )
                .order_by("-bucket_date")
                .first()
            )
            payload["v2_observation"] = (
                NdviDerivedObservationSerializer(v2_obj).data
                if v2_obj
                else _v2_blank()
            )
            payload["representation"] = "v2"
        return success_response(payload, message="Farm state")


class BaseRasterView(BaseFarmView):
    """Shared helpers for NDVI raster endpoints.

    Auth: API key, user JWT, or integration JWT.
    Permissions: IsAuthenticated; owner-only for user/API key requests.
    Integration access: allow-listed per farm via FarmIntegrationAccess.
    Integration scope: read for GET, write for POST.
    Response envelope: `success_response` (errors use standard envelope).
    """

    authentication_classes = (FarmObservationAuthentication,)
    permission_classes = [IsAuthenticated]

    def _integration_scopes(self, request: Request) -> set[str]:
        auth_obj: Any = getattr(request, "auth", None)

        def _claim(key: str) -> str:
            if isinstance(auth_obj, dict):
                return str(auth_obj.get(key, "") or "")
            getter = getattr(auth_obj, "get", None)
            if callable(getter):
                try:
                    value = getter(key)
                except Exception:
                    value = None
                else:
                    if value is not None:
                        return str(value or "")
            try:
                return str(auth_obj[key] or "")
            except Exception:
                return ""

        scope = _claim("scope")
        if not scope:
            return set()
        normalized = scope.replace(",", " ")
        return {item for item in normalized.split() if item}

    def _enforce_integration_scope(
        self, request: Request, *, write: bool
    ) -> None:
        if not isinstance(request.user, IntegrationTokenUser):
            return
        scopes = self._integration_scopes(request)
        if not scopes:
            raise PermissionDenied("Integration token scope missing.")
        read_scopes = {"read", "write", "admin"}
        write_scopes = {"write", "admin"}
        allowed = write_scopes if write else read_scopes
        if not scopes.intersection(allowed):
            raise PermissionDenied("Integration token scope not permitted.")

    def _resolve_external_farm_id(self, request: Request) -> UUID | None:
        raw = request.query_params.get("external_farm_id")
        if not raw:
            return None
        try:
            return UUID(str(raw))
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "external_farm_id must be a valid UUID."
            ) from exc

    def _get_farm_for_request(self, request: Request, farm_id: int) -> Farm:
        if isinstance(request.user, IntegrationTokenUser):
            external_farm_id = self._resolve_external_farm_id(request)
            lookup: dict[str, Any] = {
                "is_active": True,
                "integration_access__client_id": request.user.client_id,
                "integration_access__is_active": True,
            }
            if external_farm_id is not None:
                lookup["external_farm_id"] = external_farm_id
            else:
                lookup["id"] = farm_id
            return get_object_or_404(Farm, **lookup)

        user_id = getattr(request.user, "id", None)
        if user_id is None:
            raise Http404
        return self._get_farm(farm_id, cast(int, user_id))


def _v2_blank() -> dict[str, Any]:
    return {
        "smoothed_ndvi": None,
        "confidence": None,
        "source": None,
        "quality_flags": None,
    }


def _inject_v2_observations(
    payload: dict[str, Any],
    *,
    observations: list[NdviObservation] | None,
    farm: Farm,
    engine: str,
) -> None:
    """Add V2 derived observations to a timeseries payload.

    Mutates *payload* in place, adding a ``v2_observations`` key
    and a ``representation`` key.  Each observation dict is also
    extended with inline V2 fields.
    """
    bucket_dates_raw = [
        o.get("bucket_date") for o in payload.get("observations", [])
    ]
    if not bucket_dates_raw:
        return

    v2_qs = NdviDerivedObservation.objects.filter(
        farm=farm,
        engine=engine,
        bucket_date__in=[
            d if isinstance(d, date) else d for d in bucket_dates_raw
        ],
    )
    v2_index: dict[str, dict[str, Any]] = {}
    for v2 in v2_qs:
        v2_index[v2.bucket_date.isoformat()] = (
            NdviDerivedObservationSerializer(v2).data
        )

    v2_list = []
    for obs_data in payload.get("observations", []):
        iso = obs_data.get("bucket_date")
        if isinstance(iso, date):
            iso = iso.isoformat()
        v2_entry = v2_index.get(iso)
        if v2_entry:
            obs_data.update(v2_entry)
            v2_list.append(v2_entry)
        else:
            blank = _v2_blank()
            obs_data.update(blank)
            v2_list.append(blank)

    payload["v2_observations"] = v2_list
    payload["representation"] = "v2"


def _inject_single_v2_observation(
    payload: dict[str, Any],
    *,
    farm: Farm,
    engine: str,
    obs_date: date | str | None,
) -> None:
    """Add a single V2 derived observation to a latest payload.

    Mutates *payload* in place, adding ``v2_observation`` and
    ``representation`` keys.
    """
    if obs_date is None:
        payload["v2_observation"] = _v2_blank()
        payload["representation"] = "v2"
        return

    if isinstance(obs_date, str):
        obs_date = date.fromisoformat(obs_date)

    try:
        v2 = NdviDerivedObservation.objects.get(
            farm=farm, engine=engine, bucket_date=obs_date
        )
        payload["v2_observation"] = NdviDerivedObservationSerializer(v2).data
    except NdviDerivedObservation.DoesNotExist:
        payload["v2_observation"] = _v2_blank()

    payload["representation"] = "v2"


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
        optional engine, optional representation.
        Success: envelope containing observations + metadata
        (is_partial, missing_buckets_count). When representation=v2,
        payload includes v2_observations and representation.
        Side effects: schedules gap-fill job when buckets are missing.
        """

        farm = self._get_farm(farm_id, cast(int, request.user.id))
        engine_override = request.query_params.get("engine")
        representation = request.query_params.get("representation", "v1")
        try:
            engine_name = resolve_ndvi_engine_name(engine_override)
        except ValueError as exc:
            logger.warning(
                "Invalid NDVI engine override received.",
                exc_info=exc,
            )
            raise ValidationError("Invalid engine parameter.") from exc
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
            if representation == "v2":
                _inject_v2_observations(
                    cached,
                    observations=None,
                    farm=farm,
                    engine=engine_name,
                )
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
        observations = filter_observations_by_cloud(
            observations,
            max_cloud=params.max_cloud,
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
            dispatch_ndvi_job(job)

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
        if representation == "v2":
            _inject_v2_observations(
                payload,
                observations=observations,
                farm=farm,
                engine=engine_name,
            )
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
        engine (optional), representation (optional).
        Success: envelope with `observation` or null, plus stale flag.
        When representation=v2, payload includes v2_observation
        and representation fields.
        Side effects: enqueues refresh_latest job when missing/stale.
        """

        farm = self._get_farm(farm_id, cast(int, request.user.id))
        engine_override = request.query_params.get("engine")
        representation = request.query_params.get("representation", "v1")
        try:
            engine_name = resolve_ndvi_engine_name(engine_override)
        except ValueError as exc:
            logger.warning("Invalid NDVI engine override provided: %s", exc)
            raise ValidationError("Invalid engine parameter.") from exc
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
            if representation == "v2":
                obs_data = cached.get("observation") or {}
                obs_date_raw = obs_data.get("bucket_date")
                _inject_single_v2_observation(
                    cached,
                    farm=farm,
                    engine=engine_name,
                    obs_date=obs_date_raw,
                )
            return success_response(cached, message="NDVI latest (cached)")

        observations = filter_observations_by_cloud(
            list(
                NdviObservation.objects.filter(
                    farm=farm,
                    engine=engine_name,
                    is_latest=True,
                )
                .exclude(state="INVALIDATED")
                .exclude(state="REJECTED")
                .order_by("-bucket_date")
            ),
            max_cloud=params.max_cloud,
        )
        observation = observations[0] if observations else None

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
            dispatch_ndvi_job(job)
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
        if representation == "v2":
            _inject_single_v2_observation(
                payload,
                farm=farm,
                engine=engine_name,
                obs_date=observation.bucket_date if observation else None,
            )
        cache_latest_response(
            owner_id=cast(int, request.user.id),
            farm_id=farm.id,
            engine=engine_name,
            params=params,
            payload=payload,
        )
        return success_response(payload, message="Latest NDVI")


class NdviRasterPngView(BaseRasterView):
    """Serve NDVI raster PNG for a farm.

    Auth: API key, user JWT, or integration JWT.
    Permissions: owner-only for user/API key requests.
    Integration access: allow-listed per farm via FarmIntegrationAccess.
    """

    renderer_classes = [JSONRenderer]
    content_negotiation_class = IgnoreAcceptHeaderNegotiation

    @extend_schema(
        auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
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

        Query params: date (required), optional size, max_cloud, engine,
        external_farm_id (integration tokens).
        Success: binary image/png with ETag + Cache-Control.
        Errors: standard error envelope.
        """

        self._enforce_integration_scope(request, write=False)
        farm = self._get_farm_for_request(request, farm_id)
        owner_id = farm.owner_id
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
                owner_id=owner_id,
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
                owner_id=owner_id,
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


class NdviRasterQueueView(BaseRasterView):
    """Queue NDVI raster rendering with cooldown."""

    throttle_cooldown = int(
        getattr(
            settings,
            "NDVI_RASTER_MANUAL_QUEUE_COOLDOWN_SECONDS",
            900,
        )
    )

    @extend_schema(
        auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
        request=RasterPngRequestSerializer,
        parameters=[engine_query_param, external_farm_id_query_param],
        responses={
            202: raster_queue_success_response,
            400: ndvi_error_response,
            404: ndvi_error_response,
            429: ndvi_error_response,
        },
    )
    def post(self, request: Request, farm_id: int) -> Response:
        """Enqueue a raster render job for the specified date.

        Query params: optional engine override, external_farm_id (integration).
        """

        self._enforce_integration_scope(request, write=True)
        farm = self._get_farm_for_request(request, farm_id)
        owner_id = farm.owner_id
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
        key = f"ndvi:raster:queue:{owner_id}:{farm.id}"
        if throttle_cache.get(key):
            raise Throttled(detail="Raster already queued recently.")
        throttle_cache.set(key, "1", self.throttle_cooldown)

        job = enqueue_job(
            owner_id=owner_id,
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
        dispatch_ndvi_job(job)

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
        dispatch_ndvi_job(job)

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


class CircuitBreakerResetView(APIView):
    """Manually reset a circuit breaker to CLOSED state.

    Auth: IsAdminUser
    Request: {"engine": "stac"|"sentinelhub"|"sentinelhub_raster"}
    Response: envelope with previous/new state
    """

    permission_classes = [IsAdminUser]

    _VALID_ENGINES = ("stac", "sentinelhub", "sentinelhub_raster")

    @extend_schema(
        request=inline_serializer(
            name="CircuitBreakerResetRequest",
            fields={
                "engine": serializers.ChoiceField(
                    choices=["stac", "sentinelhub", "sentinelhub_raster"]
                ),
            },
        ),
        responses={
            200: inline_serializer(
                name="CircuitBreakerResetResponse",
                fields={
                    "success": serializers.IntegerField(),
                    "message": serializers.CharField(),
                    "data": inline_serializer(
                        name="CircuitBreakerResetData",
                        fields={
                            "engine": serializers.CharField(),
                            "previous_state": serializers.CharField(),
                            "new_state": serializers.CharField(),
                        },
                    ),
                },
            ),
            400: error_envelope_serializer("CircuitBreakerResetBadRequest"),
            403: error_envelope_serializer("CircuitBreakerResetForbidden"),
        },
    )
    def post(self, request: Request) -> Response:
        """Reset a circuit breaker to CLOSED state."""

        from ndvi.circuit_breaker import get_circuit_breaker

        engine = request.data.get("engine")
        if engine not in self._VALID_ENGINES:
            return error_response(
                f"Invalid engine. Must be: {', '.join(self._VALID_ENGINES)}",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        cb = get_circuit_breaker(engine)
        if cb is None:
            return error_response(
                f"Circuit breaker for '{engine}' not found. "
                "The engine may not be initialized.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        previous_state = cb.state
        cb.reset()

        return success_response(
            {
                "engine": engine,
                "previous_state": previous_state,
                "new_state": "closed",
            },
            message=f"Circuit breaker for '{engine}' reset to CLOSED",
        )


class UpstreamHealthView(APIView):
    """Return health status of all NDVI upstream services.

    Auth: IsAuthenticated
    Response: envelope with per-engine circuit breaker status
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={
            200: inline_serializer(
                name="UpstreamHealthResponse",
                fields={
                    "success": serializers.IntegerField(),
                    "message": serializers.CharField(),
                    "data": inline_serializer(
                        name="UpstreamHealthData",
                        fields={
                            "engines": serializers.DictField(
                                child=serializers.DictField(),
                            ),
                        },
                    ),
                },
            ),
            401: error_envelope_serializer("UpstreamHealthUnauthorized"),
        },
    )
    def get(self, request: Request) -> Response:
        """Return circuit breaker status for all NDVI engines."""

        from ndvi.circuit_breaker import list_circuit_breakers

        engines: dict[str, dict[str, object]] = {}
        for name, cb in sorted(list_circuit_breakers().items()):
            engines[name] = cb.get_status()

        return success_response(
            cast("dict[str, JSONValue]", {"engines": engines}),
            message="Upstream health status",
        )
