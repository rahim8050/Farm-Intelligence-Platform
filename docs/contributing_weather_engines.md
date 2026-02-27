# Contributing Weather Engines

This guide explains how to add a new weather provider engine for the
`/api/v1/weather/` endpoints (`current`, `daily`, `weekly`) using the existing
engine/service architecture.

## Architecture at a glance

1) **Views** (`weather/views.py`) are sync DRF APIViews and validate query
   params with DRF serializers.
2) **Services** (`weather/services.py`) handle provider selection, caching,
   metrics, and weekly aggregation. Services call provider engines asynchronously.
3) **Engines** (`weather/engines/*`) talk to upstream providers and normalize
   responses into the shared dataclasses in `weather/engines/types.py`.
4) **Serializers** (`weather/serializers.py`) convert dataclasses into JSON
   payloads used in the response envelope.

The views bridge async service calls using `async_to_sync`, so engines and
services should stay async.

## Engine contract (must follow)

Engines implement the abstract base in `weather/engines/base.py`:

- `name: ProviderName`
- `async def current(self, loc: Location) -> CurrentWeather`
- `async def daily(self, loc: Location, start: date, end: date) -> Sequence[DailyForecast]`

Normalized types live in `weather/engines/types.py`:

- `Location(lat: float, lon: float, tz: str)`
- `CurrentWeather(observed_at: datetime, temperature_c, wind_speed_mps, source)`
- `DailyForecast(day: date, t_min_c, t_max_c, precipitation_mm, wind_speed_max_mps, source)`
- `WeeklyReport(...)` is created by the service layer, not by providers.

Units used across engines:

- Temperatures are Celsius (`temperature_c`, `t_min_c`, `t_max_c`).
- Wind speed is meters/second (`wind_speed_mps`, `wind_speed_max_mps`).
- Precipitation is millimeters (`precipitation_mm`).

Timezone handling:

- Use `weather/timeutils.get_zone()` to validate `loc.tz`.
- Ensure `CurrentWeather.observed_at` is timezone-aware. See
  `weather/timeutils.ensure_aware()` and `isoformat_with_tz()`.
- `DailyForecast.day` should be in the requested timezone (not UTC unless the
  request timezone is UTC), because weekly aggregation uses those dates.

## Step-by-step: add a new provider

### 1) Add the provider name

Update the provider allowlist and types:

- `weather/engines/types.py`: add your name to `ProviderName`.
- `weather/serializers.py`: add it to
  `BaseWeatherParamsSerializer._allowed_providers()`.

Provider names are lowercase and used in query params (`provider=...`) and in
metrics labels, so keep them stable.

### 2) Implement the engine

Create `weather/engines/<provider>.py` and implement `WeatherProvider`:

- Use `httpx.AsyncClient` (or equivalent) for async HTTP calls.
- Call `response.raise_for_status()` and validate payload shapes.
- Normalize values into the dataclasses from `weather/engines/types.py`.
- If a provider only supports daily data (like NASA POWER), `current()` can be
  derived from the most recent daily value.

Refer to existing implementations:

- `weather/engines/open_meteo.py`
- `weather/engines/nasa_power.py`

### 3) Register the provider

Add the provider to the registry in `weather/engines/registry.py`:

```python
providers: dict[ProviderName, WeatherProvider] = {
    "open_meteo": OpenMeteoProvider(),
    "nasa_power": NasaPowerProvider(),
    "example": ExampleProvider(),
}
```

The service layer uses this registry for all endpoints.

### 4) Add settings (if needed)

If the provider requires a base URL or API key:

- Add `WEATHER_<PROVIDER>_BASE_URL` or similar to `config/settings.py`.
- Read secrets from environment variables only (never hardcode).

### 5) Add tests

Tests should avoid live network calls by monkeypatching the engine’s request
method (see `weather/tests/test_weather.py`):

- Provider parsing tests (current and daily).
- Service-level tests if you change provider selection or defaults.

