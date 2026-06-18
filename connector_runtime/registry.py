"""
Connector registry — plugin-style adapter registration.

Adapters register themselves at import time. The runtime resolves the correct
adapter for a given source_id at pipeline execution time.

Usage (in adapter module):
    from connector_runtime.registry import connector_registry
    from connector_runtime.interfaces.connector_interface import ConnectorInterface

    @connector_registry.register("salesforce")
    class SalesforceConnector(ConnectorInterface):
        ...

Usage (in runtime):
    from connector_runtime.registry import connector_registry
    connector = connector_registry.resolve("salesforce")
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from connector_runtime.interfaces.connector_interface import ConnectorInterface

# Type alias for a connector builder callable.
# Signature: (environment, region_name, connector_params, raw_s3_bucket) -> (connector, writer)
# The writer return type is Any to avoid importing orchestration from connector_runtime
# (which would create a circular dependency). Writers satisfy RawLayerWriterProtocol
# structurally at the call site.
ConnectorBuilder = Callable[
    [str, str, dict[str, str], str],
    "tuple[ConnectorInterface, Any]",
]


class ConnectorRegistry:
    """
    Thread-safe plugin registry mapping source_id → ConnectorInterface class.

    Registration happens at module import time via the @register decorator.
    Resolution happens at pipeline execution time via resolve().
    """

    def __init__(self) -> None:
        self._registry: dict[str, type[ConnectorInterface]] = {}
        self._builders: dict[str, ConnectorBuilder] = {}

    def register(
        self, source_id: str
    ) -> Callable[[type[ConnectorInterface]], type[ConnectorInterface]]:
        """
        Class decorator that registers a ConnectorInterface implementation.

        Args:
            source_id: The stable source identifier this adapter handles.

        Returns:
            The class decorator function.

        Raises:
            ValueError: If source_id is already registered (prevents silent override).
        """

        def decorator(cls: type[ConnectorInterface]) -> type[ConnectorInterface]:
            if source_id in self._registry:
                raise ValueError(
                    f"Connector for source_id '{source_id}' is already registered "
                    f"by {self._registry[source_id].__name__}. "
                    "Each source_id must map to exactly one connector adapter."
                )
            self._registry[source_id] = cls
            return cls

        return decorator

    def resolve(self, source_id: str, **kwargs: Any) -> ConnectorInterface:
        """
        Resolve and instantiate the connector for the given source_id.

        Constructor arguments for the connector class are forwarded via
        **kwargs, since different adapters require different constructor
        parameters (e.g. environment, region_name, object_name for Salesforce).

        Args:
            source_id: The stable source system identifier.
            **kwargs: Keyword arguments forwarded to the connector constructor.

        Returns:
            A new ConnectorInterface instance for the source.

        Raises:
            KeyError: If no connector is registered for source_id.
        """
        if source_id not in self._registry:
            registered = sorted(self._registry.keys())
            raise KeyError(
                f"No connector registered for source_id '{source_id}'. "
                f"Registered sources: {registered}. "
                "Add a connector adapter and register it with "
                "@connector_registry.register(source_id)."
            )
        return self._registry[source_id](**kwargs)

    @property
    def registered_source_ids(self) -> list[str]:
        """Return the sorted list of registered source IDs."""
        return sorted(self._registry.keys())

    # ── Builder registry ───────────────────────────────────────────────────────

    def register_builder(self, source_id: str, builder: ConnectorBuilder) -> None:
        """
        Register a factory function that fully wires a connector + raw-layer writer.

        Called once per adapter module at import time (after register()):

            def _build_salesforce(env, region, params, bucket):
                ...
                return SalesforceConnector(...), SalesforceRawLayerWriter(...)

            connector_registry.register_builder("salesforce", _build_salesforce)

        Args:
            source_id: The stable source identifier this builder handles.
            builder:   Callable with signature
                       (environment: str, region_name: str,
                        connector_params: dict[str, str], raw_s3_bucket: str)
                       -> tuple[ConnectorInterface, RawLayerWriter]

        Raises:
            ValueError: If a builder for source_id is already registered.
        """
        if source_id in self._builders:
            raise ValueError(
                f"A connector builder for source_id '{source_id}' is already registered. "
                "Each source_id must map to exactly one builder."
            )
        self._builders[source_id] = builder

    def resolve_builder(self, source_id: str) -> ConnectorBuilder:
        """
        Return the registered builder for the given source_id.

        Raises:
            KeyError: If no builder is registered for source_id.
        """
        if source_id not in self._builders:
            registered = sorted(self._builders.keys())
            raise KeyError(
                f"No connector builder registered for source_id '{source_id}'. "
                f"Registered sources: {registered}. "
                "Add a register_builder() call in the adapter module."
            )
        return self._builders[source_id]

    def reset(self) -> None:
        """
        Clear all registered connectors and builders.

        **For testing only.**  Call this in ``setUp`` / ``tearDown`` to prevent
        registration state leaking between test cases that share the module-level
        singleton.  Never call in production code.
        """
        self._registry.clear()
        self._builders.clear()


# Module-level singleton — imported by all adapters and the runtime
connector_registry = ConnectorRegistry()
