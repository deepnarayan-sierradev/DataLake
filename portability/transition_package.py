"""
Transition package and reproduction test (DL-PORT-02, DL-PORT-09, DL-PORT-10).

One command produces the complete exit bundle: datasets in the requested format, schema
documentation, data-dictionary artefacts, semantic-model and KPI definitions with calculation
methodology, field-mapping and transformation documentation, entity-resolution and
survivorship rules, relationship rules, workflow definitions, and an inventory of source-system
integrations.

The artefacts are the *same* ones `DL-DQ-06` and `DL-SEM-05` already generate — not
exit-specific documents, which would drift from the configuration that actually runs.

DL-PORT-10 is validated by an independent reproduction test on one entity, not by assertion:
`verify_reproducibility` checks that the bundle carries everything a successor provider needs
to recompute one entity's transformations.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from contracts.identifier_policy import validate_tenant_code
from observability.structured_logger import get_platform_logger
from portability.export_service import ExportFormat

_logger = get_platform_logger(__name__)


class PackageComponent(StrEnum):
    """Every component §24.5 requires the bundle to contain."""

    DATASETS = "datasets"
    SCHEMA_DOCUMENTATION = "schema_documentation"
    DATA_DICTIONARY = "data_dictionary"
    SEMANTIC_MODEL = "semantic_model"
    KPI_DEFINITIONS = "kpi_definitions"
    CALCULATION_METHODOLOGY = "calculation_methodology"
    FIELD_MAPPINGS = "field_mappings"
    ENTITY_RESOLUTION_RULES = "entity_resolution_rules"
    SURVIVORSHIP_RULES = "survivorship_rules"
    RELATIONSHIP_RULES = "relationship_rules"
    WORKFLOW_DEFINITIONS = "workflow_definitions"
    SOURCE_INTEGRATION_INVENTORY = "source_integration_inventory"
    INFRASTRUCTURE_HANDOVER = "infrastructure_handover"


REQUIRED_COMPONENTS: Final[frozenset[PackageComponent]] = frozenset(PackageComponent)

# Components a successor provider needs to reproduce one entity's transformations end to end.
REPRODUCTION_CRITICAL_COMPONENTS: Final[frozenset[PackageComponent]] = frozenset(
    {
        PackageComponent.DATASETS,
        PackageComponent.SCHEMA_DOCUMENTATION,
        PackageComponent.DATA_DICTIONARY,
        PackageComponent.FIELD_MAPPINGS,
        PackageComponent.ENTITY_RESOLUTION_RULES,
        PackageComponent.SURVIVORSHIP_RULES,
        PackageComponent.CALCULATION_METHODOLOGY,
    }
)


class IncompletePackageError(Exception):
    """Raised when a bundle is assembled without every required component."""


class ReproductionTestFailedError(Exception):
    """Raised when the bundle cannot reproduce one entity's transformations."""


@dataclass
class PackagedArtefact:
    """One file in the bundle."""

    component: PackageComponent
    relative_path: str
    content_bytes: int
    content_hash: str
    entity_id: str | None = None


