"""Podcast API endpoints.

Auth: Public read endpoints (list / detail / episodes / stream).
The ``POST /api/v1/podcasts/<id>/refresh/`` endpoint requires
``IsAuthenticated`` (JWT or API key) and is throttled to
``podcasts_refresh`` (default 5/min).
Response: All responses use the project ``success_response`` envelope.
"""

from __future__ import annotations

from typing import cast

from django.contrib.auth.models import AbstractBaseUser, AnonymousUser
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from config.api.openapi import (
    error_envelope_serializer,
    success_envelope_serializer,
)
from config.api.responses import success_response
from podcasts.models import Podcast, PodcastEpisode
from podcasts.serializers import (
    PodcastEpisodeSerializer,
    PodcastEpisodeStreamSerializer,
    PodcastSerializer,
)
from podcasts.services import (
    get_refresh_timeout_seconds,
    ingest_podcast,
    list_episodes_for_podcast,
)

PodcastListEnvelope = success_envelope_serializer(
    "PodcastListEnvelope", data=PodcastSerializer(many=True)
)
PodcastDetailEnvelope = success_envelope_serializer(
    "PodcastDetailEnvelope", data=PodcastSerializer()
)
PodcastEpisodeListEnvelope = success_envelope_serializer(
    "PodcastEpisodeListEnvelope",
    data=PodcastEpisodeSerializer(many=True),
)
PodcastEpisodeStreamEnvelope = success_envelope_serializer(
    "PodcastEpisodeStreamEnvelope",
    data=PodcastEpisodeStreamSerializer(),
)
RefreshReportEnvelope = success_envelope_serializer(
    "PodcastRefreshReportEnvelope",
    data=inline_serializer(
        name="PodcastRefreshReport",
        fields={
            "podcast_id": serializers.CharField(),
            "episodes_seen": serializers.IntegerField(),
            "episodes_created": serializers.IntegerField(),
            "episodes_updated": serializers.IntegerField(),
            "error": serializers.CharField(),
        },
    ),
)
podcasts_error_envelope = error_envelope_serializer("PodcastsErrorResponse")


def _get_podcast_or_404(podcast_id: str) -> Podcast:
    podcast = (
        Podcast.objects.filter(id=podcast_id, is_active=True)
        .prefetch_related("episodes")
        .first()
    )
    if podcast is None:
        raise NotFound("Podcast not found")
    return podcast


def _get_episode_or_404(
    episode_id: int, *, podcast: Podcast | None = None
) -> PodcastEpisode:
    qs = PodcastEpisode.objects.select_related("podcast")
    if podcast is not None:
        qs = qs.filter(podcast=podcast)
    episode = qs.filter(id=episode_id).first()
    if episode is None:
        raise NotFound("Episode not found")
    return episode


class PodcastListView(APIView):
    """List all active podcasts.

    Auth: Public.
    Throttle: None (public endpoint).
    Response: envelope with ``data`` = list of ``PodcastSerializer``.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        responses={200: PodcastListEnvelope},
        summary="List podcasts",
        description="Returns all active podcasts.",
        operation_id="v1_podcasts_list",
    )
    def get(self, request: Request) -> Response:
        """Return all active podcasts, alphabetised by title."""
        podcasts = Podcast.objects.filter(is_active=True).order_by("title")
        return success_response(
            PodcastSerializer(podcasts, many=True).data,
            message="Podcasts retrieved successfully",
        )


class PodcastDetailView(APIView):
    """Get a single podcast's details.

    Auth: Public.
    Throttle: None.
    Response: envelope with ``data`` = ``PodcastSerializer``.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        responses={
            200: PodcastDetailEnvelope,
            404: podcasts_error_envelope,
        },
        summary="Get podcast details",
        description="Returns details for a specific podcast.",
        operation_id="v1_podcasts_retrieve",
    )
    def get(self, request: Request, podcast_id: str) -> Response:
        """Return one podcast by id."""
        podcast = _get_podcast_or_404(podcast_id)
        return success_response(
            PodcastSerializer(podcast).data,
            message="Podcast retrieved successfully",
        )


