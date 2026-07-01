"""
SageProductRegistry — maps Sage product names to their strategy class triples.

This is the single point of extension for adding new Sage products.
When a new product (e.g. X3, Sage 100) is ready:
  1. Add a sub-package under connector_runtime/adapters/sage/products/
  2. Implement the three Protocol interfaces (auth, query, metadata).
  3. Register the triple here under the product name.
  4. Add the name to SUPPORTED_SAGE_PRODUCTS.

No changes to SageConnector, ConnectorInterface, or any existing connector.

Security (OWASP A03):
  - sage_product from connector_params is validated against SUPPORTED_SAGE_PRODUCTS
    (a frozenset whitelist) before being used as a dict key.  An arbitrary string
    cannot route to arbitrary code — only registered product names are accepted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from connector_runtime.adapters.sage.protocols.sage_auth_protocol import SageAuthProtocol
    from connector_runtime.adapters.sage.protocols.sage_metadata_protocol import (
        SageMetadataProtocol,
    )
    from connector_runtime.adapters.sage.protocols.sage_query_protocol import SageQueryProtocol


# ---------------------------------------------------------------------------
# Whitelist of accepted sage_product values in connector_params.
# Adding a new product requires adding its name here AND registering below.
# ---------------------------------------------------------------------------

SUPPORTED_SAGE_PRODUCTS: Final[frozenset[str]] = frozenset(
    {
        "intacct",
        "x3",
        # Future products added here:
        # "accounting",
        # "100",
        # "200",
        # "300",
    }
)


@dataclass(frozen=True)
class SageProductStrategies:
    """
    Holds the three strategy classes for a registered Sage product.

    Each class is instantiated by SageConnector at construction time with
    the shared SageCredentialManager and SageHttpClient instances injected.
    Frozen so that registered strategies cannot be mutated after registration.
    """

    auth_class: type  # Must implement SageAuthProtocol
    query_engine_class: type  # Must implement SageQueryProtocol
    metadata_client_class: type  # Must implement SageMetadataProtocol


class SageProductRegistryError(Exception):
    """Raised when a product name is unknown or its strategies are misconfigured."""


def resolve_product_strategies(sage_product: str) -> SageProductStrategies:
    """
    Return the strategy classes registered for the given Sage product name.

    Args:
        sage_product: Validated product name (must be in SUPPORTED_SAGE_PRODUCTS).

    Returns:
        SageProductStrategies with auth, query, and metadata classes.

    Raises:
        SageProductRegistryError: if sage_product is not in the registry.
    """
    if sage_product not in _REGISTRY:
        raise SageProductRegistryError(
            f"No strategy classes registered for Sage product {sage_product!r}. "
            f"Supported products: {sorted(SUPPORTED_SAGE_PRODUCTS)}. "
            "To add a new product, implement the three protocols and register them "
            "in sage_product_registry._REGISTRY."
        )
    return _REGISTRY[sage_product]


def _register_product(name: str, strategies: SageProductStrategies) -> None:
    """
    Internal helper: register a product's strategies at module import time.

    Import-time registration (called at the bottom of this module) mirrors
    the connector_registry.register() pattern used by the platform adapters.
    Not exposed publicly — extension happens by editing this module only.
    """
    if name in _REGISTRY:
        raise SageProductRegistryError(
            f"Sage product {name!r} is already registered. "
            "Each product name must map to exactly one strategy triple."
        )
    _REGISTRY[name] = strategies


# ---------------------------------------------------------------------------
# Internal registry dict — populated below at module load time.
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, SageProductStrategies] = {}

# ---------------------------------------------------------------------------
# Registrations — one block per product.
# Import product modules here ONLY (not at the top of the file) to avoid
# making every product a hard dependency when only one product is in use.
# ---------------------------------------------------------------------------


def _register_all() -> None:
    """Register all supported Sage product strategies. Called once at module load."""
    # ── Sage Intacct ──────────────────────────────────────────────────────────
    from connector_runtime.adapters.sage.products.intacct.intacct_auth import IntacctAuthClient
    from connector_runtime.adapters.sage.products.intacct.intacct_metadata_client import (
        IntacctMetadataClient,
    )
    from connector_runtime.adapters.sage.products.intacct.intacct_query_engine import (
        IntacctQueryEngine,
    )

    _register_product(
        "intacct",
        SageProductStrategies(
            auth_class=IntacctAuthClient,
            query_engine_class=IntacctQueryEngine,
            metadata_client_class=IntacctMetadataClient,
        ),
    )

    # ── Sage X3 ───────────────────────────────────────────────────────────────
    from connector_runtime.adapters.sage.products.x3.x3_auth import X3AuthClient
    from connector_runtime.adapters.sage.products.x3.x3_metadata_client import X3MetadataClient
    from connector_runtime.adapters.sage.products.x3.x3_query_engine import X3QueryEngine

    _register_product(
        "x3",
        SageProductStrategies(
            auth_class=X3AuthClient,
            query_engine_class=X3QueryEngine,
            metadata_client_class=X3MetadataClient,
        ),
    )

    # ── Future products ───────────────────────────────────────────────────────
    # Example (when ready):
    # from connector_runtime.adapters.sage.products.accounting.accounting_auth import AccountingAuthClient
    # ...
    # _register_product("accounting", SageProductStrategies(...))


_register_all()
