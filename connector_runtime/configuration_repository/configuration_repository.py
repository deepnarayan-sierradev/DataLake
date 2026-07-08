"""
Configuration repository client for the Enterprise Data Lake platform.

Loads EntityExtractionConfig records from DynamoDB (primary) or S3 (alternate).
All records are Pydantic-validated before being returned — invalid configurations
are rejected before the connector runtime starts.

DynamoDB table: EdlEntityExtractionConfig
  PK: source_id (str)
  SK: entity_id (str)

S3 path (when ConfigurationBackend.S3 is selected):
  s3://{bucket}/{tenant_code}/{source_id}/{entity_id}/config.json

Security:
  - DynamoDB reads use the injected boto3 session (IAM role — no credentials in code).
  - S3 reads use the same session; no public bucket access is permitted by bucket policy.
  - Validation errors include field names only — never raw stored values.
"""

from __future__ import annotations

import json
import os
from enum import StrEnum
from typing import Any

import boto3
from botocore.exceptions import ClientError
from pydantic import ValidationError

from contracts.entity_configuration_contract import EntityExtractionConfig
from contracts.identifier_policy import DEFAULT_TENANT_CODE, validate_tenant_code
from contracts.identifier_policy import STABLE_ID_PATTERN as _STABLE_ID_PATTERN
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

_DYNAMODB_TABLE_NAME: str = "EdlEntityExtractionConfig"


class ConfigurationBackend(StrEnum):
    """Storage backend for entity extraction configuration records."""

    DYNAMODB = "dynamodb"
    S3 = "s3"


class ConfigurationNotFoundError(Exception):
    """Raised when no configuration record exists for the given source/entity."""


class ConfigurationValidationError(Exception):
    """Raised when a stored configuration record fails Pydantic model validation."""


