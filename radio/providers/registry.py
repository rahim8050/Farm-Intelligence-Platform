from __future__ import annotations

from radio.providers.base import RadioProvider

PROVIDER_REGISTRY: dict[str, type[RadioProvider]] = {}


def register_provider(slug: str, provider_class: type[RadioProvider]) -> None:
    """Register a provider class."""
    PROVIDER_REGISTRY[slug] = provider_class


def get_provider(slug: str) -> RadioProvider:
    """Get provider instance by slug."""
    provider_class = PROVIDER_REGISTRY.get(slug)
    if not provider_class:
        raise ValueError(f"Unknown provider: {slug}")
    return provider_class()


def get_available_providers() -> list[str]:
    """Get list of available provider slugs."""
    return list(PROVIDER_REGISTRY.keys())
