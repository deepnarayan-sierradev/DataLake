"""
Twin index repository (FR-1.3 / FR-1.4).

Persists the twin index in DynamoDB (table EdlTwinIndex): PK tenant_code,
SK "{entity_type}#{golden_id}". Stores lifecycle stage, per-relationship
rollups and the edge adjacency list. Tenant-partitioned from creation (PK is
tenant_code) — no tenant_scoped_key() retrofit needed. Mastered attributes are
NOT duplicated here; they stay in the analytics S3 layer and are hydrated on
read as a follow-up (FR-1.4).
"""

from __future__ import annotations

import os
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

from contracts.identifier_policy import ENTITY_TYPE_PATTERN, validate_tenant_code
from knowledge.twin import Twin, TwinEdge
from observability.structured_logger import get_platform_logger
from persistence.dynamodb_paging import DEFAULT_PAGE_SIZE, fetch_page, iter_items

_logger = get_platform_logger(__name__)
_DYNAMODB_TABLE_NAME = "EdlTwinIndex"


def _optional_text(value: Any) -> str | None:
    """Normalise an absent or empty DynamoDB attribute to None rather than the string 'None'."""
    return None if value is None or value == "" else str(value)


class TwinNotFoundError(Exception):
    """Raised when no twin index entry exists for the given tenant/entity/golden_id."""


class TwinRepository:
    def __init__(self, region_name: str) -> None:
        self._dynamodb = boto3.resource("dynamodb", region_name=region_name)
        self._table_name = os.environ.get("TWIN_INDEX_TABLE") or _DYNAMODB_TABLE_NAME
        self._table = self._dynamodb.Table(self._table_name)

    @staticmethod
    def _sort_key(entity_type: str, golden_id: str) -> str:
        return f"{entity_type}#{golden_id}"

    def upsert_twin(self, tenant_code: str, twin: Twin) -> None:
        validate_tenant_code(tenant_code)
        item: dict[str, Any] = {
            "tenant_code": tenant_code,
            "sk": self._sort_key(twin.entity_type, twin.golden_id),
            "entity_type": twin.entity_type,
            "golden_id": twin.golden_id,
            "rollups": twin.rollups,
            "scope_unit_id": twin.scope_unit_id,
            "edges": [
                {
                    "relationship_type": edge.relationship_type,
                    "to_entity_type": edge.to_entity_type,
                    "to_golden_id": edge.to_golden_id,
                    "scope_unit_id": edge.scope_unit_id,
                }
                for edge in twin.edges
            ],
        }
        if twin.lifecycle_stage is not None:
            item["lifecycle_stage"] = twin.lifecycle_stage
        self._table.put_item(Item=item)

    def get_twin(self, tenant_code: str, entity_type: str, golden_id: str) -> Twin:
        validate_tenant_code(tenant_code)
        response = self._table.get_item(
            Key={"tenant_code": tenant_code, "sk": self._sort_key(entity_type, golden_id)}
        )
        item = response.get("Item")
        if item is None:
            raise TwinNotFoundError(
                f"No twin for {entity_type}/{golden_id} in tenant {tenant_code!r}."
            )
        return self._to_twin(item)

    def list_twins(self, tenant_code: str, entity_type: str) -> list[Twin]:
        """Every twin of one entity type. Drains all pages; for internal callers only."""
        validate_tenant_code(tenant_code)
        if not ENTITY_TYPE_PATTERN.match(entity_type):
            raise ValueError(f"Invalid entity_type {entity_type!r}.")
        return [
            self._to_twin(item)
            for item in iter_items(
                self._table,
                KeyConditionExpression=(
                    Key("tenant_code").eq(tenant_code) & Key("sk").begins_with(f"{entity_type}#")
                ),
            )
        ]

    def page_twins(
        self,
        tenant_code: str,
        entity_type: str,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        start_key: dict[str, Any] | None = None,
    ) -> tuple[list[Twin], dict[str, Any] | None]:
        """
        One bounded page of twins plus the cursor to continue from.

        Request-serving callers must use this rather than `list_twins`: the API previously drained
        every twin for the entity type on every request and sliced the result, so page 50 cost
        exactly as much as page 1.
        """
        validate_tenant_code(tenant_code)
        if not ENTITY_TYPE_PATTERN.match(entity_type):
            raise ValueError(f"Invalid entity_type {entity_type!r}.")
        page = fetch_page(
            self._table,
            limit=limit,
            start_key=start_key,
            KeyConditionExpression=(
                Key("tenant_code").eq(tenant_code) & Key("sk").begins_with(f"{entity_type}#")
            ),
        )
        return [self._to_twin(item) for item in page.items], page.next_key

    @staticmethod
    def _to_twin(item: dict[str, Any]) -> Twin:
        edges = tuple(
            TwinEdge(
                relationship_type=str(edge["relationship_type"]),
                to_entity_type=str(edge["to_entity_type"]),
                to_golden_id=str(edge["to_golden_id"]),
                scope_unit_id=_optional_text(edge.get("scope_unit_id")),
            )
            for edge in item.get("edges", [])
        )
        rollups = {str(key): int(value) for key, value in (item.get("rollups") or {}).items()}
        lifecycle_stage = item.get("lifecycle_stage")
        return Twin(
            entity_type=str(item["entity_type"]),
            golden_id=str(item["golden_id"]),
            attributes={},
            edges=edges,
            lifecycle_stage=str(lifecycle_stage) if lifecycle_stage is not None else None,
            rollups=rollups,
            # Items written before DL-SCOPE-13 carry no unit; they stay invisible to a
            # unit-scoped caller until the entity's next twin build restamps them.
            scope_unit_id=_optional_text(item.get("scope_unit_id")),
        )
