"""
Configuration repository client for the Enterprise Data Lake platform.

Loads EntityExtractionConfig records from DynamoDB (primary) or S3 (alternate).
All records are Pydantic-validated before being returned — invalid configurations
are rejected before the connector runtime starts.

DynamoDB table: datalake-entity-extraction-config-dev
  PK: source_id (str) — stores tenant_scoped_key(tenant_code, source_id) (ARCH-1/ARCH-03),
      e.g. "demo#salesforce", so two tenants configuring the same source_id/entity_id
      never collide on the same item. The plain source_id is restored on read.
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
from enum import StrEnum
from typing import Any, Final

import boto3
from botocore.exceptions import ClientError
from pydantic import ValidationError

from contracts.entity_configuration_contract import EntityExtractionConfig
from contracts.identifier_policy import (
    DEFAULT_TENANT_CODE,
    strip_tenant_prefix,
    tenant_scoped_key,
    validate_tenant_code,
)
from contracts.identifier_policy import STABLE_ID_PATTERN as _STABLE_ID_PATTERN
from contracts.platform_metrics import PlatformMetric
from observability.lambda_runtime import require_env
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger
from persistence.dynamodb_paging import (
    DEFAULT_PAGE_SIZE,
    fetch_page,
    index_available,
    iter_items,
)
from tenancy.connection_keys import resolve_connection_id

_logger = get_platform_logger(__name__)


SUPPORTED_CONFIG_SCHEMA_VERSIONS: range = range(1, 2)


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


class ConfigurationSchemaIncompatibleError(Exception):
    """Raised when a stored config declares a schema version this runtime cannot parse."""


_TENANT_ENTITY_INDEX: Final[str] = "tenant-entity-index"


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
            self._table_name = require_env("ENTITY_CONFIG_TABLE")
            self._table = self._dynamodb.Table(self._table_name)
            self._index_presence: dict[str, bool] = {}
        else:
            if not s3_bucket:
                raise ValueError("s3_bucket is required when backend is ConfigurationBackend.S3")
            self._s3 = boto3.client("s3", region_name=region_name)
            self._s3_bucket = s3_bucket

    def load_config(
        self,
        source_id: str,
        entity_id: str,
        tenant_code: str = DEFAULT_TENANT_CODE,
        connection_id: str | None = None,
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
        key_id = resolve_connection_id(source_id, connection_id)
        if self._backend == ConfigurationBackend.DYNAMODB:
            config = self._load_from_dynamodb(source_id, entity_id, tenant_code, key_id)
        else:
            config = self._load_from_s3(source_id, entity_id, tenant_code, key_id)
        self._enforce_tenant_match(config, tenant_code)
        self._enforce_schema_compatibility(config)
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
        item["source_system_id"] = config.source_id
        item["source_id"] = tenant_scoped_key(config.tenant_code, config.effective_connection_id)
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

    def _read_kwargs_for_tenant(self, tenant_code: str, use_index: bool) -> dict[str, Any]:
        """Query the tenant GSI when it exists; Scan while an environment has not applied it."""
        if use_index:
            return {
                "IndexName": _TENANT_ENTITY_INDEX,
                "KeyConditionExpression": "tenant_code = :tc",
                "ExpressionAttributeValues": {":tc": tenant_code},
            }
        return {
            "FilterExpression": "tenant_code = :tc",
            "ExpressionAttributeValues": {":tc": tenant_code},
        }

    def _to_config(self, tenant_code: str, item: dict[str, Any]) -> EntityExtractionConfig | None:
        """Validate one stored record; a malformed one is skipped, never fatal to the listing."""
        record: dict[str, Any] = dict(item)
        scoped = strip_tenant_prefix(tenant_code, str(record.get("source_id", "")))
        record["source_id"] = str(record.pop("source_system_id", "") or scoped)
        try:
            return EntityExtractionConfig(**record)
        except ValidationError:
            _logger.warning(
                "list_configs_skipped_invalid_record",
                source_id=item.get("source_id"),
                entity_id=item.get("entity_id"),
            )
            return None

    def list_configs_for_tenant(self, tenant_code: str) -> list[EntityExtractionConfig]:
        """
        Every validated config for a tenant, draining all pages. Internal callers only.

        The `tenant-entity-index` projection is `ALL` as of 2026-07-29, so items come back whole.
        It was `KEYS_ONLY`, and this method issued one `GetItem` per configured entity to rehydrate
        each — serially, unbounded. At the stated target of 80-100 entities per tenant that made the
        platform's most-used endpoint 100+ round trips, which is worse than the Scan the index was
        introduced to replace.

        Request paths must use `page_configs_for_tenant`: this one grows with the tenant's entity
        count and nothing bounds it.
        """
        if self._backend != ConfigurationBackend.DYNAMODB:
            raise NotImplementedError(
                "list_configs_for_tenant is only implemented for the DynamoDB backend."
            )
        tenant_code = validate_tenant_code(tenant_code)
        use_index = self._tenant_index_available()
        configs = [
            config
            for item in iter_items(
                self._table,
                use_query=use_index,
                **self._read_kwargs_for_tenant(tenant_code, use_index),
            )
            if (config := self._to_config(tenant_code, item)) is not None
        ]
        return configs

    def page_configs_for_tenant(
        self,
        tenant_code: str,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        start_key: dict[str, Any] | None = None,
    ) -> tuple[list[EntityExtractionConfig], dict[str, Any] | None]:
        """
        One bounded page of configs plus the cursor to continue from (F9).

        A page can come back short — or empty, on the Scan fallback where a FilterExpression
        discards a whole page — while more pages remain, so callers must follow the cursor rather
        than treat a short page as the end.
        """
        if self._backend != ConfigurationBackend.DYNAMODB:
            raise NotImplementedError(
                "page_configs_for_tenant is only implemented for the DynamoDB backend."
            )
        tenant_code = validate_tenant_code(tenant_code)
        use_index = self._tenant_index_available()
        page = fetch_page(
            self._table,
            limit=limit,
            start_key=start_key,
            use_query=use_index,
            **self._read_kwargs_for_tenant(tenant_code, use_index),
        )
        configs = [
            config
            for item in page.items
            if (config := self._to_config(tenant_code, item)) is not None
        ]
        return configs, page.next_key

    def _tenant_index_available(self) -> bool:
        """One shared probe, cached per client: the GSI may not exist yet in an environment."""
        return index_available(self._table, _TENANT_ENTITY_INDEX, self._index_presence)

    def _load_from_dynamodb(
        self, source_id: str, entity_id: str, tenant_code: str, key_id: str
    ) -> EntityExtractionConfig:
        scoped_source_id = tenant_scoped_key(tenant_code, key_id)
        try:
            response = self._table.get_item(
                Key={"source_id": scoped_source_id, "entity_id": entity_id},
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

        record = {**dict(item), "source_id": source_id}
        record.pop("source_system_id", None)
        return self._validate(source_id, entity_id, record)

    def _load_from_s3(
        self, source_id: str, entity_id: str, tenant_code: str, key_id: str
    ) -> EntityExtractionConfig:
        s3_key = f"{tenant_code}/{key_id}/{entity_id}/config.json"
        try:
            response = self._s3.get_object(Bucket=self._s3_bucket, Key=s3_key)
            raw: dict[str, Any] = json.loads(response["Body"].read().decode("utf-8"))
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            if error_code in ("NoSuchKey", "404"):
                raise ConfigurationNotFoundError(
                    f"No configuration record found at s3://{self._s3_bucket}/{s3_key}"
                ) from exc
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

    @staticmethod
    def _enforce_schema_compatibility(config: EntityExtractionConfig) -> None:
        """
        Fail closed on a config generation this runtime cannot parse (DL-CFG-14).

        OWASP A08: an out-of-range artefact is refused, never parsed leniently.
        """
        if config.config_schema_version not in SUPPORTED_CONFIG_SCHEMA_VERSIONS:
            record_platform_metric(
                PlatformMetric.CONFIG_SCHEMA_INCOMPATIBILITY_REJECTIONS,
                1.0,
                EntityId=config.entity_id,
            )
            raise ConfigurationSchemaIncompatibleError(
                f"Configuration for source_id={config.source_id!r} "
                f"entity_id={config.entity_id!r} declares config_schema_version="
                f"{config.config_schema_version}, outside this runtime's supported range "
                f"[{SUPPORTED_CONFIG_SCHEMA_VERSIONS.start}, "
                f"{SUPPORTED_CONFIG_SCHEMA_VERSIONS.stop - 1}]. Upgrade the runtime or "
                "republish the config at a supported version."
            )

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
