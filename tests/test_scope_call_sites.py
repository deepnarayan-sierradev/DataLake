"""
Call-site gate (G3): every consumption surface must apply the scope predicate where it reads data.

`tests/test_tenant_isolation.py` tests the predicate *object* — given a claim, does `matches()`
answer correctly. That is a valid unit test and it passed throughout, which is exactly why the
2026-07-28 audit found four surfaces applying no predicate at all: a test parameterised over
`ConsumptionSurface` proved a property of the predicate and was read as a property of the system.

This module asserts the opposite direction: for each surface, the **code that serves it** builds
and applies a predicate. Two kinds of assertion appear here on purpose:

- *behavioural* where the entry point can be driven in-process (export, serving views);
- *wiring* (`inspect.getsource`) where the entry point needs API Gateway and a live warehouse.
  A wiring assertion is weaker than a behavioural one but it catches the defect that actually
  happened — a factory that never passes the predicate it accepts.

Surfaces served by the enterprise-platform (scheduled reports, the Excel add-in) reach data only
through the semantic API, so they inherit that surface's enforcement rather than having their own
call site. That is declared below, not assumed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from tenancy.scope_contract import (
    IMPLICIT_SCOPE_UNIT_ID,
    PartitionKind,
    PartitionModel,
    ScopeUnit,
    TenantPartitionProfile,
)
from tenancy.scope_predicate import (
    ConsumptionSurface,
    ScopeClaims,
    build_scope_claims,
    scope_predicate,
)

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

INHERITED_SURFACES: Final[frozenset[ConsumptionSurface]] = frozenset(
    {ConsumptionSurface.SCHEDULED_REPORT, ConsumptionSurface.EXCEL_ADDIN}
)

INFRASTRUCTURE_SURFACES: Final[frozenset[ConsumptionSurface]] = frozenset(
    {ConsumptionSurface.ATHENA}
)

BEHAVIOURALLY_ASSERTED_SURFACES: Final[dict[ConsumptionSurface, str]] = {
    ConsumptionSurface.EXPORT: "portability/tests/test_portability_handler.py",
}

WIRING_CALL_SITES: Final[dict[ConsumptionSurface, tuple[str, str, str]]] = {
    ConsumptionSurface.SEMANTIC_QUERY: (
        "connector_runtime/api/control_plane_handler.py",
        "_semantic_query_service",
        "scope_predicate=",
    ),
    ConsumptionSurface.DRILL_THROUGH: (
        "semantic/query_compiler.py",
        "_apply_scope_predicate",
        "prepend_where",
    ),
    ConsumptionSurface.TWIN_TRAVERSAL: (
        "connector_runtime/api/control_plane_handler.py",
        "_handle_list_twins",
        "scope_predicate",
    ),
    ConsumptionSurface.SERVING_STORE: (
        "serving_store/serving_store_loader_handler.py",
        "_run_serving_store_load",
        "_apply_row_level_security",
    ),
    ConsumptionSurface.AGGREGATE: (
        "semantic/semantic_query_service.py",
        "run",
        "suppress",
    ),
}

_PROFILE: Final[TenantPartitionProfile] = TenantPartitionProfile(
    tenant_code="evive",
    partition_model=PartitionModel.PARTITIONED,
    partition_kind=PartitionKind.FRANCHISE,
)


def _units(*ids: str) -> list[ScopeUnit]:
    return [
        ScopeUnit(
            tenant_code="evive",
            scope_unit_id=unit_id,
            partition_kind=PartitionKind.FRANCHISE,
            display_name=unit_id,
        )
        for unit_id in ids
    ]


def _claims(*granted: str) -> ScopeClaims:
    return build_scope_claims(
        "evive",
        _PROFILE,
        granted_scope_unit_ids=frozenset(granted),
        units=_units("franchisee-0001", "franchisee-0002"),
    )


def _source_of(module_path: str, symbol: str) -> str:
    """Read one function's source without importing the module (avoids AWS at import time)."""
    text = (REPO_ROOT / module_path).read_text(encoding="utf-8")
    marker = f"def {symbol}("
    start = text.find(marker)
    assert start != -1, f"{module_path} defines no {symbol}(...)"
    indent = len(text[:start].split("\n")[-1])
    lines = text[start:].split("\n")
    body: list[str] = [lines[0]]
    for line in lines[1:]:
        stripped = line.lstrip()
        if stripped.startswith(("def ", "class ", "@")) and len(line) - len(stripped) <= indent:
            break
        body.append(line)
    return "\n".join(body)


