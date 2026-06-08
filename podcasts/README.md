# Podcasts App

Back to root: `../README.md`

## Overview

This app provides public podcast metadata and per-episode audio URLs for the
Django + Nextcloud ecosystem.

It is responsible for:
- A small catalogue of podcasts mirrored from public RSS/Atom feeds
- Per-episode metadata (title, audio URL, duration, published date)
- Periodic feed refresh via a Celery task
- Manual on-demand refresh (auth'd)

It is not responsible for:
- Audio proxying or transcoding
- Subscription management or push notifications
- Per-user playback history (use the `radio/` listening-history hooks)

## API surface

Base path: `/api/v1/podcasts/` (from code: `podcasts/urls.py` and
`config/urls.py`).

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
| GET | `/api/v1/podcasts/` | Public | List all active podcasts (alphabetised by title) |
| GET | `/api/v1/podcasts/<podcast_id>/` | Public | Get podcast details |
| GET | `/api/v1/podcasts/<podcast_id>/episodes/?limit=N` | Public | List episodes (default 100, max 500) |
| GET | `/api/v1/podcasts/episodes/<episode_id>/stream/` | Public | Get the audio URL and metadata for one episode |
| POST | `/api/v1/podcasts/<podcast_id>/refresh/` | Auth | Trigger an immediate re-ingestion of the upstream feed |

## Data model

| Model | Notes |
| --- | --- |
| `Podcast` | `id` (short slug, PK), `title`, `author`, `feed_url`, `image_url`, `language`, `is_active`, `last_refreshed_at`, `last_refresh_status` (`ok`/`error`/`""`), `last_refresh_error` |
| `PodcastEpisode` | FK to `Podcast`, `guid` (unique per podcast), `title`, `description`, `audio_url`, `audio_mime_type`, `duration_seconds`, `published_at`, `image_url`. Unique on `(podcast, guid)`. |

The unique constraint is `podcasts_episode_podcast_guid_unique`.

## Feed ingestion

The `podcasts.services.ingest_podcast` function fetches the upstream feed with
`httpx` (15s default timeout) and parses it with `feedparser`. For each
`<item>` / `<entry>` it builds a `PodcastEpisode` field dict; entries that
are missing a guid, title, or audio enclosure are skipped silently. Existing
episodes are matched by `(podcast, guid)` and updated in place when the
fields differ.

The Celery task `podcasts.tasks.refresh_all_feeds` runs hourly (configurable
via `PODCASTS_REFRESH_INTERVAL_SECONDS`) and reports counts in its return
value. A failed feed never blocks the others: each podcast's outcome is
recorded on its own `last_refresh_status` field.

## Sample data

```bash
python manage.py load_sample_podcasts
```

This seeds two well-known public RSS feeds (BBC Global News Podcast, NPR
Up First) and triggers an immediate ingestion pass. Re-running the command
is idempotent.

## Settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `PODCASTS_REFRESH_INTERVAL_SECONDS` | 3600 | Celery beat schedule for `podcasts-refresh-feeds` |
| `PODCASTS_REFRESH_TIMEOUT_SECONDS` | 15.0 | Per-feed HTTP timeout |

## Testing

```bash
pytest podcasts/tests
```

Coverage: model invariants, parser coercion, full ingestion pass with a
mocked httpx client, all five endpoints, and the auth'd refresh flow.

## Documentation

See `docs/architecture/radio/IMPLEMENTATION_SUMMARY.md` § Phase 4 and
`docs/architecture/radio/08_future_expansion.md` for the P4 roadmap entry.
