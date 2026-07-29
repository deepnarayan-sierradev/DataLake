"""
G1's entry points must come from Terraform, not from a constant (F14).

`EXTRA_ENTRY_POINTS` used to hardcode the four platform Lambdas directly beneath a comment
saying "Terraform is the source of truth for the Lambda list". Because the constant was unioned
into the seed set, deleting the `platform_lambdas` module would have left G1 green while four
handlers became undeployed — the gate could not fail for the thing it exists to catch. The
constant is gone; these assertions are what keeps the Terraform parse honest in its place.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_module_reachability import (
    EXTRA_ENTRY_POINTS,
    _terraform_handlers,
)

_PLATFORM_LAMBDAS = {
    "connector_runtime.webhook_receiver_handler",
    "connector_runtime.writeback_handler",
    "workflow_automation.workflow_runner_handler",
    "portability.portability_handler",
}


class TestEntryPointsComeFromTerraform:
    def test_the_hardcoded_constant_stays_empty(self) -> None:
        # Re-adding an entry here re-creates the blind spot: the gate would stop depending on
        # Terraform for that handler and could no longer notice its removal.
        assert EXTRA_ENTRY_POINTS == ()

    def test_terraform_declares_the_platform_lambdas(self) -> None:
        handlers = _terraform_handlers()
        missing = sorted(_PLATFORM_LAMBDAS - handlers)
        assert not missing, (
            f"{len(missing)} platform Lambda handler(s) are no longer declared in Terraform: "
            f"{missing}. Either the module was removed (in which case those modules are now "
            "undeployed and G1 should be told) or the handler attribute changed shape and the "
            "parser needs updating."
        )

    def test_the_parse_is_not_silently_empty(self) -> None:
        # A regex that stops matching would make every module unreachable *and* make the gate
        # report success for an empty graph, depending on how the sweep is seeded.
        assert len(_terraform_handlers()) >= 8
