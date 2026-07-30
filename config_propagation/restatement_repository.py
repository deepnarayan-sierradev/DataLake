"""
`datalake-config-restatements-dev` — restatement events for definition changes (DL-CFG-13).

A semantic-model change is apply-forward but read-time, so it silently restates every
historical figure unless announced. This record is the source of the explanation board
and investor reporting need when a number changes between two readings.

Security (OWASP A09): the record is itself the audit evidence for a changed historical
figure, so it is append-only and names the actor.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import boto3

from config_propagation.capability import ConfigCapability
from contracts.identifier_policy import validate_tenant_code
from contracts.platform_metrics import PlatformMetric
from observability.lambda_runtime import require_env
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger
from persistence.dynamodb_paging import iter_items

_logger = get_platform_logger(__name__)


@dataclass(frozen=True)
class RestatementEvent:
    """The metrics affected, the periods affected, and the before/after definition."""

    tenant_code: str
    capability: ConfigCapability
    metrics_affected: tuple[str, ...]
    periods_affected: tuple[str, ...]
    previous_version: str
    new_version: str
    actor: str
    correlation_id: str
    definition_before: str = ""
    definition_after: str = ""
    event_id: str = field(default_factory=lambda: f"rst-{uuid.uuid4().hex[:12]}")

    def __post_init__(self) -> None:
        validate_tenant_code(self.tenant_code)
        if not self.metrics_affected:
            raise ValueError(
                "A restatement event must name at least one affected metric — an unnamed "
                "restatement cannot explain a changed figure."
            )
        if not self.actor:
            raise ValueError("A restatement event must name the actor who caused it.")

    @property
    def sort_key(self) -> str:
        return f"{self.capability.value}#{self.event_id}"


class RestatementRepository:
    """Appends and reads restatement events."""

    def __init__(self, environment: str, region_name: str) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        self._environment = environment
        table_name = require_env("CONFIG_RESTATEMENT_TABLE")
        self._table = boto3.resource("dynamodb", region_name=region_name).Table(table_name)

    def emit(self, event: RestatementEvent) -> str:
        item: dict[str, Any] = {
            "tenant_code": event.tenant_code,
            "restatement_key": event.sort_key,
            "event_id": event.event_id,
            "capability": event.capability.value,
            "metrics_affected": list(event.metrics_affected),
            "periods_affected": list(event.periods_affected),
            "previous_version": event.previous_version,
            "new_version": event.new_version,
            "definition_before": event.definition_before,
            "definition_after": event.definition_after,
            "actor": event.actor,
            "correlation_id": event.correlation_id,
            "emitted_at": datetime.now(UTC).isoformat(),
            "environment": self._environment,
        }
        self._table.put_item(Item=item)
        record_platform_metric(
            PlatformMetric.RESTATEMENT_EVENTS_EMITTED, 1.0, Capability=event.capability.value
        )
        _logger.info(
            "config_restatement_emitted",
            tenant_code=event.tenant_code,
            capability=event.capability.value,
            event_id=event.event_id,
            metrics_affected=list(event.metrics_affected),
            previous_version=event.previous_version,
            new_version=event.new_version,
        )
        return event.event_id

    def list_restatements(
        self, tenant_code: str, capability: ConfigCapability | None = None
    ) -> list[dict[str, Any]]:
        tenant_code = validate_tenant_code(tenant_code)
        query_kwargs: dict[str, Any] = {
            "KeyConditionExpression": "tenant_code = :tc",
            "ExpressionAttributeValues": {":tc": tenant_code},
        }
        if capability is not None:
            query_kwargs["KeyConditionExpression"] = (
                "tenant_code = :tc AND begins_with(restatement_key, :cap)"
            )
            query_kwargs["ExpressionAttributeValues"][":cap"] = f"{capability.value}#"
        events = list(iter_items(self._table, **query_kwargs))
        return sorted(events, key=lambda e: str(e.get("emitted_at", "")), reverse=True)
