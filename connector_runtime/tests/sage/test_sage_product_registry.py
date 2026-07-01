"""
Tests for SageProductRegistry.

Coverage:
  - SUPPORTED_SAGE_PRODUCTS contains "intacct"
  - SUPPORTED_SAGE_PRODUCTS contains "x3"
  - SUPPORTED_SAGE_PRODUCTS is a frozenset (immutable)
  - resolve_product_strategies("intacct") returns correct class triple
  - resolve_product_strategies("x3") returns correct class triple
  - resolve_product_strategies for unknown product → SageProductRegistryError
  - SageProductStrategies is a frozen dataclass (immutable)
  - Resolved auth_class is IntacctAuthClient / X3AuthClient
  - Resolved query_engine_class is IntacctQueryEngine / X3QueryEngine
  - Resolved metadata_client_class is IntacctMetadataClient / X3MetadataClient
  - Duplicate registration raises SageProductRegistryError
"""

from __future__ import annotations

import pytest

from connector_runtime.adapters.sage.common.sage_product_registry import (
    SUPPORTED_SAGE_PRODUCTS,
    SageProductRegistryError,
    SageProductStrategies,
    _register_product,
    resolve_product_strategies,
)
from connector_runtime.adapters.sage.products.intacct.intacct_auth import IntacctAuthClient
from connector_runtime.adapters.sage.products.intacct.intacct_metadata_client import (
    IntacctMetadataClient,
)
from connector_runtime.adapters.sage.products.intacct.intacct_query_engine import IntacctQueryEngine
from connector_runtime.adapters.sage.products.x3.x3_auth import X3AuthClient
from connector_runtime.adapters.sage.products.x3.x3_metadata_client import X3MetadataClient
from connector_runtime.adapters.sage.products.x3.x3_query_engine import X3QueryEngine


class TestSupportedProducts:
    def test_intacct_in_whitelist(self) -> None:
        assert "intacct" in SUPPORTED_SAGE_PRODUCTS

    def test_x3_in_whitelist(self) -> None:
        assert "x3" in SUPPORTED_SAGE_PRODUCTS

    def test_whitelist_is_frozenset(self) -> None:
        assert isinstance(SUPPORTED_SAGE_PRODUCTS, frozenset)

    def test_whitelist_is_immutable(self) -> None:
        """frozenset must raise TypeError on mutation attempts."""
        with pytest.raises(AttributeError):
            SUPPORTED_SAGE_PRODUCTS.add("new-product")  # type: ignore[attr-defined]


class TestResolveProductStrategies:
    def test_resolve_intacct_returns_strategies(self) -> None:
        strategies = resolve_product_strategies("intacct")
        assert isinstance(strategies, SageProductStrategies)

    def test_intacct_auth_class_is_intacct_auth_client(self) -> None:
        strategies = resolve_product_strategies("intacct")
        assert strategies.auth_class is IntacctAuthClient

    def test_intacct_query_engine_class_is_correct(self) -> None:
        strategies = resolve_product_strategies("intacct")
        assert strategies.query_engine_class is IntacctQueryEngine

    def test_intacct_metadata_client_class_is_correct(self) -> None:
        strategies = resolve_product_strategies("intacct")
        assert strategies.metadata_client_class is IntacctMetadataClient

    def test_resolve_x3_returns_strategies(self) -> None:
        strategies = resolve_product_strategies("x3")
        assert isinstance(strategies, SageProductStrategies)

    def test_x3_auth_class_is_x3_auth_client(self) -> None:
        strategies = resolve_product_strategies("x3")
        assert strategies.auth_class is X3AuthClient

    def test_x3_query_engine_class_is_correct(self) -> None:
        strategies = resolve_product_strategies("x3")
        assert strategies.query_engine_class is X3QueryEngine

    def test_x3_metadata_client_class_is_correct(self) -> None:
        strategies = resolve_product_strategies("x3")
        assert strategies.metadata_client_class is X3MetadataClient

    def test_unknown_product_raises_registry_error(self) -> None:
        with pytest.raises(SageProductRegistryError, match="No strategy classes registered"):
            resolve_product_strategies("nonexistent-erp")

    def test_injection_attempt_raises_registry_error(self) -> None:
        with pytest.raises(SageProductRegistryError):
            resolve_product_strategies("'; DROP TABLE strategies; --")


class TestSageProductStrategies:
    def test_is_frozen_dataclass(self) -> None:
        strategies = resolve_product_strategies("intacct")
        with pytest.raises((AttributeError, TypeError)):
            strategies.auth_class = object  # type: ignore[misc]

    def test_dataclass_equality(self) -> None:
        s1 = resolve_product_strategies("intacct")
        s2 = resolve_product_strategies("intacct")
        assert s1 == s2


class TestDuplicateRegistration:
    def test_duplicate_product_raises(self) -> None:
        strategies = SageProductStrategies(
            auth_class=IntacctAuthClient,
            query_engine_class=IntacctQueryEngine,
            metadata_client_class=IntacctMetadataClient,
        )
        with pytest.raises(SageProductRegistryError, match="already registered"):
            _register_product("intacct", strategies)



class TestSupportedProducts:
    def test_intacct_in_whitelist(self) -> None:
        assert "intacct" in SUPPORTED_SAGE_PRODUCTS

    def test_whitelist_is_frozenset(self) -> None:
        assert isinstance(SUPPORTED_SAGE_PRODUCTS, frozenset)

    def test_whitelist_is_immutable(self) -> None:
        """frozenset must raise TypeError on mutation attempts."""
        with pytest.raises(AttributeError):
            SUPPORTED_SAGE_PRODUCTS.add("new-product")  # type: ignore[attr-defined]


class TestResolveProductStrategies:
    def test_resolve_intacct_returns_strategies(self) -> None:
        strategies = resolve_product_strategies("intacct")
        assert isinstance(strategies, SageProductStrategies)

    def test_intacct_auth_class_is_intacct_auth_client(self) -> None:
        strategies = resolve_product_strategies("intacct")
        assert strategies.auth_class is IntacctAuthClient

    def test_intacct_query_engine_class_is_correct(self) -> None:
        strategies = resolve_product_strategies("intacct")
        assert strategies.query_engine_class is IntacctQueryEngine

    def test_intacct_metadata_client_class_is_correct(self) -> None:
        strategies = resolve_product_strategies("intacct")
        assert strategies.metadata_client_class is IntacctMetadataClient

    def test_unknown_product_raises_registry_error(self) -> None:
        with pytest.raises(SageProductRegistryError, match="No strategy classes registered"):
            resolve_product_strategies("nonexistent-erp")

    def test_injection_attempt_raises_registry_error(self) -> None:
        with pytest.raises(SageProductRegistryError):
            resolve_product_strategies("'; DROP TABLE strategies; --")


class TestSageProductStrategies:
    def test_is_frozen_dataclass(self) -> None:
        strategies = resolve_product_strategies("intacct")
        with pytest.raises((AttributeError, TypeError)):
            strategies.auth_class = object  # type: ignore[misc]

    def test_dataclass_equality(self) -> None:
        s1 = resolve_product_strategies("intacct")
        s2 = resolve_product_strategies("intacct")
        assert s1 == s2


class TestDuplicateRegistration:
    def test_duplicate_product_raises(self) -> None:
        strategies = SageProductStrategies(
            auth_class=IntacctAuthClient,
            query_engine_class=IntacctQueryEngine,
            metadata_client_class=IntacctMetadataClient,
        )
        with pytest.raises(SageProductRegistryError, match="already registered"):
            _register_product("intacct", strategies)
