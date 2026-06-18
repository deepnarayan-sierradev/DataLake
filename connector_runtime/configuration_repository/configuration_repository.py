"""
Configuration repository client for the Enterprise Data Lake platform.

Loads EntityExtractionConfig records from DynamoDB (primary) or S3 (alternate).
All records are Pydantic-validated before being returned — invalid configurations
are rejected before the connector runtime starts.

DynamoDB table: {environment}-entity-extraction-config
  PK: source_id (str)
  SK: entity_id (str)

S3 path (when ConfigurationBackend.S3 is selected):
  s3://{bucket}/{source_id}/{entity_id}/config.json

Security:
  - DynamoDB reads use the injected boto3 session (IAM role — no credentials in code).
  - S3 reads use the same session; no public bucket access is permitted by bucket policy.
  - Validation errors include field names only — never raw stored values.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

import boto3
from botocore.exceptions import ClientError
from pydantic import ValidationError

from contracts.entity_configuration_contract import EntityExtractionConfig
from contracts.identifier_policy import STABLE_ID_PATTERN as _STABLE_ID_PATTERN
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

_DYNAMODB_TABLE_TEMPLATE: str = "{environment}-entity-extraction-config"


class ConfigurationBackend(StrEnum):
    """Storage backend for entity extraction configuration records."""

    DYNAMODB = "dynamodb"
    S3 = "s3"


class ConfigurationNotFoundError(Exception):
    """Raised when no configuration record exists for the given source/entity."""


class ConfigurationValidationError(Exception):
    """Raised when a stored configuration record fails Pydantic model validation."""


class ConfigurationRepositoryClient:
    """
    Loads and validates EntityExtractionConfig records from DynamoDB or S3.

    The backend is determined at construction time.  Both backends validate
    the loaded record through the EntityExtractionConfig Pydantic model
    before returning — invalid records are rejected before runtime starts.

    Thread-safety: boto3 DynamoDB and S3 clients are thread-safe for read
    operations.  This client may be shared across threads.
    """

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
            self._table_name = _DYNAMODB_TABLE_TEMPLATE.format(environment=environment)
            self._table = self._dynamodb.Table(self._table_name)
        else:
            if not s3_bucket:
                raise ValueError("s3_bucket is required when backend is ConfigurationBackend.S3")
            self._s3 = boto3.client("s3", region_name=region_name)
            self._s3_bucket = s3_bucket

    # ── Public API ─────────────────────────────────────────────────────────────

    def load_config(self, source_id: str, entity_id: str) -> EntityExtractionConfig:
        """
        Load and validate the configuration record for the given source/entity.

        Raises:
            ValueError: source_id or entity_id does not conform to stable ID format.
            ConfigurationNotFoundError: no record exists for source_id/entity_id.
            ConfigurationValidationError: stored record fails schema validation.
        """
        if not _STABLE_ID_PATTERN.match(source_id):
            raise ValueError(
                f"source_id={source_id!r} does not conform to the stable identifier "
                "format (lowercase letters, digits, hyphens; 2-64 chars; must start "
                "with a letter). Example: 'salesforce', 'mysql-rds'."
            )
        if not _STABLE_ID_PATTERN.match(entity_id):
            raise ValueError(
                f"entity_id={entity_id!r} does not conform to the stable identifier "
                "format (lowercase letters, digits, hyphens; 2-64 chars; must start "
                "with a letter). Example: 'salesforce-account', 'netsuite-customer'."
            )
        if self._backend == ConfigurationBackend.DYNAMODB:
            return self._load_from_dynamodb(source_id, entity_id)
        return self._load_from_s3(source_id, entity_id)

    # ── DynamoDB backend ───────────────────────────────────────────────────────

    def _load_from_dynamodb(self, source_id: str, entity_id: str) -> EntityExtractionConfig:
        try:
            response = self._table.get_item(
                Key={"source_id": source_id, "entity_id": entity_id},
                ConsistentRead=True,
            )
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            _logger.warning(
                "configuration_load_dynamodb_error",
                source_id=source_id,
                entity_id=entity_id,
                error_code=error_code,
            )
            raise ConfigurationNotFoundError(
                f"DynamoDB error loading config for source_id={source_id!r} "
                f"entity_id={entity_id!r}: {error_code}"
            ) from exc

        item = response.get("Item")
        if not item:
            raise ConfigurationNotFoundError(
                f"No configuration record found for source_id={source_id!r} "
                f"entity_id={entity_id!r} in table {self._table_name!r}."
            )

        return self._validate(source_id, entity_id, dict(item))

    # ── S3 backend ─────────────────────────────────────────────────────────────

    def _load_from_s3(self, source_id: str, entity_id: str) -> EntityExtractionConfig:
        s3_key = f"{source_id}/{entity_id}/config.json"
        try:
            response = self._s3.get_object(Bucket=self._s3_bucket, Key=s3_key)
            raw: dict[str, Any] = json.loads(response["Body"].read().decode("utf-8"))
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            if error_code in ("NoSuchKey", "404"):
                raise ConfigurationNotFoundError(
                    f"No configuration record found at s3://{self._s3_bucket}/{s3_key}"
                ) from exc
            # Non-404 errors (throttle, AccessDenied, VPC endpoint failure) must
            # propagate as the original ClientError so callers can distinguish
            # infrastructure failures from genuinely absent records.
            raise

        return self._validate(source_id, entity_id, raw)

    # ── Validation ─────────────────────────────────────────────────────────────

    @staticmethod
    def _validate(source_id: str, entity_id: str, record: dict[str, Any]) -> EntityExtractionConfig:
        try:
            return EntityExtractionConfig(**record)
        except ValidationError as exc:
            raise ConfigurationValidationError(
                f"Configuration record for source_id={source_id!r} "
                f"entity_id={entity_id!r} failed schema validation: "
                f"{exc.error_count()} error(s). Fix the stored record and retry."
            ) from exc
