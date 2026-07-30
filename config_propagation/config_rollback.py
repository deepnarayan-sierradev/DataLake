"""
Rollback of the published configuration contract (DL-CFG-09) and in-flight run
coordination (DL-CFG-07).

Rollback repoints `latest` to a prior retained version as one audited operation under the
same maker-checker treatment as a publish. A publish landing while a run is in flight is
permitted but recorded, and where a capability cannot tolerate a mid-flight change the
publish is queued to the next run boundary rather than blocked — blocking on run state
would make the console unusable at scale.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final, Protocol

import boto3

from config_propagation.capability import ConfigCapability
from contracts.identifier_policy import validate_tenant_code
from contracts.platform_metrics import PlatformMetric
from observability.lambda_runtime import require_env
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)


MID_FLIGHT_SENSITIVE_CAPABILITIES: Final[frozenset[ConfigCapability]] = frozenset(
    {
        ConfigCapability.ENTITY_RESOLUTION,
        ConfigCapability.SURVIVORSHIP,
        ConfigCapability.FIELD_MAPPING,
        ConfigCapability.SCOPE_MODEL,
    }
)


class PublishDisposition(StrEnum):
    """What happened to a publish attempted against a capability with a run in flight."""

    APPLIED_IMMEDIATELY = "applied_immediately"
    APPLIED_AND_RUN_ANNOTATED = "applied_and_run_annotated"
    QUEUED_FOR_NEXT_RUN_BOUNDARY = "queued_for_next_run_boundary"


class MakerCheckerViolationError(Exception):
    """Raised when a rollback's approver is absent or is the same actor as the maker."""


class PointerStore(Protocol):
    """The minimal `latest`-pointer surface a rollback needs."""

    def read_pointer(self, tenant_code: str, capability: ConfigCapability, key: str) -> str: ...

    def write_pointer(
        self, tenant_code: str, capability: ConfigCapability, key: str, version: str
    ) -> None: ...

    def version_exists(
        self, tenant_code: str, capability: ConfigCapability, key: str, version: str
    ) -> bool: ...


@dataclass(frozen=True)
class RollbackRequest:
    """A maker-checker rollback of one capability's published pointer."""

    tenant_code: str
    capability: ConfigCapability
    entity_key: str
    target_version: str
    requested_by: str
    approved_by: str
    correlation_id: str

    def __post_init__(self) -> None:
        validate_tenant_code(self.tenant_code)
        if not self.approved_by:
            raise MakerCheckerViolationError(
                f"Rollback of {self.capability.value!r} requires an approver distinct from the "
                "requester — a single actor must not silently revert a governed definition."
            )
        if self.approved_by == self.requested_by:
            raise MakerCheckerViolationError(
                f"Rollback of {self.capability.value!r} was approved by its own requester "
                f"({self.requested_by!r}). Maker and checker must differ (OWASP A04)."
            )


@dataclass(frozen=True)
class RollbackResult:
    """Outcome of a rollback, with the version it replaced."""

    rollback_id: str
    previous_version: str
    target_version: str