class PodcastEpisodeListView(APIView):
    """List episodes for a podcast.

    Auth: Public.
    Throttle: None.
    Query params: ``limit`` (int, 1..500, default 100).
    Response: envelope with ``data`` = list of ``PodcastEpisodeSerializer``.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        responses={
            200: PodcastEpisodeListEnvelope,
            400: podcasts_error_envelope,
            404: podcasts_error_envelope,
        },
        summary="List podcast episodes",
        description=(
            "Returns the most recent ``limit`` episodes for a podcast "
            "(default 100, max 500)."
        ),
        operation_id="v1_podcasts_episodes_list",
    )
    def get(self, request: Request, podcast_id: str) -> Response:
        """Return episodes for one podcast, newest first."""
        podcast = _get_podcast_or_404(podcast_id)
        try:
            limit = int(request.query_params.get("limit", "100"))
        except (TypeError, ValueError):
            return success_response(
                data=None,
                message="Invalid limit",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        if limit < 1 or limit > 500:
            return success_response(
                data=None,
                message="limit must be between 1 and 500",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        rows = list_episodes_for_podcast(podcast, limit=limit)
        return success_response(
            PodcastEpisodeSerializer(rows, many=True).data,
            message="Episodes retrieved successfully",
        )


class PodcastEpisodeStreamView(APIView):
    """Get the audio URL and metadata for a single episode.

    Auth: Public.
    Throttle: None.
    Response: envelope with ``data`` = ``PodcastEpisodeStreamSerializer``.
    Errors:
        - 404 if the episode does not exist.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        responses={
            200: PodcastEpisodeStreamEnvelope,
            404: podcasts_error_envelope,
        },
        summary="Get episode stream URL",
        description=(
            "Returns the audio URL, format hint, and episode "
            "metadata for playback."
        ),
        operation_id="v1_podcasts_episode_stream",
    )
    def get(self, request: Request, episode_id: int) -> Response:
        """Return the audio URL and metadata for an episode."""
        episode = _get_episode_or_404(episode_id)
        return success_response(
            {
                "audio_url": episode.audio_url,
                "format": (
                    episode.audio_mime_type
                    or _guess_format_from_url(episode.audio_url)
                ),
                "duration_seconds": episode.duration_seconds,
                "episode_title": episode.title,
                "podcast_title": episode.podcast.title,
                "podcast_id": episode.podcast.id,
            },
            message="Episode stream URL retrieved successfully",
        )


def _guess_format_from_url(url: str) -> str:
    """Best-effort format guess from the audio URL extension."""
    lowered = url.lower()
    for ext, label in (
        (".mp3", "audio/mpeg"),
        (".m4a", "audio/mp4"),
        (".aac", "audio/aac"),
        (".ogg", "audio/ogg"),
        (".oga", "audio/ogg"),
        (".opus", "audio/opus"),
        (".wav", "audio/wav"),
    ):
        if ext in lowered:
            return label
    return "audio/mpeg"


@extend_schema(
    auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
)
class PodcastRefreshView(APIView):
    """Manually trigger an ingestion pass for a single podcast.

    Authentication: BearerAuth (JWT) or ApiKeyAuth.
    Permissions: IsAuthenticated.
    Throttling: scope ``podcasts_refresh`` (5/min).
    Side effects: fetches ``podcast.feed_url`` and upserts episodes.
    Response: envelope with ``data`` = the :class:`IngestionReport`.
    """

    permission_classes = [IsAuthenticated]
    throttle_scope = "podcasts_refresh"

    @extend_schema(
        responses={
            200: RefreshReportEnvelope,
            401: podcasts_error_envelope,
            404: podcasts_error_envelope,
        },
        summary="Refresh a podcast feed",
        description=(
            "Triggers an immediate ingestion of the podcast's "
            "upstream feed. Auth required."
        ),
        operation_id="v1_podcasts_refresh",
    )
    def post(self, request: Request, podcast_id: str) -> Response:
        """Run :func:`podcasts.services.ingest_podcast` for one podcast."""
        podcast = _get_podcast_or_404(podcast_id)
        _ = cast(
            AbstractBaseUser | AnonymousUser,
            getattr(request, "user", AnonymousUser()),
        )
        report = ingest_podcast(
            podcast, timeout_seconds=get_refresh_timeout_seconds()
        )
        return success_response(
            report.to_dict(),
            message=(
                "Refresh completed" if not report.error else "Refresh failed"
            ),
            status_code=(
                status.HTTP_200_OK
                if not report.error
                else status.HTTP_502_BAD_GATEWAY
            ),
        )
