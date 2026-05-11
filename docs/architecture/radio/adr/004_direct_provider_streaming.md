# ADR 004: Direct Provider Streaming

## Status

Accepted

## Context

How does audio reach the client?

## Options Considered

### 1. Django → Client

Django acts as intermediary.

### 2. Provider → Client (Selected)

Client connects directly to radio provider.

## Decision

Direct streaming from provider to client.

## Architecture

```
[Nextcloud]  --metadata API-->  [Django]
     |                               |
     |  stream URL                   |
     v                               v
[Provider] <----------------------- [Django]
```

## Rationale

1. **No bottleneck**: Django doesn't become the streaming bottleneck
2. **Lower latency**: Direct connection, no proxy overhead
3. **Provider reliability**: If provider is up, streams work even if Django has issues
4. **Cost**: No bandwidth costs to Django server
5. **Protocol support**: Providers optimize for streaming (HLS, DASH, etc.)

## Consequences

- Stream URLs must be publicly accessible
- Client must handle various streaming formats
- No server-side caching of audio

## Related ADRs

- ADR 001: Dedicated Radio App
- ADR 002: No Stream Proxying
- ADR 003: Metadata APIs Preferred