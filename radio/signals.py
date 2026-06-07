"""Radio signal handlers.

Wires the station-list cache invalidation to ``Station`` writes so
that admin-curated changes show up on the public list endpoint
within one cache TTL (default 60s) at most.

The handler is registered from :class:`radio.apps.RadioConfig.ready`
to keep the wiring in one place.
"""

from __future__ import annotations

import logging

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from radio.models import Provider, Station
from radio.services import invalidate_station_list_cache

logger = logging.getLogger("radio")


@receiver(post_save, sender=Station)
@receiver(post_delete, sender=Station)
def _invalidate_on_station_change(
    sender: type[Station],
    instance: Station,
    **kwargs: object,
) -> None:
    """Drop the cached station list on any ``Station`` write/delete."""
    invalidate_station_list_cache()
    logger.debug(
        "radio_station_list_cache_invalidated station_id=%s",
        instance.id,
        extra={
            "event": "radio_station_list_cache_invalidated",
            "station_id": instance.id,
        },
    )


@receiver(post_save, sender=Provider)
@receiver(post_delete, sender=Provider)
def _invalidate_on_provider_change(
    sender: type[Provider],
    instance: Provider,
    **kwargs: object,
) -> None:
    """Drop the cached station list on any ``Provider`` write/delete
    because the embedded provider_name / provider_logo_url is part of
    the cached payload."""
    invalidate_station_list_cache()
