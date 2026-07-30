"""
Method-granularity reachability (G10) — the gap G1 cannot see.

G1 asks "does any production module import this module". That is the right question for the defect
it was built for (eighteen modules with no consumer), but it answers at module granularity, so a
*capability* with no path from any entry point passes it. Two instances survived two audit passes
because of this:

- `ExportService.execute` had no production caller anywhere. `portability_handler` imported the
  module and called `request_export`, so G1 was satisfied — while the format rendering, the
  KMS-encrypted upload, and the row-by-row scope filter were dead, and DL-PORT-01 produced a job id
  and no artefact.
- The semantic compiler's filters, joins, time grains and comparisons were unreachable because the
  request model exposed only entity/metrics/dimensions. `WAIVERS.md` recorded DL-SEM-07 as
  implemented on the strength of the compiler alone, with the advice "add the id rather than the
  code".

So this asserts, for a curated list of methods that *are* a delivered capability, that
production code calls them. The list is explicit rather than derived: a heuristic over every
public method would drown in false positives from registries and dynamic dispatch, and a gate
that cries wolf gets muted. Adding a capability means adding a line here — the same discipline
the metric catalogue applies.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final, NamedTuple

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
EXCLUDED_PARTS: Final[frozenset[str]] = frozenset({"tests", "__pycache__", ".venv", "dist", "pptx"})


class Capability(NamedTuple):
    """One method whose absence of a caller would mean a requirement is undelivered."""

    method: str
    requirement: str
    why_it_matters: str


GUARDED_CAPABILITIES: Final[tuple[Capability, ...]] = (
    Capability(
        "execute",
        "DL-PORT-01",
        "ExportService.execute renders and uploads the artefact; without a caller an export is a "
        "job record and SOW §24.4 is unmet",
    ),
    Capability(
        "drop_tenant_container",
        "DL-PORT-04",
        "a certified deletion must cover the serving store, and the saga refuses to certify "
        "coverage it did not achieve",
    ),
    Capability(
        "page_configs_for_tenant",
        "DL-SEC-03",
        "the bounded listing; without a caller /entities is back to an unbounded drain",
    ),
    Capability(
        "page_twins",
        "DL-SCOPE-13",
        "the bounded twin listing; the draining form is for internal callers only",
    ),
    Capability(
        "unrestricted_predicate",
        "DL-SCOPE-14",
        "the audited stand-in for `None`; without a caller the fail-open it replaced would return",
    ),
)

PENDING_CAPABILITIES: Final[tuple[Capability, ...]] = (
    Capability(
        "tenant_scoped_session",
        "DL-SEC-01",
        "the mechanism exists and is tested, but 47 data-plane call sites still build clients from "
        "ambient credentials — tracked by `make tenant-session-adoption` (G9), and the Terraform "
        "interlock refuses `enforce` until adoption is complete",
    ),
)

SCAFFOLD_INTERNAL: Final[frozenset[str]] = frozenset({"enqueue_stage_failure"})


def _production_modules() -> list[Path]:
    return [
        path
        for path in sorted(REPO_ROOT.rglob("*.py"))
        if not (set(path.relative_to(REPO_ROOT).parts) & EXCLUDED_PARTS)
        and path.relative_to(REPO_ROOT).parts[0] != "scripts"
    ]


def _called_names(path: Path) -> set[str]:
    """Every name this module calls, whether as `x.method(...)` or a bare `method(...)`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    called: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Attribute):
            called.add(function.attr)
        elif isinstance(function, ast.Name):
            called.add(function.id)
    return called


def _callers_of(method: str) -> list[str]:
    """Production modules that call `method`, excluding the module that defines it."""
    callers: list[str] = []
    for path in _production_modules():
        source = path.read_text(encoding="utf-8")
        defines_it = f"def {method}(" in source
        if method in _called_names(path) and not defines_it:
            callers.append(str(path.relative_to(REPO_ROOT)))
    return callers


class TestEveryGuardedCapabilityHasAProductionCaller:
    @pytest.mark.parametrize("capability", GUARDED_CAPABILITIES, ids=lambda c: c.method)
    def test_it_is_called_from_production_code(self, capability: Capability) -> None:
        callers = _callers_of(capability.method)
        assert callers, (
            f"{capability.method}() has no production caller, so {capability.requirement} is not "
            f"delivered: {capability.why_it_matters}. G1 cannot see this — it asks whether the "
            "module is imported, and it is."
        )


class TestPendingCapabilitiesAreStillPending:
    @pytest.mark.parametrize("capability", PENDING_CAPABILITIES, ids=lambda c: c.method)
    def test_the_pending_list_has_not_gone_stale(self, capability: Capability) -> None:
        """
        A pending entry that has since gained a caller must be promoted, not left here.

        This is the stale-waiver rule applied to capabilities: without it, the list would quietly
        become a place where delivered work is recorded as undelivered, which is the mirror image of
        the DL-SEM-07 waiver recording undelivered work as delivered.
        """
        callers = _callers_of(capability.method)
        assert not callers, (
            f"{capability.method}() now has production callers {callers}, so it is no longer "
            f"pending. Move it to GUARDED_CAPABILITIES and update the gate that tracked it."
        )


class TestTheGateItselfCanFail:
    def test_a_method_nothing_calls_is_reported_as_uncalled(self) -> None:
        assert _callers_of("a_method_name_that_does_not_exist_anywhere") == []

    def test_a_method_defined_and_called_only_in_its_own_module_does_not_count(self) -> None:
        assert "portability/export_service.py" not in _callers_of("_apply_scope")


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
