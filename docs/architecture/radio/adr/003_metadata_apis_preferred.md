# ADR 003: Metadata APIs Preferred

## Status

Accepted

## Context

What should Django's radio API provide?

## Options Considered

### 1. Serve audio directly

Django hosts audio files or proxies streams.

### 2. Serve metadata only (Selected)

Django returns station info, stream URLs, but not audio.

## Decision

Django serves metadata APIs only.

## Rationale

1. **Separation of concerns**: API vs streaming are different protocols
2. **Statelessness**: REST is stateless; streaming is stateful
3. **Scalability**: API and streaming scale differently
4. **Provider dependency**: Most radio providers already serve streams

## API Examples

```json
{
  "success": 0,
  "message": "Station retrieved",
  "data": {
    "id": "bbc_1xtra",
    "name": "BBC 1Xtra",
    "stream_url": "http://stream.live.vc.bbcmedia.co.uk/bbc_1xtra",
    "format": "MP3",
    "bitrate": 128
  }
}
```

## Consequences

- Client retrieves stream URL from API
- Client connects directly to provider for audio
- Django focuses on discovery and metadata

## Related ADRs

- ADR 001: Dedicated Radio App
- ADR 002: No Stream Proxying
- ADR 004: Direct Provider Streaming