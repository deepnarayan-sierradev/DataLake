"""
PHI onboarding gate (DL-PORT-08) and the subprocessor register (DL-PORT-07).

The platform **refuses** to onboard a tenant, brand, or entity flagged as PHI-bearing until a
BAA is executed and the environment is confirmed HIPAA-capable. This is not a theoretical
clause: Executive Home Care (WellSky) and Assisted Living Locators (SeniorPlace) are home-care
and senior-placement businesses, so the gate must land before those connectors onboard.

Security (OWASP A05): the gate fails closed — an *unclassified* source is treated as
potentially PHI-bearing until classified, rather than assumed safe.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Final

import boto3

from contracts.platform_metrics import PlatformMetric
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

_TABLE_NAME: Final[str] = "EdlSourceOnboardingRegistry"
_SUBPROCESSOR_TABLE_NAME: Final[str] = "EdlSubprocessorRegister"


class PhiClassification(StrEnum):
    """Whether a source carries protected health information."""

    UNCLASSIFIED = "unclassified"
    NOT_PHI = "not_phi"
    PHI_BEARING = "phi_bearing"


class PhiGateBlockedError(Exception):
    """Raised when onboarding is refused because the PHI preconditions are unmet."""


# Sources known to be PHI-bearing from the customer's own source list.
KNOWN_PHI_SOURCES: Final[frozenset[str]] = frozenset({"wellsky", "seniorplace"})


@dataclass(frozen=True)
class PhiOnboardingState:
    """The gate's inputs for one source."""

    source_id: str
    classification: PhiClassification = PhiClassification.UNCLASSIFIED
    baa_executed: bool = False
    baa_executed_at: date | None = None
    baa_counterparty: str = ""
    environment_hipaa_capable: bool = False

    @property
    def is_phi_bearing(self) -> bool:
        """Unclassified counts as PHI-bearing — fail closed, not open."""
        return self.classification is not PhiClassification.NOT_PHI

    @property
    def preconditions_met(self) -> bool:
        if not self.is_phi_bearing:
            return True
        return bool(
            self.classification is PhiClassification.PHI_BEARING
            and self.baa_executed
            and self.baa_executed_at is not None
            and self.baa_counterparty
            and self.environment_hipaa_capable
        )


@dataclass(frozen=True)
class PhiGateVerdict:
    """The gate's decision and the specific reason it blocked."""

    permitted: bool
    source_id: str
    reasons: tuple[str, ...] = ()


def evaluate_phi_gate(state: PhiOnboardingState) -> PhiGateVerdict:
    """Decide whether a source may onboard."""
    if not state.is_phi_bearing:
        return PhiGateVerdict(permitted=True, source_id=state.source_id)
    reasons: list[str] = []
    if state.classification is PhiClassification.UNCLASSIFIED:
        reasons.append(
            "source is unclassified; an unclassified source is treated as potentially "
            "PHI-bearing until classified (OWASP A05 — fail closed)"
        )
    if not state.baa_executed or state.baa_executed_at is None:
        reasons.append("no executed BAA is recorded")
    if state.baa_executed and not state.baa_counterparty:
        reasons.append("the recorded BAA names no counterparty")
    if not state.environment_hipaa_capable:
        reasons.append("the target environment is not confirmed HIPAA-capable")
    if reasons:
        return PhiGateVerdict(permitted=False, source_id=state.source_id, reasons=tuple(reasons))
    return PhiGateVerdict(permitted=True, source_id=state.source_id)


def enforce_phi_gate(state: PhiOnboardingState) -> PhiGateVerdict:
    """Raise rather than return when onboarding must be refused."""
    verdict = evaluate_phi_gate(state)
    if not verdict.permitted:
        record_platform_metric(PlatformMetric.PHI_GATE_BLOCKS, 1.0, SourceId=state.source_id)
        raise PhiGateBlockedError(
            f"Onboarding source {state.source_id!r} is refused: {'; '.join(verdict.reasons)}."
        )
    return verdict


