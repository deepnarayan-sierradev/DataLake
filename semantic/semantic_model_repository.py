"""
Semantic model repository (FR-2.1).

Persists versioned SemanticModel records in DynamoDB (table EdlSemanticModel):
PK tenant_code, SK model_version, with the model stored as JSON and a
"$latest" pointer item naming the active version. Tenant-partitioned from
creation.
"""

from __future__ import annotations

import os
from typing import Any

import boto3

from contracts.identifier_policy import validate_tenant_code
from observability.structured_logger import get_platform_logger
from semantic.semantic_model import SemanticModel

_logger = get_platform_logger(__name__)
_DYNAMODB_TABLE_NAME = "EdlSemanticModel"
_LATEST_POINTER = "$latest"


class SemanticModelNotFoundError(Exception):
    """Raised when no semantic model exists for the given tenant/version."""


class SemanticModelRepository:
    def __init__(self, region_name: str) -> None:
        self._dynamodb = boto3.resource("dynamodb", region_name=region_name)
        self._table_name = os.environ.get("SEMANTIC_MODEL_TABLE") or _DYNAMODB_TABLE_NAME
        self._table = self._dynamodb.Table(self._table_name)

    def publish(self, model: SemanticModel) -> None:
        validate_tenant_code(model.tenant_code)
        self._table.put_item(
            Item={
                "tenant_code": model.tenant_code,
                "model_version": model.model_version,
                "model_json": model.model_dump_json(),
            }
        )
        self._table.put_item(
            Item={
                "tenant_code": model.tenant_code,
                "model_version": _LATEST_POINTER,
                "active_version": model.model_version,
            }
        )

    def load(self, tenant_code: str, model_version: str) -> SemanticModel:
        validate_tenant_code(tenant_code)
        item = self._get(tenant_code, model_version)
        if item is None or "model_json" not in item:
            raise SemanticModelNotFoundError(
                f"No semantic model {model_version!r} for tenant {tenant_code!r}."
            )
        return SemanticModel.model_validate_json(str(item["model_json"]))

    def load_active(self, tenant_code: str) -> SemanticModel:
        validate_tenant_code(tenant_code)
        pointer = self._get(tenant_code, _LATEST_POINTER)
        if pointer is None or "active_version" not in pointer:
            raise SemanticModelNotFoundError(
                f"No active semantic model for tenant {tenant_code!r}."
            )
        return self.load(tenant_code, str(pointer["active_version"]))

    def _get(self, tenant_code: str, model_version: str) -> dict[str, Any] | None:
        response = self._table.get_item(
            Key={"tenant_code": tenant_code, "model_version": model_version}
        )
        item: dict[str, Any] | None = response.get("Item")
        return item