class ConfigGovernanceService:
    """Audited rollback and in-flight publish coordination."""

    def __init__(
        self,
        environment: str,
        region_name: str,
        pointer_store: PointerStore,
    ) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        self._environment = environment
        self._pointers = pointer_store
        table_name = require_env("CONFIG_GOVERNANCE_TABLE")
        self._table = boto3.resource("dynamodb", region_name=region_name).Table(table_name)

    def rollback(self, request: RollbackRequest) -> RollbackResult:
        if not self._pointers.version_exists(
            request.tenant_code, request.capability, request.entity_key, request.target_version
        ):
            raise ValueError(
                f"Rollback target version {request.target_version!r} does not exist for "
                f"capability {request.capability.value!r} / {request.entity_key!r}. Prior "
                "versions are retained; a missing one means the target is wrong."
            )
        previous = self._pointers.read_pointer(
            request.tenant_code, request.capability, request.entity_key
        )
        self._pointers.write_pointer(
            request.tenant_code,
            request.capability,
            request.entity_key,
            request.target_version,
        )
        rollback_id = f"rbk-{uuid.uuid4().hex[:12]}"
        self._audit(
            tenant_code=request.tenant_code,
            record_key=f"rollback#{request.capability.value}#{rollback_id}",
            payload={
                "rollback_id": rollback_id,
                "capability": request.capability.value,
                "entity_key": request.entity_key,
                "previous_version": previous,
                "target_version": request.target_version,
                "requested_by": request.requested_by,
                "approved_by": request.approved_by,
                "correlation_id": request.correlation_id,
            },
        )
        record_platform_metric(
            PlatformMetric.CONFIG_ROLLBACKS, 1.0, Capability=request.capability.value
        )
        record_platform_metric(PlatformMetric.ADMIN_ACTIONS, 1.0, Capability="config_rollback")
        _logger.warning(
            "config_rollback_applied",
            tenant_code=request.tenant_code,
            capability=request.capability.value,
            entity_key=request.entity_key,
            previous_version=previous,
            target_version=request.target_version,
            requested_by=request.requested_by,
            approved_by=request.approved_by,
        )
        return RollbackResult(
            rollback_id=rollback_id,
            previous_version=previous,
            target_version=request.target_version,
        )

    def coordinate_publish(
        self,
        tenant_code: str,
        capability: ConfigCapability,
        entity_key: str,
        version: str,
        *,
        in_flight_run_ids: tuple[str, ...] = (),
        actor: str = "",
        correlation_id: str = "",
    ) -> PublishDisposition:
        """
        Decide and record how a publish interacts with runs already executing.

        Never blocks: a tolerant capability applies immediately and annotates the affected
        runs; a mid-flight-sensitive one is queued to the next run boundary.
        """
        tenant_code = validate_tenant_code(tenant_code)
        if not in_flight_run_ids:
            disposition = PublishDisposition.APPLIED_IMMEDIATELY
        elif capability in MID_FLIGHT_SENSITIVE_CAPABILITIES:
            disposition = PublishDisposition.QUEUED_FOR_NEXT_RUN_BOUNDARY
        else:
            disposition = PublishDisposition.APPLIED_AND_RUN_ANNOTATED

        self._audit(
            tenant_code=tenant_code,
            record_key=f"publish#{capability.value}#{entity_key}#{version}",
            payload={
                "capability": capability.value,
                "entity_key": entity_key,
                "version": version,
                "disposition": disposition.value,
                "in_flight_run_ids": list(in_flight_run_ids),
                "actor": actor,
                "correlation_id": correlation_id,
            },
        )
        _logger.info(
            "config_publish_coordinated",
            tenant_code=tenant_code,
            capability=capability.value,
            entity_key=entity_key,
            version=version,
            disposition=disposition.value,
            in_flight_runs=len(in_flight_run_ids),
        )
        return disposition

    def queued_publishes(
        self, tenant_code: str, capability: ConfigCapability
    ) -> list[dict[str, Any]]:
        """Publishes deferred to the next run boundary, oldest first."""
        tenant_code = validate_tenant_code(tenant_code)
        response = self._table.query(
            KeyConditionExpression="tenant_code = :tc AND begins_with(record_key, :prefix)",
            ExpressionAttributeValues={
                ":tc": tenant_code,
                ":prefix": f"publish#{capability.value}#",
            },
        )
        queued = [
            dict(item)
            for item in response.get("Items", [])
            if item.get("disposition") == PublishDisposition.QUEUED_FOR_NEXT_RUN_BOUNDARY.value
        ]
        return sorted(queued, key=lambda item: str(item.get("recorded_at", "")))

    def _audit(self, tenant_code: str, record_key: str, payload: dict[str, Any]) -> None:
        self._table.put_item(
            Item={
                "tenant_code": tenant_code,
                "record_key": record_key,
                "recorded_at": datetime.now(UTC).isoformat(),
                "environment": self._environment,
                **payload,
            }
        )