class PhiOnboardingGate:
    """Reads and writes the PHI gate attributes on the existing onboarding registry."""

    def __init__(self, environment: str, region_name: str, hipaa_capable: bool = False) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        self._environment = environment
        self._hipaa_capable = hipaa_capable
        table_name = os.environ.get("SOURCE_ONBOARDING_TABLE") or _TABLE_NAME
        self._table = boto3.resource("dynamodb", region_name=region_name).Table(table_name)

    def classify(
        self,
        source_id: str,
        classification: PhiClassification,
        *,
        classified_by: str,
    ) -> None:
        """Record a source's PHI classification; the actor is retained for the audit trail."""
        if not classified_by:
            raise ValueError("A PHI classification must name the person who made it.")
        self._table.update_item(
            Key={"source_id": source_id},
            UpdateExpression=(
                "SET phi_classification = :c, phi_classified_by = :by, phi_classified_at = :ts"
            ),
            ExpressionAttributeValues={
                ":c": classification.value,
                ":by": classified_by,
                ":ts": datetime.now(UTC).isoformat(),
            },
        )
        _logger.info(
            "phi_classification_recorded",
            source_id=source_id,
            classification=classification.value,
            classified_by=classified_by,
        )

    def record_baa(
        self, source_id: str, *, counterparty: str, executed_on: date, recorded_by: str
    ) -> None:
        if not counterparty or not recorded_by:
            raise ValueError("A recorded BAA must name both its counterparty and the recorder.")
        self._table.update_item(
            Key={"source_id": source_id},
            UpdateExpression=(
                "SET baa_executed = :executed, baa_executed_at = :on, "
                "baa_counterparty = :party, baa_recorded_by = :by"
            ),
            ExpressionAttributeValues={
                ":executed": True,
                ":on": executed_on.isoformat(),
                ":party": counterparty,
                ":by": recorded_by,
            },
        )
        _logger.warning(
            "baa_recorded", source_id=source_id, counterparty=counterparty, recorded_by=recorded_by
        )

    def state_for(self, source_id: str) -> PhiOnboardingState:
        response = self._table.get_item(Key={"source_id": source_id}, ConsistentRead=True)
        item = response.get("Item") or {}
        raw_classification = str(item.get("phi_classification", ""))
        if raw_classification:
            classification = PhiClassification(raw_classification)
        elif source_id in KNOWN_PHI_SOURCES:
            # The adapter already declares these PHI-bearing; a missing registry row must not
            # downgrade that to unknown.
            classification = PhiClassification.PHI_BEARING
        else:
            classification = PhiClassification.UNCLASSIFIED
        executed_at = item.get("baa_executed_at")
        return PhiOnboardingState(
            source_id=source_id,
            classification=classification,
            baa_executed=bool(item.get("baa_executed", False)),
            baa_executed_at=date.fromisoformat(str(executed_at)) if executed_at else None,
            baa_counterparty=str(item.get("baa_counterparty", "")),
            environment_hipaa_capable=self._hipaa_capable,
        )

    def guard_onboarding(self, source_id: str) -> PhiGateVerdict:
        """The hard gate; called before a source's first extraction is scheduled."""
        return enforce_phi_gate(self.state_for(source_id))


# ---------------------------------------------------------------------------
# Subprocessor register (DL-PORT-07) and processing-purpose controls (DL-PORT-06)
# ---------------------------------------------------------------------------


class SubprocessorCategory(StrEnum):
    """What a subprocessor does with customer data."""

    INFRASTRUCTURE = "infrastructure"
    ANALYTICS_ENGINE = "analytics_engine"
    LLM_PROVIDER = "llm_provider"
    MONITORING = "monitoring"
    SOURCE_SYSTEM = "source_system"


@dataclass(frozen=True)
class Subprocessor:
    """One third party processing customer data."""

    name: str
    category: SubprocessorCategory
    purpose: str
    data_classes: tuple[str, ...]
    region: str = "us-east-1"
    in_customer_account: bool = True

    def __post_init__(self) -> None:
        if not self.purpose:
            raise ValueError(
                f"subprocessor {self.name!r} must state its processing purpose (DL-PORT-06)."
            )


# The AWS services actually in use. The LLM provider is deliberately absent: DL-04 is deferred
# and no concrete adapter exists, so listing one would misrepresent the register.
PLATFORM_SUBPROCESSORS: Final[tuple[Subprocessor, ...]] = (
    Subprocessor(
        "AWS S3",
        SubprocessorCategory.INFRASTRUCTURE,
        "Storage of raw, curated, analytics, governance, and export data.",
        ("raw", "curated", "golden", "analytics", "exports"),
    ),
    Subprocessor(
        "AWS DynamoDB",
        SubprocessorCategory.INFRASTRUCTURE,
        "Configuration, watermarks, audit log, and operational metadata.",
        ("configuration", "metadata", "audit"),
    ),
    Subprocessor(
        "AWS Lambda",
        SubprocessorCategory.INFRASTRUCTURE,
        "Execution of extraction, transformation, resolution, and publication stages.",
        ("raw", "curated", "golden", "analytics"),
    ),
    Subprocessor(
        "AWS Step Functions",
        SubprocessorCategory.INFRASTRUCTURE,
        "Pipeline orchestration; carries identifiers, not record payloads.",
        ("metadata",),
    ),
    Subprocessor(
        "AWS Secrets Manager",
        SubprocessorCategory.INFRASTRUCTURE,
        "Storage of source-system credentials.",
        ("credentials",),
    ),
    Subprocessor(
        "AWS Glue and Athena",
        SubprocessorCategory.ANALYTICS_ENGINE,
        "Catalog registration and SQL query over curated and analytics data.",
        ("curated", "analytics"),
    ),
    Subprocessor(
        "AWS RDS and Redshift",
        SubprocessorCategory.ANALYTICS_ENGINE,
        "Serving store for BI tool access.",
        ("analytics",),
    ),
    Subprocessor(
        "AWS CloudWatch and X-Ray",
        SubprocessorCategory.MONITORING,
        "Structured logs, metrics, and traces. No record values and no PII are logged.",
        ("metadata",),
    ),
    Subprocessor(
        "AWS KMS",
        SubprocessorCategory.INFRASTRUCTURE,
        "Encryption keys per data class.",
        ("keys",),
    ),
)


