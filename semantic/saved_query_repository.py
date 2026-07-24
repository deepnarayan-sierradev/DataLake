"""
Saved query repository (FR-3.4).

Persists SavedQuery records in DynamoDB (table EdlSavedQuery): PK tenant_code,
SK query_id. Tenant-partitioned from creation.
"""

from __future__ import annotations

import os
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

from contracts.identifier_policy import validate_tenant_code
from observability.structured_logger import get_platform_logger
from semantic.saved_query import SavedQuery

_logger = get_platform_logger(__name__)
_DYNAMODB_TABLE_NAME = "EdlSavedQuery"


class SavedQueryNotFoundError(Exception):
    """Raised when no saved query exists for the given tenant/query_id."""


class SavedQueryRepository:
    def __init__(self, region_name: str) -> None:
        self._dynamodb = boto3.resource("dynamodb", region_name=region_name)
        self._table_name = os.environ.get("SAVED_QUERY_TABLE") or _DYNAMODB_TABLE_NAME
        self._table = self._dynamodb.Table(self._table_name)

    def save(self, tenant_code: str, saved_query: SavedQuery) -> None:
        validate_tenant_code(tenant_code)
        self._table.put_item(
            Item={
                "tenant_code": tenant_code,
                "query_id": saved_query.query_id,
                "name": saved_query.name,
                "entity": saved_query.entity,
                "metrics": list(saved_query.metrics),
                "dimensions": list(saved_query.dimensions),
                "created_by": saved_query.created_by,
            }
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
        validate_tenant_code(tenant_code)
        saved_queries: list[SavedQuery] = []
        kwargs: dict[str, Any] = {"KeyConditionExpression": Key("tenant_code").eq(tenant_code)}
        while True:
            response = self._table.query(**kwargs)
            saved_queries.extend(self._to_saved_query(item) for item in response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
        return saved_queries

    @staticmethod
    def _to_saved_query(item: dict[str, Any]) -> SavedQuery:
        return SavedQuery(
            query_id=str(item["query_id"]),
            name=str(item["name"]),
            entity=str(item["entity"]),
            metrics=tuple(str(metric) for metric in item.get("metrics", [])),
            dimensions=tuple(str(dimension) for dimension in item.get("dimensions", [])),
            created_by=str(item["created_by"]),
        )
