"""
Serving store loader registry — plugin-style engine adapter registration.

Mirrors connector_runtime/registry.py's pattern on the write side. Adapters
register themselves at import time; the handler resolves the correct adapter
for a given target_engine at load time.

Usage (in adapter module):
    from serving_store.registry import serving_store_registry
    from serving_store.interfaces.loader_interface import ServingStoreLoaderInterface

    @serving_store_registry.register("postgresql")
    class PostgreSqlLoader(ServingStoreLoaderInterface):
        ...

Usage (in handler):
    from serving_store.registry import serving_store_registry
    loader = serving_store_registry.resolve("postgresql", secret_arn=..., region_name=...)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from serving_store.interfaces.loader_interface import ServingStoreLoaderInterface


class ServingStoreLoaderRegistry:
    """Plugin registry mapping target_engine → ServingStoreLoaderInterface class."""

    def __init__(self) -> None:
        self._registry: dict[str, type[ServingStoreLoaderInterface]] = {}

    def register(
        self, engine_id: str
    ) -> Callable[[type[ServingStoreLoaderInterface]], type[ServingStoreLoaderInterface]]:
        """
        Class decorator that registers a ServingStoreLoaderInterface implementation.

        Raises:
            ValueError: If engine_id is already registered (prevents silent override).
        """

        def decorator(
            cls: type[ServingStoreLoaderInterface],
        ) -> type[ServingStoreLoaderInterface]:
            if engine_id in self._registry:
                raise ValueError(
                    f"Serving store loader for engine_id '{engine_id}' is already "
                    f"registered by {self._registry[engine_id].__name__}."
                )
            self._registry[engine_id] = cls
            return cls

        return decorator

    def resolve(self, engine_id: str, **kwargs: Any) -> ServingStoreLoaderInterface:
        """
        Resolve and instantiate the loader for the given target_engine.

        Raises:
            KeyError: If no loader is registered for engine_id.
        """
        if engine_id not in self._registry:
            registered = sorted(self._registry.keys())
            raise KeyError(
                f"No serving store loader registered for engine_id '{engine_id}'. "
                f"Registered engines: {registered}."
            )
        loader = self._registry[engine_id](**kwargs)
        loader.engine_id = engine_id
        return loader


serving_store_registry = ServingStoreLoaderRegistry()