class SubprocessorRegister:
    """The maintained list available to the customer on request, with change notification."""

    def __init__(self, environment: str, region_name: str) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        self._environment = environment
        table_name = os.environ.get("SUBPROCESSOR_TABLE") or _SUBPROCESSOR_TABLE_NAME
        self._table = boto3.resource("dynamodb", region_name=region_name).Table(table_name)

    def publish(self, subprocessors: tuple[Subprocessor, ...] = PLATFORM_SUBPROCESSORS) -> int:
        """Write the current register; each row records when it was last confirmed."""
        now = datetime.now(UTC).isoformat()
        with self._table.batch_writer() as batch:
            for subprocessor in subprocessors:
                batch.put_item(
                    Item={
                        "register_scope": self._environment,
                        "subprocessor_name": subprocessor.name,
                        "category": subprocessor.category.value,
                        "purpose": subprocessor.purpose,
                        "data_classes": list(subprocessor.data_classes),
                        "region": subprocessor.region,
                        "in_customer_account": subprocessor.in_customer_account,
                        "confirmed_at": now,
                    }
                )
        return len(subprocessors)

    def list_register(self) -> list[dict[str, Any]]:
        response = self._table.query(
            KeyConditionExpression="register_scope = :scope",
            ExpressionAttributeValues={":scope": self._environment},
        )
        return [dict(item) for item in response.get("Items", [])]

    def render_markdown(
        self, subprocessors: tuple[Subprocessor, ...] = PLATFORM_SUBPROCESSORS
    ) -> str:
        lines = [
            "# Subprocessor register",
            "",
            f"**Environment:** {self._environment}  ",
            f"**Confirmed:** {datetime.now(UTC).date().isoformat()}",
            "",
            "| Subprocessor | Category | Purpose | Data classes | Region | In customer account |",
            "|---|---|---|---|---|---|",
        ]
        for subprocessor in subprocessors:
            lines.append(
                f"| {subprocessor.name} | {subprocessor.category.value} | {subprocessor.purpose} "
                f"| {', '.join(subprocessor.data_classes)} | {subprocessor.region} "
                f"| {'yes' if subprocessor.in_customer_account else 'no'} |"
            )
        lines.append("")
        return "\n".join(lines)


@dataclass(frozen=True)
class ProcessingPurposeTag:
    """Purpose-tagged role, demonstrating processing only for service delivery (DL-PORT-06)."""

    role_name: str
    purpose: str
    permitted_data_classes: tuple[str, ...] = field(default_factory=tuple)

    def permits(self, data_class: str) -> bool:
        return data_class in self.permitted_data_classes


PLATFORM_PURPOSE_TAGS: Final[tuple[ProcessingPurposeTag, ...]] = (
    ProcessingPurposeTag(
        "EdlExtractionRuntimeRole",
        "service_delivery:ingestion",
        ("raw", "credentials", "metadata"),
    ),
    ProcessingPurposeTag(
        "EdlTransformationRuntimeRole",
        "service_delivery:normalisation",
        ("raw", "curated", "metadata"),
    ),
    ProcessingPurposeTag(
        "EdlEntityResolutionRuntimeRole",
        "service_delivery:resolution",
        ("curated", "golden", "metadata"),
    ),
    ProcessingPurposeTag(
        "EdlAnalyticsPublisherRuntimeRole",
        "service_delivery:publication",
        ("golden", "analytics", "metadata"),
    ),
    ProcessingPurposeTag(
        "EdlExportRuntimeRole",
        "service_delivery:portability",
        ("raw", "curated", "golden", "analytics", "exports"),
    ),
)
