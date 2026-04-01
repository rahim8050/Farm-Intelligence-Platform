from __future__ import annotations

from config import settings
from config.celery import app as celery_app


def test_parse_cache_url_and_build_sentinel_options() -> None:
    sentinel_url = (
        "redis-sentinel://sentinel1:26379;sentinel2:26380/2?"
        "service_name=mymaster&socket_timeout=1.5"
    )
    location, sentinel_config = settings._parse_cache_url(sentinel_url)
    assert location == "redis://mymaster/2"
    assert sentinel_config is not None

    cache = settings._redis_cache(location, sentinel_config)
    options = cache["OPTIONS"]
    assert cache["LOCATION"] == location
    assert options["CLIENT_CLASS"] == "django_redis.client.SentinelClient"
    assert ("sentinel1", 26379) in options["SENTINELS"]
    assert ("sentinel2", 26380) in options["SENTINELS"]
    assert options["SENTINEL_KWARGS"]["socket_timeout"] == 1.5


def test_parse_celery_sentinel_url_and_transport_options() -> None:
    sentinel_url = (
        "redis-sentinel://sentinel1:26379;sentinel2:26380/1?"
        "service_name=mymaster&socket_timeout=1.5"
    )

    url, transport_options = settings._parse_celery_redis_url(
        sentinel_url,
        '{"visibility_timeout": 30}',
        "CELERY_BROKER_TRANSPORT_OPTIONS",
    )

    assert url == "sentinel://sentinel1:26379/1;sentinel://sentinel2:26380/1"
    assert transport_options["master_name"] == "mymaster"
    assert transport_options["visibility_timeout"] == 30
    assert transport_options["sentinel_kwargs"]["socket_timeout"] == 1.5


def test_celery_app_uses_translated_sentinel_urls() -> None:
    assert celery_app.conf.broker_write_url == settings.CELERY_BROKER_URL
    assert celery_app.conf.broker_read_url == settings.CELERY_BROKER_URL
    assert (
        celery_app.backend.as_uri(include_password=False)
        == settings.CELERY_RESULT_BACKEND
    )
