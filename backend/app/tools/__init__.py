"""Business tools with lazy registry exports, so leaf modules stay importable."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.tools.registry import ToolRegistry, ToolRegistryError, build_tool_registry

__all__ = ["ToolRegistry", "ToolRegistryError", "build_tool_registry"]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from app.tools import registry

    value = getattr(registry, name)
    globals()[name] = value
    return value
