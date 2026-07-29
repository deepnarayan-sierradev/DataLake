"""
`EdlEffectiveConfig` — the record that turns "published" into "in effect" (DL-CFG-08).

PK `tenant_code`, SK `{capability}#{scope_id}#{entity_key}`. Written once per capability
version transition, on first consumption by a run — not once per run.

Security (OWASP A01, A09): tenant-partitioned at the key level; every transition is an
audit record naming the run that first consumed the version.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any, Final

import boto3
from botocore.exceptions import ClientError

from config_propagation.capability import ConfigCapability
from contracts.identifier_policy import validate_tenant_code
from contracts.platform_metrics import PlatformMetric
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger
from tenancy.scope_contract import IMPLICIT_SCOPE_UNIT_ID

_logger = get_platform_logger(__name__)

_TABLE_NAME: Final[str] = "EdlEffectiveConfig"


def effective_config_sort_key(
    capability: ConfigCapability, entity_key: str, scope_id: str = IMPLICIT_SCOPE_UNIT_ID
) -> str:
    """`{capability}#{scope_id}#{entity_key}` — one row per governed configuration target."""
    return f"{capability.value}#{scope_id}#{entity_key}"


class EffectiveConfigRecord(dict[str, Any]):
    """DynamoDB item view; a plain mapping keeps the read path allocation-free."""


class EffectiveConfigRepository:
    """Records and reads which configuration version is currently in effect."""

    def __init__(self, environment: str, region_name: str) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        self._environment = environment
        table_name = os.environ.get("EFFECTIVE_CONFIG_TABLE") or _TABLE_NAME
        self._table = boto3.resource("dynamodb", region_name=region_name).Table(table_name)

    def record_consumption(
        self,
        tenant_code: str,
        capability: ConfigCapability,
        entity_key: str,
        version: str,
        run_id: str,
        *,
        scope_id: str = IMPLICIT_SCOPE_UNIT_ID,
        config_schema_version: int = 1,
        published_at: str | None = None,
    ) -> bool:
        """
        Record `version` as effective, attributing it to the first run that consumed it.

        Returns True when this call caused a version transition. The conditional write is
        what makes "first consumer" meaningful under concurrent stages.
        """
        tenant_code = validate_tenant_code(tenant_code)
        sort_key = effective_config_sort_key(capability, entity_key, scope_id)
        now = datetime.now(UTC).isoformat()
        item: dict[str, Any] = {
            "tenant_code": tenant_code,
            "capability_key": sort_key,
            "capability": capability.value,
            "scope_id": scope_id,
            "entity_key": entity_key,
            "effective_version": version,
            "first_consuming_run_id": run_id,
            "effective_at": now,
            "config_schema_version": config_schema_version,
            "environment": self._environment,
        }
        if published_at:
            item["published_at"] = published_at
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression=(
                    "attribute_not_exists(effective_version) OR effective_version <> :v"
                ),
                ExpressionAttributeValues={":v": version},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            _logger.warning(
                "effective_config_write_failed",
                tenant_code=tenant_code,
                capability=capability.value,
                entity_key=entity_key,
                error_code=exc.response["Error"]["Code"],
            )
            return False
        record_platform_metric(
            PlatformMetric.EFFECTIVE_VERSION_TRANSITIONS, 1.0, Capability=capability.value
        )
        lag = self.propagation_lag_seconds(tenant_code, capability, entity_key, scope_id)
        if lag is not None:
            record_platform_metric(
                PlatformMetric.CONFIG_PROPAGATION_LAG_SECONDS, lag, Capability=capability.value
            )
        _logger.info(
            "effective_config_version_transitioned",
            tenant_code=tenant_code,
            capability=capability.value,
            entity_key=entity_key,
            effective_version=version,
            first_consuming_run_id=run_id,
        )
        return True

    def get_effective(
        self,
        tenant_code: str,
        capability: ConfigCapability,
        entity_key: str,
        scope_id: str = IMPLICIT_SCOPE_UNIT_ID,
    ) -> dict[str, Any] | None:
        tenant_code = validate_tenant_code(tenant_code)
        response = self._table.get_item(
            Key={
                "tenant_code": tenant_code,
                "capability_key": effective_config_sort_key(capability, entity_key, scope_id),
            }
        )
        item = response.get("Item")
        return dict(item) if item else None

    def list_effective(
        self, tenant_code: str, capability: ConfigCapability | None = None
    ) -> list[dict[str, Any]]:
        tenant_code = validate_tenant_code(tenant_code)
        query_kwargs: dict[str, Any] = {
            "KeyConditionExpression": "tenant_code = :tc",
            "ExpressionAttributeValues": {":tc": tenant_code},
        }
        if capability is not None:
            query_kwargs["KeyConditionExpression"] = (
                "tenant_code = :tc AND begins_with(capability_key, :cap)"
            )
            query_kwargs["ExpressionAttributeValues"][":cap"] = f"{capability.value}#"
        records: list[dict[str, Any]] = []
        while True:
            response = self._table.query(**query_kwargs)
            records.extend(dict(item) for item in response.get("Items", []))
            record_platform_metric(
                PlatformMetric.PUBLISHES_NOT_YET_EFFECTIVE,
                sum(1 for item in response.get("Items", []) if "published_at" not in item),
            )
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key
        return records

    def propagation_lag_seconds(
        self,
        tenant_code: str,
        capability: ConfigCapability,
        entity_key: str,
        scope_id: str = IMPLICIT_SCOPE_UNIT_ID,
    ) -> float | None:
        """
        Observed publish-to-effective lag — the metric the console reads for "is it live yet".

        None when the record carries no `published_at`, which is the case for a version the
        runtime saw before any publish record existed.
        """
        record = self.get_effective(tenant_code, capability, entity_key, scope_id)
        if not record or "published_at" not in record:
            return None
        try:
            published = datetime.fromisoformat(str(record["published_at"]))
            effective = datetime.fromisoformat(str(record["effective_at"]))
        except ValueError:
            return None
        return max(0.0, (effective - published).total_seconds())