`asyncio.run()` is used in tests to call async engines and services.

## How requests flow (current/daily/weekly)

### current

`GET /api/v1/weather/current/`

1) `WeatherCurrentView` validates query params with
   `BaseWeatherParamsSerializer`.
2) `get_current_weather()` selects the provider, checks cache, updates metrics,
   and calls `provider.current()`.
3) Response data is serialized by `serialize_current()` and wrapped by
   `success_response`.

### daily

`GET /api/v1/weather/daily/`

1) `WeatherDailyView` validates query params with
   `RangeWeatherParamsSerializer`.
2) `get_daily_forecast()` performs validation and caching, then calls
   `provider.daily()`.
3) Response data is serialized by `serialize_daily()` and wrapped by
   `success_response`.

### weekly

`GET /api/v1/weather/weekly/`

1) `WeatherWeeklyView` validates the date range, then calls
   `get_weekly_report()`.
2) Weekly reports are derived from daily forecasts via `_aggregate_weekly()`.
3) Response data is serialized by `serialize_weekly()` and wrapped by
   `success_response`.

The response envelope is produced by `config/api/responses.success_response`
and includes `status`, `message`, `data`, and `errors`.

## Example engine skeleton

This skeleton mirrors patterns from `open_meteo.py` and `nasa_power.py`:

```python
from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from django.conf import settings

from .base import WeatherProvider
from .types import CurrentWeather, DailyForecast, Location, ProviderName
from ..timeutils import ensure_aware, get_zone


class ExampleEngine(WeatherProvider):
    name: ProviderName = "example"

    def __init__(self, *, base_url: str | None = None) -> None:
        self.base_url = base_url or getattr(
            settings,
            "WEATHER_EXAMPLE_BASE_URL",
            "https://api.example.com/weather",
        )

    async def current(self, loc: Location) -> CurrentWeather:
        zone = get_zone(loc.tz)
        payload = await self._request(
            {"lat": loc.lat, "lon": loc.lon, "tz": loc.tz}
        )
        observed = self._parse_datetime(payload.get("observed_at"), zone)
        temperature = self._to_float(payload.get("temperature_c"))
        wind = self._to_float(payload.get("wind_speed_mps"))
        return CurrentWeather(
            observed_at=observed,
            temperature_c=temperature,
            wind_speed_mps=wind,
            source=self.name,
        )

    async def daily(
        self, loc: Location, start: date, end: date
    ) -> Sequence[DailyForecast]:
        get_zone(loc.tz)  # validate tz
        payload = await self._request(
            {
                "lat": loc.lat,
                "lon": loc.lon,
                "start": start.isoformat(),
                "end": end.isoformat(),
            }
        )
        days = payload.get("days", [])
        forecasts: list[DailyForecast] = []
        for item in days:
            day = self._parse_date(item.get("day"))
            if day is None:
                continue
            forecasts.append(
                DailyForecast(
                    day=day,
                    t_min_c=self._to_float(item.get("t_min_c")),
                    t_max_c=self._to_float(item.get("t_max_c")),
                    precipitation_mm=self._to_float(
                        item.get("precipitation_mm")
                    ),
                    source=self.name,
                )
            )
        return forecasts

    async def _request(self, params: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(self.base_url, params=params)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Unexpected ExampleEngine response shape")
        return data

    def _parse_datetime(self, raw: Any, zone: ZoneInfo) -> datetime:
        if not isinstance(raw, str):
            return ensure_aware(datetime.utcnow(), zone)
        return ensure_aware(datetime.fromisoformat(raw), zone)

    def _parse_date(self, raw: Any) -> date | None:
        if not isinstance(raw, str):
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None

    def _to_float(self, raw: Any) -> float | None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
```

## Quick checklist before you open a PR

- Provider name added to `ProviderName` and serializer allowlist.
- Engine registered in `weather/engines/registry.py`.
- Engine methods are async and return normalized dataclasses.
- Tests added (no network calls).
- Settings wired through environment variables as needed.