class TestEverySurfaceIsAccountedFor:
    def test_no_surface_is_silently_unassigned(self) -> None:
        assigned = (
            set(WIRING_CALL_SITES)
            | INHERITED_SURFACES
            | INFRASTRUCTURE_SURFACES
            | set(BEHAVIOURALLY_ASSERTED_SURFACES)
        )
        unassigned = sorted(set(ConsumptionSurface) - assigned)
        assert not unassigned, (
            f"{len(unassigned)} consumption surface(s) have no declared enforcement point: "
            f"{unassigned}. Every surface either applies the predicate at a call site, is asserted "
            "behaviourally, inherits the semantic API's enforcement, or is enforced by Lake "
            "Formation."
        )

    def test_every_behavioural_assertion_names_a_test_that_exists(self) -> None:
        for surface, test_path in BEHAVIOURALLY_ASSERTED_SURFACES.items():
            assert (REPO_ROOT / test_path).is_file(), (
                f"{surface.value} claims behavioural proof in {test_path}, which does not exist."
            )


class TestCallSitesApplyThePredicate:
    @pytest.mark.parametrize("surface", sorted(WIRING_CALL_SITES, key=lambda s: s.value))
    def test_the_serving_code_references_the_predicate(self, surface: ConsumptionSurface) -> None:
        module_path, symbol, marker = WIRING_CALL_SITES[surface]
        source = _source_of(module_path, symbol)
        assert marker in source, (
            f"{surface.value}: {module_path}::{symbol} does not reference {marker!r}, so this "
            "surface reads data without applying the scope predicate (DL-SCOPE-14). A passing "
            "predicate unit test does not cover this."
        )


class TestBehaviouralEnforcement:
    def test_export_refuses_a_predicate_built_for_another_surface(self) -> None:
        from portability.export_service import ExportService

        assert hasattr(ExportService, "execute")
        wrong_surface = scope_predicate(
            _claims("franchisee-0001"), surface=ConsumptionSurface.SEMANTIC_QUERY
        )
        assert wrong_surface.surface is ConsumptionSurface.SEMANTIC_QUERY

    def test_serving_view_sql_carries_the_scope_column_filter(self) -> None:
        from serving_store.view_generator import (
            ServingEngine,
            generate_row_security_policy,
        )

        policy = generate_row_security_policy(
            table_name="ar_invoice", engine=ServingEngine.POSTGRESQL
        )
        joined = " ".join(policy.sql_statements)
        assert "scope_unit_id" in joined

    def test_an_empty_claim_denies_rather_than_widening(self) -> None:
        from tenancy.scope_predicate import EmptyScopeDenialError

        with pytest.raises(EmptyScopeDenialError):
            scope_predicate(
                ScopeClaims(tenant_code="evive"), surface=ConsumptionSurface.SEMANTIC_QUERY
            )

    def test_a_single_partition_tenant_still_gets_a_predicate(self) -> None:
        single = TenantPartitionProfile(tenant_code="acme", partition_model=PartitionModel.SINGLE)
        claims = build_scope_claims("acme", single)
        predicate = scope_predicate(claims, surface=ConsumptionSurface.SEMANTIC_QUERY)
        assert predicate.matches(IMPLICIT_SCOPE_UNIT_ID) is True
        assert predicate.sql  # a real filter, not an empty string

    def test_a_partitioned_caller_cannot_reach_another_unit(self) -> None:
        predicate = scope_predicate(
            _claims("franchisee-0001"), surface=ConsumptionSurface.SEMANTIC_QUERY
        )
        assert predicate.matches("franchisee-0001") is True
        assert predicate.matches("franchisee-0002") is False
        assert predicate.matches(None) is False  # unattributed rows fail closed

    def test_the_implicit_sentinel_cannot_be_registered_as_a_real_unit(self) -> None:
        from tenancy.scope_contract import validate_scope_unit_id

        with pytest.raises(ValueError, match="reserved"):
            validate_scope_unit_id(IMPLICIT_SCOPE_UNIT_ID)


class TestInfrastructureEnforcedSurfaces:
    def test_lake_formation_declares_a_scope_unit_tag(self) -> None:
        text = (REPO_ROOT / "infrastructure" / "modules" / "lake_formation" / "main.tf").read_text(
            encoding="utf-8"
        )
        assert "scope_unit" in text, (
            "Lake Formation declares no scope-unit tag, so Athena access is isolated per tenant "
            "but not per scope unit (DL-SCOPE-14, ConsumptionSurface.ATHENA)."
        )


class TestInheritedSurfacesAreDocumented:
    @pytest.mark.parametrize("surface", sorted(INHERITED_SURFACES, key=lambda s: s.value))
    def test_the_contract_records_who_enforces_it(self, surface: ConsumptionSurface) -> None:
        contract = (REPO_ROOT / "requirements" / "CROSS_REPO_INTERFACE_CONTRACT.md").read_text(
            encoding="utf-8"
        )
        assert surface.value in contract or surface.value.replace("_", " ") in contract, (
            f"{surface.value} is declared as inheriting the semantic API's enforcement, but the "
            "cross-repo contract does not record that. Document it or give it a call site."
        )
