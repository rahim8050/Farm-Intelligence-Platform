# Radio App

Internet radio streaming integration for Django + Nextcloud ecosystem.

## Overview

Provides REST API endpoints for discovering and streaming radio stations.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/radio/stations/` | List all active stations |
| GET | `/api/v1/radio/stations/{id}/` | Get station details |
| GET | `/api/v1/radio/stations/{id}/stream/` | Get stream URL |
| GET | `/api/v1/radio/providers/` | List providers |

## Response Format

All responses use the standard envelope:

```json
{
    "status": 0,
    "message": "OK",
    "data": { ... },
    "errors": null
}
```

## Authentication

Public access (no authentication required).

## Seed Data

BBC 1Xtra is loaded via management command:

```bash
python manage.py load_stations
```

## Tests

```bash
python manage.py test radio.tests.test_views
```

## Documentation

See `docs/architecture/radio/` for full architecture documentation.