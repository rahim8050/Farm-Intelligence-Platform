# ADR 002: No Stream Proxying

## Status

Accepted

## Context

We need to deliver audio streams to clients. Should Django proxy the audio?

## Options Considered

### 1. Proxy through Django

Django downloads stream from provider and forwards to client.

```python
def stream_audio(request, station_id):
    stream_url = get_stream_url(station_id)
    response = requests.get(stream_url, stream=True)
    return StreamingHttpResponse(response.iter_content())
```

### 2. Direct provider streaming (Selected)

Client connects directly to provider; Django only provides metadata.

## Decision

No stream proxying. Django returns metadata only; audio flows directly from provider to client.

## Rationale

| Factor | Proxy | Direct |
|--------|-------|--------|
| Django load | O(listeners) | O(0) |
| Latency | +1 hop | Baseline |
| Bandwidth | Server pays | Client pays |
| Reliability | Single point | Resilient |
| Scalability | Poor | Excellent |

## Consequences

- Django API returns only metadata (stream URLs, station info)
- Client is responsible for direct HTTP connection to provider
- No transcoding or buffering in Django

## Related ADRs

- ADR 001: Dedicated Radio App
- ADR 003: Metadata APIs Preferred
- ADR 004: Direct Provider Streaming