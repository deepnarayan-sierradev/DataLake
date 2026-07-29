"""
`EdlSourceConnection` repository — PK `tenant_code`, SK `connection_id` (DL-SCOPE-03).

Security (OWASP A01): every read and write is tenant-partitioned at the key level, and a
retired connection is never returned as extractable.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any, Final

import boto3
from botocore.exceptions import ClientError
from pydantic import ValidationError

from contracts.identifier_policy import validate_stable_id, validate_tenant_code
from contracts.platform_metrics import PlatformMetric
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger
from persistence.dynamodb_paging import iter_items
from tenancy.source_connection import (
    ConnectionState,
    SourceConnection,
    default_connection_for_source,
    validate_connection_transition,
)

_logger = get_platform_logger(__name__)

_TABLE_NAME: Final[str] = "EdlSourceConnection"


class ConnectionNotFoundError(Exception):
    """Raised when no connection record exists for the tenant/connection pair."""


class ConnectionAlreadyExistsError(Exception):
    """Raised when registering a connection_id that already exists for the tenant."""


class SourceConnectionRepository:
    """Reads and writes source-connection records."""

    def __init__(self, environment: str, region_name: str) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        self._environment = environment
        table_name = os.environ.get("SOURCE_CONNECTION_TABLE") or _TABLE_NAME
        self._table = boto3.resource("dynamodb", region_name=region_name).Table(table_name)

    # ── Reads ─────────────────────────────────────────────────────────────────

    def get_connection(self, tenant_code: str, connection_id: str) -> SourceConnection:
        tenant_code = validate_tenant_code(tenant_code)
        validate_stable_id(connection_id, "connection_id")
        try:
            response = self._table.get_item(
                Key={"tenant_code": tenant_code, "connection_id": connection_id},
                ConsistentRead=True,
            )
        except ClientError as exc:
            raise ConnectionNotFoundError(
                f"DynamoDB error loading connection {connection_id!r} for tenant "
                f"{tenant_code!r}: {exc.response['Error']['Code']}"
            ) from exc
        item = response.get("Item")
        if not item:
            raise ConnectionNotFoundError(
                f"No connection record found for tenant_code={tenant_code!r} "
                f"connection_id={connection_id!r}."
            )
        return self._deserialise(item)

    def resolve_connection(self, tenant_code: str, connection_id: str) -> SourceConnection:
        """
        Load a connection, synthesising the migration default when absent.

        Backward compatibility for the DL-12 rollout: a pre-migration source is
        addressed as `connection_id == source_id` and behaves as a tenant-owned
        active connection until the record is created.
        """
        try:
            return self.get_connection(tenant_code, connection_id)
        except ConnectionNotFoundError:
            _logger.info(
                "source_connection_default_synthesised",
                tenant_code=tenant_code,
                connection_id=connection_id,
            )
            return default_connection_for_source(tenant_code, connection_id)

    def list_connections(
        self,
        tenant_code: str,
        *,
        source_id: str | None = None,
        include_retired: bool = False,
    ) -> list[SourceConnection]:
        tenant_code = validate_tenant_code(tenant_code)
        connections: list[SourceConnection] = []
        for item in iter_items(
            self._table,
            KeyConditionExpression="tenant_code = :tc",
            ExpressionAttributeValues={":tc": tenant_code},
        ):
            try:
                connection = self._deserialise(item)
            except ValidationError:
                _logger.warning(
                    "source_connection_skipped_invalid_record",
                    tenant_code=tenant_code,
                    connection_id=item.get("connection_id"),
                )
                continue
            if source_id is not None and connection.source_id != source_id:
                continue
            if not include_retired and connection.state is ConnectionState.RETIRED:
                continue
            connections.append(connection)
        record_platform_metric(
            PlatformMetric.CONNECTIONS_PER_TENANT, len(connections), TenantCode=tenant_code
        )
        for connection in connections:
            record_platform_metric(
                PlatformMetric.CONNECTION_HEALTH,
                1.0 if connection.is_extractable else 0.0,
                ConnectionId=connection.connection_id,
            )
        return sorted(connections, key=lambda c: c.connection_id)

    def list_extractable_connections(
        self, tenant_code: str, source_id: str | None = None
    ) -> list[SourceConnection]:
        """Connections a scheduler may still trigger — active or failing only."""
        return [
            c for c in self.list_connections(tenant_code, source_id=source_id) if c.is_extractable
        ]

    # ── Writes ────────────────────────────────────────────────────────────────

    def register_connection(self, connection: SourceConnection) -> None:
        item = self._serialise(connection)
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression=(
                    "attribute_not_exists(tenant_code) AND attribute_not_exists(connection_id)"
                ),
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ConnectionAlreadyExistsError(
                    f"Connection {connection.connection_id!r} already exists for tenant "
                    f"{connection.tenant_code!r}."
                ) from exc
            raise

    def save_connection(self, connection: SourceConnection) -> None:
        """Upsert — used by migrations and by lifecycle transitions."""
        self._table.put_item(Item=self._serialise(connection))

    def transition_state(
        self, tenant_code: str, connection_id: str, target: ConnectionState
    ) -> SourceConnection:
        current = self.get_connection(tenant_code, connection_id)
        validate_connection_transition(current.state, target)
        updated = current.transitioned_to(target)
        self.save_connection(updated)
        _logger.info(
            "source_connection_state_transitioned",
            tenant_code=tenant_code,
            connection_id=connection_id,
            from_state=current.state.value,
            to_state=target.value,
        )
        return updated

    def record_successful_run(self, tenant_code: str, connection_id: str) -> None:
        """Health signal for `ConnectionHealth` — last successful extraction timestamp."""
        self._table.update_item(
            Key={"tenant_code": validate_tenant_code(tenant_code), "connection_id": connection_id},
            UpdateExpression="SET last_successful_run_at = :ts",
            ExpressionAttributeValues={":ts": datetime.now(UTC).isoformat()},
        )

    def record_credential_verified(self, tenant_code: str, connection_id: str) -> None:
        self._table.update_item(
            Key={"tenant_code": validate_tenant_code(tenant_code), "connection_id": connection_id},
            UpdateExpression="SET credential_verified_at = :ts",
            ExpressionAttributeValues={":ts": datetime.now(UTC).isoformat()},
        )

    # ── Serialisation ─────────────────────────────────────────────────────────

    @staticmethod
    def _serialise(connection: SourceConnection) -> dict[str, Any]:
        return connection.model_dump(mode="json", exclude_none=False)

    @staticmethod
    def _deserialise(item: dict[str, Any]) -> SourceConnection:
        return SourceConnection(**item)
