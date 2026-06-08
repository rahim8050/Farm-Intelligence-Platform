# Radio App

Back to root: `../README.md`

## Overview

This app provides public radio metadata and stream discovery for the Django +
Nextcloud ecosystem.

It is responsible for:
- Station and provider metadata endpoints
- Stream URL lookup for playback clients
- Periodic station health checks (Phase 2)
- Per-user favorites and listening history (Phase 3)
- Seed data for BBC, SomaFM, and TuneIn stations

It is not responsible for:
- Audio proxying or transcoding
- Authentication or authorization
- Radio playback state on the client

## API surface

Base path: `/api/v1/radio/` (from code: `radio/urls.py` and `config/urls.py`).

All successful responses use the project envelope produced by
`config.api.responses.success_response`:

```json
{
  "status": 0,
  "message": "string",
  "data": {},
  "errors": null,
  "request_id": "req_..."
}
```

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/api/v1/radio/stations/` | Public | List all active stations |
| GET | `/api/v1/radio/stations/<station_id>/` | Public | Get station details |
| GET | `/api/v1/radio/stations/<station_id>/stream/` | Public | Get stream URL and playback metadata; records a listening-history row when authenticated |
| GET | `/api/v1/radio/providers/` | Public | List all active providers |
| GET | `/api/v1/radio/health/` | Public | Aggregate station availability |
| GET | `/api/v1/radio/favorites/` | Auth | List current user's favorite stations |
| POST | `/api/v1/radio/favorites/` | Auth | Add a station to favorites (idempotent) |
| DELETE | `/api/v1/radio/favorites/<station_id>/` | Auth | Remove a station from favorites (idempotent) |
| GET | `/api/v1/radio/history/` | Auth | List current user's listening history (newest first, capped 100) |
| GET | `/api/v1/radio/history/recent/?limit=N` | Auth | Most recent N history rows (default 20, max 100) |

## Seed Data

Initial station/provider records are loaded via:

```bash
python manage.py load_stations
```

The current seed set includes BBC, SomaFM, and TuneIn providers.

## Testing

```bash
pytest radio/tests
```

## Documentation

See `docs/architecture/radio/` for full architecture documentation.