@dataclass
class TransitionPackage:
    """The assembled exit bundle."""

    tenant_code: str
    export_format: ExportFormat
    artefacts: list[PackagedArtefact] = field(default_factory=list)
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def __post_init__(self) -> None:
        validate_tenant_code(self.tenant_code)

    @property
    def components_present(self) -> frozenset[PackageComponent]:
        return frozenset(a.component for a in self.artefacts)

    @property
    def missing_components(self) -> frozenset[PackageComponent]:
        return REQUIRED_COMPONENTS - self.components_present

    @property
    def total_bytes(self) -> int:
        return sum(a.content_bytes for a in self.artefacts)

    def entities_covered(self) -> frozenset[str]:
        return frozenset(a.entity_id for a in self.artefacts if a.entity_id)

    def add(self, artefact: PackagedArtefact) -> None:
        self.artefacts.append(artefact)

    def render_manifest(self) -> str:
        """The manifest a successor provider reads first."""
        lines = [
            f"# Transition package — {self.tenant_code}",
            "",
            f"**Generated:** {self.generated_at}  ",
            f"**Dataset format:** {self.export_format.value}  ",
            f"**Artefacts:** {len(self.artefacts)} ({self.total_bytes} bytes)",
            "",
            "Data is in open Parquet under tenant-prefixed keys in the customer's own AWS "
            "account. Nothing in this bundle depends on the vendor's platform to read.",
            "",
            "| Component | Path | Entity | Bytes | SHA-256 |",
            "|---|---|---|---|---|",
        ]
        for artefact in sorted(self.artefacts, key=lambda a: (a.component.value, a.relative_path)):
            lines.append(
                f"| {artefact.component.value} | `{artefact.relative_path}` | "
                f"{artefact.entity_id or '—'} | {artefact.content_bytes} | "
                f"`{artefact.content_hash[:16]}…` |"
            )
        missing = self.missing_components
        if missing:
            lines.extend(
                [
                    "",
                    "## Missing components",
                    "",
                    "This package is **incomplete**. The following are absent:",
                    "",
                ]
            )
            lines.extend(f"- {name}" for name in sorted(c.value for c in missing))
        lines.append("")
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps(
            {
                "tenant_code": self.tenant_code,
                "generated_at": self.generated_at,
                "export_format": self.export_format.value,
                "artefacts": [
                    {
                        "component": a.component.value,
                        "path": a.relative_path,
                        "entity_id": a.entity_id,
                        "bytes": a.content_bytes,
                        "sha256": a.content_hash,
                    }
                    for a in self.artefacts
                ],
                "missing_components": sorted(c.value for c in self.missing_components),
            },
            indent=2,
        )


def require_complete_package(package: TransitionPackage) -> TransitionPackage:
    """A bundle missing any required component is refused, not shipped with a caveat."""
    missing = package.missing_components
    if missing:
        raise IncompletePackageError(
            f"Transition package for {package.tenant_code!r} is missing "
            f"{sorted(c.value for c in missing)}. An exit capability improvised under notice is "
            "not a capability — assemble the full bundle."
        )
    return package


@dataclass(frozen=True)
class ReproductionResult:
    """Whether one entity can be reproduced from the bundle alone."""

    entity_id: str
    reproducible: bool
    missing_components: tuple[str, ...] = ()
    detail: str = ""


def verify_reproducibility(package: TransitionPackage, entity_id: str) -> ReproductionResult:
    """
    Check the bundle carries everything needed to recompute one entity's transformations.

    Validated by inspection of the bundle rather than by assertion in a document: DL-PORT-10
    says the test is independent, and this is the machine-checkable half of it.
    """
    present_for_entity = {
        artefact.component
        for artefact in package.artefacts
        if artefact.entity_id in (entity_id, None)
    }
    missing = REPRODUCTION_CRITICAL_COMPONENTS - present_for_entity
    if missing:
        return ReproductionResult(
            entity_id=entity_id,
            reproducible=False,
            missing_components=tuple(sorted(c.value for c in missing)),
            detail=(
                "a successor provider could not recompute this entity's transformations from "
                "the bundle alone"
            ),
        )
    return ReproductionResult(
        entity_id=entity_id,
        reproducible=True,
        detail="every reproduction-critical component is present for this entity",
    )


def enforce_reproducibility(package: TransitionPackage, entity_id: str) -> ReproductionResult:
    """Raise rather than return when the reproduction test fails."""
    result = verify_reproducibility(package, entity_id)
    if not result.reproducible:
        raise ReproductionTestFailedError(
            f"Entity {entity_id!r} is not reproducible from the transition package; missing "
            f"{list(result.missing_components)}."
        )
    return result


# ---------------------------------------------------------------------------
# Infrastructure hand-over (DL-PORT-09)
# ---------------------------------------------------------------------------

