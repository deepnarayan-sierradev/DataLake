"""
Serving store configuration repository for the Enterprise Data Lake platform.

Loads ServingStoreLoadConfig records from DynamoDB (primary) or S3 (alternate).
All records are Pydantic-validated before being returned.

DynamoDB table: datalake-serving-store-config-dev
  PK: tenant_code (str)
  SK: entity_type (str)

S3 path (when ConfigurationBackend.S3 is selected):
  s3://{bucket}/{tenant_code}/serving-store/{entity_type}/config.json

Security:
  - DynamoDB/S3 reads use the injected boto3 session (IAM role — no credentials in code).
  - The partition key is tenant_code itself (not a composite key needing
    tenant_scoped_key()) — this table is new, so it is tenant-partitioned
    from creation rather than retrofitted.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from pydantic import ValidationError

from contracts.identifier_policy import ENTITY_TYPE_PATTERN as _ENTITY_TYPE_PATTERN
from contracts.identifier_policy import validate_tenant_code
from contracts.serving_store_config_contract import ServingStoreLoadConfig
from observability.lambda_runtime import require_env
from observability.structured_logger import get_platform_logger
from persistence.dynamodb_paging import iter_items

_logger = get_platform_logger(__name__)


class ConfigurationBackend(StrEnum):
    """Storage backend for serving store configuration records."""

    DYNAMODB = "dynamodb"
    S3 = "s3"


class ServingStoreConfigNotFoundError(Exception):
    """Raised when no configuration record exists for the given tenant/entity."""


class ServingStoreConfigValidationError(Exception):
    """Raised when a stored configuration record fails Pydantic model validation."""


class ServingStoreConfigAlreadyExistsError(Exception):
    """Raised by save_config when a record already exists and overwrite=False."""


class ServingStoreConfigRepositoryClient:
    """Loads and validates ServingStoreLoadConfig records from DynamoDB or S3."""

    def __init__(
        self,
        environment: str,
        region_name: str,
        backend: ConfigurationBackend = ConfigurationBackend.DYNAMODB,
        s3_bucket: str | None = None,
    ) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        self._environment = environment
        self._backend = backend

        if backend == ConfigurationBackend.DYNAMODB:
            self._dynamodb = boto3.resource("dynamodb", region_name=region_name)
            self._table_name = require_env("SERVING_STORE_CONFIG_TABLE")
            self._table = self._dynamodb.Table(self._table_name)
        else:
            if not s3_bucket:
                raise ValueError("s3_bucket is required when backend is ConfigurationBackend.S3")
            self._s3 = boto3.client("s3", region_name=region_name)
            self._s3_bucket = s3_bucket

    def load_config(self, tenant_code: str, entity_type: str) -> ServingStoreLoadConfig:
        """
        Load and validate the serving store config record for tenant_code/entity_type.

        Raises:
            ValueError: tenant_code or entity_type fails its identifier format.
            ServingStoreConfigNotFoundError: no record exists.
            ServingStoreConfigValidationError: stored record fails schema validation.
        """
        tenant_code = validate_tenant_code(tenant_code)
        if not _ENTITY_TYPE_PATTERN.match(entity_type):
            raise ValueError(
                f"entity_type={entity_type!r} does not conform to the entity type format."
            )

        if self._backend == ConfigurationBackend.DYNAMODB:
            return self._load_from_dynamodb(tenant_code, entity_type)
        return self._load_from_s3(tenant_code, entity_type)

    def save_config(self, config: ServingStoreLoadConfig, *, overwrite: bool = False) -> None:
        """Persist a validated ServingStoreLoadConfig record (control-plane write path)."""
        if self._backend != ConfigurationBackend.DYNAMODB:
            raise NotImplementedError("save_config is only implemented for the DynamoDB backend.")

        item = config.model_dump(mode="json")
        put_kwargs: dict[str, Any] = {"Item": item}
        if not overwrite:
            put_kwargs["ConditionExpression"] = (
                "attribute_not_exists(tenant_code) AND attribute_not_exists(entity_type)"
            )
        try:
            self._table.put_item(**put_kwargs)
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            if error_code == "ConditionalCheckFailedException":
                raise ServingStoreConfigAlreadyExistsError(
                    f"Serving store config already exists for tenant_code={config.tenant_code!r} "
                    f"entity_type={config.entity_type!r}. Use overwrite=True to update."
                ) from exc
            _logger.error(
                "serving_store_config_save_dynamodb_error",
                tenant_code=config.tenant_code,
                entity_type=config.entity_type,
                error_code=error_code,
            )
            raise

    def list_configs_for_tenant(self, tenant_code: str) -> list[ServingStoreLoadConfig]:
        """
        Return all validated ServingStoreLoadConfig records for tenant_code.

        An efficient Query, not a Scan — tenant_code is the partition key.
        Records that fail validation are skipped (logged as a warning).
        """
        if self._backend != ConfigurationBackend.DYNAMODB:
            raise NotImplementedError(
                "list_configs_for_tenant is only implemented for the DynamoDB backend."
            )
        tenant_code = validate_tenant_code(tenant_code)

        configs: list[ServingStoreLoadConfig] = []
        for item in iter_items(
            self._table, KeyConditionExpression=Key("tenant_code").eq(tenant_code)
        ):
            try:
                configs.append(self._validate(tenant_code, str(item.get("entity_type")), item))
            except ServingStoreConfigValidationError:
                _logger.warning(
                    "serving_store_config_list_skipped_invalid_record",
                    tenant_code=tenant_code,
                    entity_type=item.get("entity_type"),
                )
        return configs

    def _load_from_dynamodb(self, tenant_code: str, entity_type: str) -> ServingStoreLoadConfig:
        try:
            response = self._table.get_item(
                Key={"tenant_code": tenant_code, "entity_type": entity_type},
                ConsistentRead=True,
            )
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            _logger.warning(
                "serving_store_config_load_dynamodb_error",
                tenant_code=tenant_code,
                entity_type=entity_type,
                error_code=error_code,
            )
            raise ServingStoreConfigNotFoundError(
                f"DynamoDB error loading serving store config for tenant_code={tenant_code!r} "
                f"entity_type={entity_type!r}: {error_code}"
            ) from exc

        item = response.get("Item")
        if not item:
            raise ServingStoreConfigNotFoundError(
                f"No serving store config found for tenant_code={tenant_code!r} "
                f"entity_type={entity_type!r} in table {self._table_name!r}."
            )
        return self._validate(tenant_code, entity_type, dict(item))

    def _load_from_s3(self, tenant_code: str, entity_type: str) -> ServingStoreLoadConfig:
        s3_key = f"{tenant_code}/serving-store/{entity_type}/config.json"
        try:
            response = self._s3.get_object(Bucket=self._s3_bucket, Key=s3_key)
            raw: dict[str, Any] = json.loads(response["Body"].read().decode("utf-8"))
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            if error_code in ("NoSuchKey", "404"):
                raise ServingStoreConfigNotFoundError(
                    f"No serving store config found at s3://{self._s3_bucket}/{s3_key}"
                ) from exc
            raise
        return self._validate(tenant_code, entity_type, raw)

    @staticmethod
    def _validate(
        tenant_code: str, entity_type: str, record: dict[str, Any]
    ) -> ServingStoreLoadConfig:
        try:
            return ServingStoreLoadConfig(**record)
        except ValidationError as exc:
            raise ServingStoreConfigValidationError(
                f"Serving store config for tenant_code={tenant_code!r} "
                f"entity_type={entity_type!r} failed schema validation: {exc.error_count()} "
                "error(s)."
            ) from exc
