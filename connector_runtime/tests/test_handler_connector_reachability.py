"""
Registry-completeness gate (G2): every supported source must resolve from the handler alone.

Adapters register themselves at import time, so a source whose module the handler never imports
is unreachable at runtime — `resolve_builder()` raises `KeyError` on first invocation, in
production, with credentials and config already in place.

The reason the existing adapter tests could not catch this is that each one imports the adapter
module it exercises. That is precisely the import the handler was missing. This module therefore
imports **only the handler**, and asserts against a declaration list that does not depend on the
adapters having been imported.
"""

from __future__ import annotations

from typing import Final

import pytest

import connector_runtime.extraction_pipeline_handler  # noqa: F401
from connector_runtime.registry import connector_registry

LEGACY_SOURCE_IDS: Final[frozenset[str]] = frozenset(
    {"salesforce", "netsuite", "mysql-rds", "sage"}
)
SOW_SOURCE_IDS: Final[frozenset[str]] = frozenset(
    {
        "hubspot",
        "maid-central",
        "servman-pro",
        "wellsky",
        "housecall-pro",
        "dialpad",
        "seniorplace",
        "google-ads",
        "google-analytics",
        "meta-ads",
    }
)
SUPPLEMENTARY_SOURCE_IDS: Final[frozenset[str]] = frozenset({"servicebridge", "bepro"})
ALL_SUPPORTED_SOURCE_IDS: Final[frozenset[str]] = (
    LEGACY_SOURCE_IDS | SOW_SOURCE_IDS | SUPPLEMENTARY_SOURCE_IDS
)


class TestEverySupportedSourceResolvesFromTheHandler:
    @pytest.mark.parametrize("source_id", sorted(ALL_SUPPORTED_SOURCE_IDS))
    def test_the_builder_resolves(self, source_id: str) -> None:
        builder = connector_registry.resolve_builder(source_id)
        assert callable(builder)

    @pytest.mark.parametrize("source_id", sorted(ALL_SUPPORTED_SOURCE_IDS))
    def test_the_params_model_resolves(self, source_id: str) -> None:
        assert connector_registry.get_params_model(source_id) is not None

    def test_no_supported_source_is_missing_from_the_registry(self) -> None:
        registered = set(connector_registry.registered_source_ids)
        missing = sorted(ALL_SUPPORTED_SOURCE_IDS - registered)
        assert not missing, (
            f"{len(missing)} supported source(s) cannot be resolved by the extraction handler: "
            f"{missing}. Adapters register at import time, so the handler must import each "
            "adapter module. Registered: " + str(sorted(registered))
        )

    def test_the_ten_sow_sources_are_all_present(self) -> None:
        registered = set(connector_registry.registered_source_ids)
        assert SOW_SOURCE_IDS <= registered, (
            "DL-01 requires ten source systems to be extractable. Missing from the handler's "
            f"import set: {sorted(SOW_SOURCE_IDS - registered)}."
        )

    def test_the_supplementary_sources_are_present(self) -> None:
        registered = set(connector_registry.registered_source_ids)
        assert SUPPLEMENTARY_SOURCE_IDS <= registered, (
            "DL-CONN-18 (ServiceBridge) and DL-CONN-19 (BePro) must be reachable from the "
            f"handler. Missing: {sorted(SUPPLEMENTARY_SOURCE_IDS - registered)}."
        )