# Resources whose lifecycle is protected by `prevent_destroy`; a hand-over must name them
# because the customer cannot delete or move them without first removing the protection.
PREVENT_DESTROY_RESOURCES: Final[tuple[str, ...]] = (
    "EdlEntityExtractionConfig",
    "EdlWatermarkRepository",
    "EdlRunAuditLog",
    "EdlEntityTypeRegistry",
    "EdlSchemaSnapshot",
)


def render_infrastructure_handover(
    tenant_code: str,
    *,
    account_id: str,
    region: str,
    terraform_state_bucket: str,
    terraform_lock_table: str,
) -> str:
    """
    The documented procedure for the customer to assume control of the account and resources.

    Written from the deployed reality — state bucket, lock table, and the `prevent_destroy`
    set — rather than as generic advice, because a hand-over document that omits the lifecycle
    protections leaves the customer unable to complete the transfer.
    """
    validate_tenant_code(tenant_code)
    lines = [
        f"# Infrastructure hand-over — {tenant_code}",
        "",
        f"**AWS account:** {account_id}  ",
        f"**Region:** {region}  ",
        f"**Terraform state:** `s3://{terraform_state_bucket}` with lock table "
        f"`{terraform_lock_table}`",
        "",
        "## 1. Assume control of the account",
        "",
        "1. The customer creates an administrative principal in the account and confirms "
        "console and CLI access independently of any vendor credential.",
        "2. The vendor's access roles are enumerated and removed **after** step 4, not before, "
        "so the state hand-off can be completed.",
        "3. CloudTrail and the security log group are confirmed to be writing under the "
        "customer's own retention configuration.",
        "",
        "## 2. Take over Terraform state",
        "",
        f"1. Confirm read/write access to `s3://{terraform_state_bucket}` and to the "
        f"`{terraform_lock_table}` lock table.",
        "2. Clone the infrastructure repository at the tag matching the deployed release.",
        "3. Run `terraform init` then `terraform plan` per environment and confirm a **zero-diff "
        "plan**. A non-empty plan means the code and the deployed reality disagree; resolve "
        "that before proceeding.",
        "",
        "## 3. Lifecycle-protected resources",
        "",
        "These carry `lifecycle { prevent_destroy = true }`. They cannot be destroyed or "
        "replaced until the protection is removed in code and applied:",
        "",
    ]
    lines.extend(f"- `{resource}`" for resource in PREVENT_DESTROY_RESOURCES)
    lines.extend(
        [
            "",
            "## 4. Rotate every credential",
            "",
            "1. Rotate all source-system credentials in Secrets Manager; the vendor has held "
            "operational access to them.",
            "2. Rotate serving-store reader credentials and reissue per-tenant VPN certificates.",
            "3. Rotate the Cognito app client secret and confirm the control-plane authorizer "
            "still validates.",
            "",
            "## 5. Confirm and close",
            "",
            "1. Run the post-deploy smoke suite and confirm one extraction, one full pipeline "
            "run, and one semantic query succeed under customer-controlled credentials.",
            "2. Remove the vendor's access roles.",
            "3. Record the hand-over date; the deletion obligation runs from it.",
            "",
        ]
    )
    return "\n".join(lines)


def source_integration_inventory(
    declarations: tuple[Any, ...],
) -> str:
    """
    Inventory of source-system integrations, generated from the capability registry.

    Generated rather than written so it cannot omit a source the platform actually extracts.
    """
    lines = [
        "# Source integration inventory",
        "",
        "| Source | Capabilities | Sync strategy | Pagination | Notes |",
        "|---|---|---|---|---|",
    ]
    for declaration in declarations:
        capabilities = ", ".join(sorted(c.value for c in declaration.capabilities))
        lines.append(
            f"| {declaration.display_name} (`{declaration.source_id}`) | {capabilities} | "
            f"{declaration.default_sync_strategy} | "
            f"{declaration.default_pagination_strategy} | {declaration.notes} |"
        )
    lines.append("")
    return "\n".join(lines)
