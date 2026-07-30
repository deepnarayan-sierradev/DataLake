"""
Saved query repository (FR-3.4, DL-SEM-07).

Persists SavedQuery records in DynamoDB (table `datalake-saved-query-<env>`):
PK tenant_code, SK query_id.
Tenant-partitioned from creation.

Serialisation goes through the Pydantic model rather than a hand-listed field set. The hand-listed
version named five fields, so when `SavedQuery` gained filters, joins, and a time range, a saved
query would have been *stored* without them and read back as unfiltered — the same class of silent
loss as a filter that never applies, arriving by a different route.
"""

from __future__ import annotations

from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

from contracts.identifier_policy import validate_tenant_code
from observability.lambda_runtime import require_env
from observability.structured_logger import get_platform_logger
from persistence.dynamodb_paging import iter_items
from semantic.saved_query import SavedQuery

_logger = get_platform_logger(__name__)


class SavedQueryNotFoundError(Exception):
    """Raised when no saved query exists for the given tenant/query_id."""


class SavedQueryRepository:
    def __init__(self, region_name: str) -> None:
        self._dynamodb = boto3.resource("dynamodb", region_name=region_name)
        self._table_name = require_env("SAVED_QUERY_TABLE")
        self._table = self._dynamodb.Table(self._table_name)

    def save(self, tenant_code: str, saved_query: SavedQuery) -> None:
        validate_tenant_code(tenant_code)
        self._table.put_item(
            Item={"tenant_code": tenant_code, **saved_query.model_dump(mode="json")}
        )

    def get(self, tenant_code: str, query_id: str) -> SavedQuery:
        validate_tenant_code(tenant_code)
        response = self._table.get_item(Key={"tenant_code": tenant_code, "query_id": query_id})
        item = response.get("Item")
        if item is None:
            raise SavedQueryNotFoundError(
                f"No saved query {query_id!r} for tenant {tenant_code!r}."
            )
        return self._to_saved_query(item)

    def list_for_tenant(self, tenant_code: str) -> list[SavedQuery]:
        """Every saved query for a tenant; drains through the shared paging primitive."""
        validate_tenant_code(tenant_code)
        return [
            self._to_saved_query(item)
            for item in iter_items(
                self._table, KeyConditionExpression=Key("tenant_code").eq(tenant_code)
            )
        ]

    @staticmethod
    def _to_saved_query(item: dict[str, Any]) -> SavedQuery:
        """Validate through the model, so every declared field round-trips or fails loudly."""
        payload = {name: value for name, value in item.items() if name != "tenant_code"}
        return SavedQuery.model_validate(payload)
