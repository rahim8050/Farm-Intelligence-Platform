from __future__ import annotations

from config import settings


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
