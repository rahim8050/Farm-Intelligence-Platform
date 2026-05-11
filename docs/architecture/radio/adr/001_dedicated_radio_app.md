# ADR 001: Dedicated Radio App

## Status

Accepted

## Context

We need to integrate internet radio streaming (starting with BBC 1Xtra) into the Django + Nextcloud ecosystem. The question is where to place this functionality.

## Options Considered

### 1. Add to existing `weather` app

- **Pros**: Reuses existing app infrastructure
- **Cons**: Violates single responsibility; unrelated domain

### 2. Add to existing `activities` app

- **Pros**: Existing API patterns
- **Cons**: Radio is live/continuous; activities are event-driven

### 3. Standalone `radio` app (Selected)

- **Pros**: Clear ownership, independent deployment, future extensibility
- **Cons**: Additional app to maintain

## Decision

Create a new `radio/` Django app.

## Rationale

1. **Domain separation**: Radio is fundamentally different from weather/activities
2. **Scalability**: Can be deployed/scaled independently
3. **Maintenance**: Smaller scope per app = easier to test/maintain
4. **Future growth**: Easy to add podcasts, favorites, history without cluttering other apps

## Consequences

- New Django app under `radio/`
- New URL prefix `/api/v1/radio/`
- Shared authentication (project-level) but independent from other apps

## Related ADRs

- ADR 002: No Stream Proxying
- ADR 003: Metadata APIs Preferred
- ADR 004: Direct Provider Streaming