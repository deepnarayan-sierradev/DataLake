"""
Registry of set-based query engines (FR-F0.1).

Same decorator-registry pattern as connector_runtime.registry and
serving_store.registry: an engine class registers under a name and is built by
name at runtime, so selecting DuckDB vs Athena vs Glue per tenant/entity is a
config lookup, not a code branch.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from processing_engine.interfaces.set_based_engine_interface import SetBasedQueryEngine

_EngineClass = TypeVar("_EngineClass", bound=type[SetBasedQueryEngine])


class SetBasedEngineRegistry:
    """Maps an engine name to its SetBasedQueryEngine class."""

    def __init__(self) -> None:
        self._engines: dict[str, type[SetBasedQueryEngine]] = {}

    def register(self, engine_name: str) -> Callable[[_EngineClass], _EngineClass]:
        def _decorator(cls: _EngineClass) -> _EngineClass:
            if engine_name in self._engines:
                raise ValueError(f"Engine {engine_name!r} is already registered.")
            self._engines[engine_name] = cls
            return cls

        return _decorator

    def build(self, engine_name: str, **kwargs: object) -> SetBasedQueryEngine:
        cls = self._engines.get(engine_name)
        if cls is None:
            raise ValueError(
                f"No set-based engine registered for {engine_name!r}. "
                f"Known engines: {sorted(self._engines)}."
            )
        return cls(**kwargs)

    def known_engines(self) -> frozenset[str]:
        return frozenset(self._engines)


set_based_engine_registry = SetBasedEngineRegistry()
