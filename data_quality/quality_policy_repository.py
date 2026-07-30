"""
Quality policy attachment and the promotion gate (DL-DQ-05, DL-DQ-15).

Closes gap register item 9: no entity reaches production without an attached policy, and
that is enforced by a pre-promotion check rather than by convention.

The gate fails closed — an evaluator error blocks promotion rather than passing (OWASP A04).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import boto3
from botocore.exceptions import ClientError

from contracts.identifier_policy import validate_tenant_code
from contracts.platform_metrics import PlatformMetric
from data_quality.exception_repository import ExceptionSeverity
from observability.lambda_runtime import require_env
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    """DynamoDB string-set/list attributes deserialise to a broad union; narrow to strings."""
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)


class PolicyEnforcementMode(StrEnum):
    """What an ERROR-severity finding does to the analytics publish."""

    BLOCK = "block"
    WARN = "warn"


class QualityPolicyNotAttachedError(Exception):
    """Raised when an entity reaches the promotion gate with no attached policy."""


class QualityGateBlockedError(Exception):
    """Raised when ERROR-severity findings block the analytics publish for an entity."""


@dataclass(frozen=True)
class QualityPolicyAttachment:
    """Which policy version governs one entity, and how strictly."""

    tenant_code: str
    entity_id: str
    policy_id: str
    policy_version: str
    enforcement_mode: PolicyEnforcementMode = PolicyEnforcementMode.BLOCK
    required_fields: tuple[str, ...] = ()
    natural_key_fields: tuple[str, ...] = ()
    date_fields: tuple[str, ...] = ()
    minimum_population_rate_pct: float = 95.0
    maximum_duplicate_rate_pct: float = 0.5
    maximum_orphan_rate_pct: float = 1.0
    attached_by: str = ""
    attached_at: str = ""

    def __post_init__(self) -> None:
        validate_tenant_code(self.tenant_code)
        if not self.policy_id:
            raise ValueError(
                f"entity {self.entity_id!r}: policy_id must not be empty — an attachment with "
                "no policy is what gap 9 was."
            )


class QualityPolicyRepository:
    """Version-keyed store of policy attachments per tenant and entity."""

    def __init__(self, environment: str, region_name: str) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        self._environment = environment
        table_name = require_env("QUALITY_POLICY_TABLE")
        self._table = boto3.resource("dynamodb", region_name=region_name).Table(table_name)

    def attach(self, attachment: QualityPolicyAttachment) -> None:
        item: dict[str, Any] = {
            "tenant_code": attachment.tenant_code,
            "entity_id": attachment.entity_id,
            "policy_id": attachment.policy_id,
            "policy_version": attachment.policy_version,
            "enforcement_mode": attachment.enforcement_mode.value,
            "required_fields": list(attachment.required_fields),
            "natural_key_fields": list(attachment.natural_key_fields),
            "date_fields": list(attachment.date_fields),
            "minimum_population_rate_pct": json.dumps(attachment.minimum_population_rate_pct),
            "maximum_duplicate_rate_pct": json.dumps(attachment.maximum_duplicate_rate_pct),
            "maximum_orphan_rate_pct": json.dumps(attachment.maximum_orphan_rate_pct),
            "attached_by": attachment.attached_by,
            "attached_at": attachment.attached_at or datetime.now(UTC).isoformat(),
            "environment": self._environment,
        }
        self._table.put_item(Item=item)
        _logger.info(
            "quality_policy_attached",
            tenant_code=attachment.tenant_code,
            entity_id=attachment.entity_id,
            policy_id=attachment.policy_id,
            policy_version=attachment.policy_version,
        )

    def get(self, tenant_code: str, entity_id: str) -> QualityPolicyAttachment | None:
        tenant_code = validate_tenant_code(tenant_code)
        try:
            response = self._table.get_item(
                Key={"tenant_code": tenant_code, "entity_id": entity_id}, ConsistentRead=True
            )
        except ClientError as exc:
            raise QualityPolicyNotAttachedError(
                f"Quality policy lookup failed for {tenant_code!r}/{entity_id!r}: "
                f"{exc.response['Error']['Code']}. The gate fails closed."
            ) from exc
        item = response.get("Item")
        if not item:
            return None
        return QualityPolicyAttachment(
            tenant_code=tenant_code,
            entity_id=entity_id,
            policy_id=str(item["policy_id"]),
            policy_version=str(item["policy_version"]),
            enforcement_mode=PolicyEnforcementMode(str(item.get("enforcement_mode", "block"))),
            required_fields=_as_str_tuple(item.get("required_fields")),
            natural_key_fields=_as_str_tuple(item.get("natural_key_fields")),
            date_fields=_as_str_tuple(item.get("date_fields")),
            minimum_population_rate_pct=float(
                json.loads(str(item.get("minimum_population_rate_pct", "95.0")))
            ),
            maximum_duplicate_rate_pct=float(
                json.loads(str(item.get("maximum_duplicate_rate_pct", "0.5")))
            ),
            maximum_orphan_rate_pct=float(
                json.loads(str(item.get("maximum_orphan_rate_pct", "1.0")))
            ),
            attached_by=str(item.get("attached_by", "")),
            attached_at=str(item.get("attached_at", "")),
        )

    def list_attachments(self, tenant_code: str) -> list[QualityPolicyAttachment]:
        tenant_code = validate_tenant_code(tenant_code)
        response = self._table.query(
            KeyConditionExpression="tenant_code = :tc",
            ExpressionAttributeValues={":tc": tenant_code},
        )
        attachments: list[QualityPolicyAttachment] = []
        for item in response.get("Items", []):
            loaded = self.get(tenant_code, str(item["entity_id"]))
            if loaded is not None:
                attachments.append(loaded)
        return attachments


def require_quality_policy(
    repository: QualityPolicyRepository, tenant_code: str, entity_id: str
) -> QualityPolicyAttachment:
    """Pre-promotion check: an entity with no attached policy cannot be promoted."""
    attachment = repository.get(tenant_code, entity_id)
    if attachment is None:
        raise QualityPolicyNotAttachedError(
            f"Entity {entity_id!r} of tenant {tenant_code!r} has no attached quality policy. "
            "No entity reaches production without one (DL-DQ-05)."
        )
    return attachment


@dataclass(frozen=True)
class GateVerdict:
    """Whether the analytics publish may proceed, and why not."""

    permitted: bool
    blocking_count: int
    warning_count: int
    reason: str = ""
    dlq_reason_code: str = ""


def evaluate_quality_gate(
    attachment: QualityPolicyAttachment,
    severities: list[ExceptionSeverity],
) -> GateVerdict:
    """
    Decide whether findings block the analytics publish (DL-DQ-15).

    ERROR blocks and routes to the DLQ with a distinguishable reason; WARN publishes and
    alerts. Configurable per entity, defaulting to block.
    """
    blocking = sum(1 for severity in severities if severity is ExceptionSeverity.ERROR)
    warnings = sum(1 for severity in severities if severity is ExceptionSeverity.WARN)
    if blocking and attachment.enforcement_mode is PolicyEnforcementMode.BLOCK:
        record_platform_metric(
            PlatformMetric.QUALITY_GATE_BLOCKS, blocking, EntityId=attachment.entity_id
        )
        return GateVerdict(
            permitted=False,
            blocking_count=blocking,
            warning_count=warnings,
            reason=(
                f"{blocking} ERROR-severity quality violation(s) for entity "
                f"{attachment.entity_id!r} under policy {attachment.policy_id!r} "
                f"v{attachment.policy_version}."
            ),
            dlq_reason_code="quality_gate_blocked",
        )
    return GateVerdict(permitted=True, blocking_count=blocking, warning_count=warnings)


def enforce_quality_gate(
    attachment: QualityPolicyAttachment, severities: list[ExceptionSeverity]
) -> GateVerdict:
    """Raise rather than return when the gate blocks."""
    verdict = evaluate_quality_gate(attachment, severities)
    if not verdict.permitted:
        raise QualityGateBlockedError(verdict.reason)
    return verdict