class ConfigurationAlreadyExistsError(Exception):
    """Raised by save_config when a record already exists and overwrite=False."""


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
            self._table_name = os.environ.get("ENTITY_CONFIG_TABLE") or _DYNAMODB_TABLE_NAME
            self._table = self._dynamodb.Table(self._table_name)
        else:
            if not s3_bucket:
                raise ValueError("s3_bucket is required when backend is ConfigurationBackend.S3")
            self._s3 = boto3.client("s3", region_name=region_name)
            self._s3_bucket = s3_bucket

    # ── Public API ─────────────────────────────────────────────────────────────

    def load_config(
        self, source_id: str, entity_id: str, tenant_code: str = DEFAULT_TENANT_CODE
    ) -> EntityExtractionConfig:
        """
        Load and validate the configuration record for the given source/entity.

        Args:
            tenant_code: Tenant the caller is loading on behalf of (§1.1 / ARCH-1).
                Cross-checked against the stored record's own `tenant_code` field
                (see `_enforce_tenant_match`) so a record seeded for one tenant is
                never handed back to a caller acting for a different tenant, even
                though the DynamoDB key itself is not yet tenant-partitioned (that
                requires the table migration tracked separately — see
                architecture/MULTI_TENANT_ROLLOUT_PLAN.md §1.5).

        Raises:
            ValueError: source_id, entity_id, or tenant_code does not conform to
                its stable identifier format.
            ConfigurationNotFoundError: no record exists for source_id/entity_id,
                or the stored record belongs to a different tenant.
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
        tenant_code = validate_tenant_code(tenant_code)
        if self._backend == ConfigurationBackend.DYNAMODB:
            config = self._load_from_dynamodb(source_id, entity_id)
        else:
            config = self._load_from_s3(source_id, entity_id, tenant_code)
        self._enforce_tenant_match(config, tenant_code)
        return config

    def save_config(self, config: EntityExtractionConfig, *, overwrite: bool = False) -> None:
        """
        Persist a validated EntityExtractionConfig record (control-plane write path).

        Only implemented for the DynamoDB backend today — the S3 backend is
        used for infrequently-changing bulk-loaded configuration and has no
        corresponding write path yet (tracked as follow-up work).

        Args:
            config:    A validated EntityExtractionConfig instance.
            overwrite: When False (default), the write is conditioned on the
                (source_id, entity_id) pair not already existing, giving
                idempotent *creation* semantics suitable for entity
                registration. Pass True to update an existing record.

        Raises:
            NotImplementedError: When called against the S3 backend.
            ConfigurationAlreadyExistsError: overwrite=False and a record
                already exists for (config.source_id, config.entity_id).
        """
        if self._backend != ConfigurationBackend.DYNAMODB:
            raise NotImplementedError("save_config is only implemented for the DynamoDB backend.")

        item = config.model_dump(mode="json")
        put_kwargs: dict[str, Any] = {"Item": item}
        if not overwrite:
            put_kwargs["ConditionExpression"] = (
                "attribute_not_exists(source_id) AND attribute_not_exists(entity_id)"
            )
        try:
            self._table.put_item(**put_kwargs)
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            if error_code == "ConditionalCheckFailedException":
                raise ConfigurationAlreadyExistsError(
                    f"Configuration record already exists for source_id={config.source_id!r} "
                    f"entity_id={config.entity_id!r}. Use overwrite=True to update."
                ) from exc
            _logger.error(
                "configuration_save_dynamodb_error",
                source_id=config.source_id,
                entity_id=config.entity_id,
                error_code=error_code,
            )
            raise

    def list_configs_for_tenant(self, tenant_code: str) -> list[EntityExtractionConfig]:
        """
        Return all validated EntityExtractionConfig records belonging to tenant_code.

        Implemented as a full table Scan with a FilterExpression on
        tenant_code because the entity-extraction-config table is keyed on
        (source_id, entity_id) with tenant_code as a plain attribute — not
        part of the key — and there is no tenant-scoped GSI yet. This is
        adequate at current table sizes; adding a GSI hash-keyed on
        tenant_code (to make this an efficient Query) is tracked as follow-up
        infra work, not implemented speculatively here.

        Records that fail EntityExtractionConfig validation are skipped
        (logged as a warning) rather than raised, so one malformed record
        does not break the whole listing.

        Raises:
            NotImplementedError: When called against the S3 backend.
        """
        if self._backend != ConfigurationBackend.DYNAMODB:
            raise NotImplementedError(
                "list_configs_for_tenant is only implemented for the DynamoDB backend."
            )
        tenant_code = validate_tenant_code(tenant_code)

        configs: list[EntityExtractionConfig] = []
        scan_kwargs: dict[str, Any] = {
            "FilterExpression": "tenant_code = :tc",
            "ExpressionAttributeValues": {":tc": tenant_code},
        }
        while True:
            response = self._table.scan(**scan_kwargs)
            for item in response.get("Items", []):
                record: dict[str, Any] = dict(item)
                try:
                    configs.append(EntityExtractionConfig(**record))
                except ValidationError:
                    _logger.warning(
                        "list_configs_skipped_invalid_record",
                        source_id=item.get("source_id"),
                        entity_id=item.get("entity_id"),
                    )
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            scan_kwargs["ExclusiveStartKey"] = last_key
        return configs

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

    def _load_from_s3(
        self, source_id: str, entity_id: str, tenant_code: str
    ) -> EntityExtractionConfig:
        # Tenant-prefixed path (matches the convention already established in
        # transformation/curated_layer_writer.py) — genuinely IAM-enforceable
        # via an S3 bucket-policy condition on the key prefix, unlike the
        # DynamoDB backend above.
        s3_key = f"{tenant_code}/{source_id}/{entity_id}/config.json"
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

    @staticmethod
    def _enforce_tenant_match(config: EntityExtractionConfig, tenant_code: str) -> None:
        """
        Reject a record that belongs to a different tenant than the caller.

        Application-level guard (not yet IAM-enforced — see load_config's
        docstring) that prevents cross-tenant reads through this client if two
        tenants' records happen to share the same source_id/entity_id in a
        not-yet-partitioned table. Raises ConfigurationNotFoundError rather
        than a permission error so callers cannot distinguish "wrong tenant"
        from "does not exist" (no existence leakage across tenants).
        """
        if config.tenant_code != tenant_code:
            raise ConfigurationNotFoundError(
                f"No configuration record found for source_id={config.source_id!r} "
                f"entity_id={config.entity_id!r} and tenant_code={tenant_code!r}."
            )

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
