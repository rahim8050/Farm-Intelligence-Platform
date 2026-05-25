# Radio App

Back to root: `../README.md`

## Overview

This app provides public radio metadata and stream discovery for the Django +
Nextcloud ecosystem.

It is responsible for:
- Station and provider metadata endpoints
- Stream URL lookup for playback clients
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
{ "status": 0, "message": "string", "data": {}, "errors": null }
```

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/api/v1/radio/stations/` | Public | List all active stations |
| GET | `/api/v1/radio/stations/<station_id>/` | Public | Get station details |
| GET | `/api/v1/radio/stations/<station_id>/stream/` | Public | Get stream URL and playback metadata |
| GET | `/api/v1/radio/providers/` | Public | List all active providers |

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
