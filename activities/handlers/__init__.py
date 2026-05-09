"""Activity handler registry.

Provides handler lookup for activity types.
"""

from __future__ import annotations

# Import HandlerResult from base
from .base import HandlerResult

# Import from registry module
from .registry import (
    HANDLER_REGISTRY,
    ActivityHandler,
    DefaultHandler,
    get_handler,
    register_handler,
)

# Explicit re-exports
__all__ = [
    "ActivityHandler",
    "DefaultHandler",
    "HANDLER_REGISTRY",
    "get_handler",
    "register_handler",
    "HandlerResult",
]


# Lazy import of handler modules to avoid circular imports
def _import_handlers() -> None:
    """Import handler modules to register them."""
    from . import (  # noqa: F401
        fertilizer,
        irrigation,
        ndvi_trigger,
        vaccination,
    )


_import_handlers()
