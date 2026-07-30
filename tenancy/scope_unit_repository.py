"""
`datalake-scope-units-dev` repository and tenant partition profile store (DL-SCOPE-01, DL-SCOPE-02).

The profile lives in the same table under a reserved sort key so a tenant's partition
model and its unit set are read in one place and can never disagree about which units
exist. A tenant with no profile record is `single` — the safe default, because a
`single` tenant's predicate matches only its own implicit unit.

Security (OWASP A01): **absence and failure are different answers.** A read that fails must
never be reported as "no record", because the default profile is `single`, and a `single`
profile for a partitioned tenant produces a match-all predicate on every read surface and
stamps `__tenant__` on its rows at ingestion. A throttle would therefore have silently widened
a franchisee boundary. `_get_item` raises `ScopeStoreUnavailableError` and the caller fails
closed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

import boto3
from botocore.exceptions import ClientError
from pydantic import ValidationError

from contracts.identifier_policy import validate_tenant_code
from contracts.platform_metrics import PlatformMetric
from observability.lambda_runtime import require_env
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger
from persistence.dynamodb_paging import iter_items
from tenancy.scope_contract import (
    IMPLICIT_SCOPE_UNIT_ID,
    PartitionModel,
    ScopeUnit,
    TenantPartitionProfile,
    validate_scope_unit_id,
)

_logger = get_platform_logger(__name__)

_PROFILE_SORT_KEY: Final[str] = "__profile__"


def _as_payload(item: Any) -> dict[str, Any]:
    """DynamoDB attribute values deserialise to a broad union; Pydantic coerces from Any."""
    return {str(key): value for key, value in dict(item).items()}


class ScopeStoreUnavailableError(Exception):
    """Raised when the scope store cannot be read, so no partition answer can be trusted."""


class ScopeUnitNotFoundError(Exception):
    """Raised when no scope unit record exists for the tenant/unit pair."""


class ScopeWideningNotApprovedError(Exception):
    """Raised when narrowing `partitioned` -> `single` is attempted without approval."""


class ScopeUnitRepository:
    """Reads and writes scope units plus the owning tenant's partition profile."""

    def __init__(self, environment: str, region_name: str) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        self._environment = environment
        table_name = require_env("SCOPE_UNIT_TABLE")
        self._table = boto3.resource("dynamodb", region_name=region_name).Table(table_name)

    def get_partition_profile(self, tenant_code: str) -> TenantPartitionProfile:
        tenant_code = validate_tenant_code(tenant_code)
        item = self._get_item(tenant_code, _PROFILE_SORT_KEY)
        if item is None:
            return TenantPartitionProfile(tenant_code=tenant_code)
        persistence_only = {
            "scope_unit_id",
            "updated_at",
            "widening_approved_by",
            "widening_approved_at",
        }
        payload = {k: v for k, v in item.items() if k not in persistence_only}
        try:
            return TenantPartitionProfile(**payload)
        except ValidationError:
            _logger.error(
                "tenant_partition_profile_invalid_failing_closed", tenant_code=tenant_code
            )
            raise

    def save_partition_profile(
        self, profile: TenantPartitionProfile, *, widening_approved_by: str | None = None
    ) -> None:
        """
        Persist a partition profile.

        `partitioned` -> `single` is a widening operation that exposes data across
        units, so it requires an explicit approver and produces an audit record
        (DL-SCOPE-02, OWASP A09).
        """
        current = self.get_partition_profile(profile.tenant_code)
        is_widening = (
            current.partition_model is PartitionModel.PARTITIONED
            and profile.partition_model is PartitionModel.SINGLE
        )
        if is_widening and not widening_approved_by:
            raise ScopeWideningNotApprovedError(
                f"tenant {profile.tenant_code!r}: changing partition_model from 'partitioned' "
                "to 'single' exposes data across scope units and requires an approver."
            )
        item: dict[str, Any] = {
            **profile.model_dump(mode="json"),
            "scope_unit_id": _PROFILE_SORT_KEY,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        if is_widening:
            item["widening_approved_by"] = widening_approved_by
            item["widening_approved_at"] = datetime.now(UTC).isoformat()
            record_platform_metric(
                PlatformMetric.ADMIN_ACTIONS, 1.0, Capability="partition_widening"
            )
            _logger.warning(
                "tenant_partition_widened",
                tenant_code=profile.tenant_code,
                approved_by=widening_approved_by,
            )
        self._table.put_item(Item=item)

    def get_scope_unit(self, tenant_code: str, scope_unit_id: str) -> ScopeUnit:
        tenant_code = validate_tenant_code(tenant_code)
        validate_scope_unit_id(scope_unit_id)
        item = self._get_item(tenant_code, scope_unit_id)
        if item is None:
            raise ScopeUnitNotFoundError(
                f"No scope unit found for tenant_code={tenant_code!r} "
                f"scope_unit_id={scope_unit_id!r}."
            )
        return ScopeUnit(**_as_payload(item))

    def list_scope_units(self, tenant_code: str, *, effective_only: bool = True) -> list[ScopeUnit]:
        tenant_code = validate_tenant_code(tenant_code)
        units: list[ScopeUnit] = []
        for item in iter_items(
            self._table,
            KeyConditionExpression="tenant_code = :tc",
            ExpressionAttributeValues={":tc": tenant_code},
        ):
            if item.get("scope_unit_id") == _PROFILE_SORT_KEY:
                continue
            try:
                unit = ScopeUnit(**_as_payload(item))
            except ValidationError:
                _logger.warning(
                    "scope_unit_skipped_invalid_record",
                    tenant_code=tenant_code,
                    scope_unit_id=item.get("scope_unit_id"),
                )
                continue
            if effective_only and not unit.is_effective_on():
                continue
            units.append(unit)
        return sorted(units, key=lambda u: u.scope_unit_id)

    def save_scope_unit(self, unit: ScopeUnit) -> None:
        profile = self.get_partition_profile(unit.tenant_code)
        if profile.partition_model is PartitionModel.SINGLE:
            raise ValueError(
                f"tenant {unit.tenant_code!r} declares partition_model='single'; declare the "
                "tenant partitioned before registering explicit scope units."
            )
        if profile.partition_kind is not None and unit.partition_kind is not profile.partition_kind:
            raise ValueError(
                f"scope unit {unit.scope_unit_id!r} declares partition_kind "
                f"{unit.partition_kind.value!r} but tenant {unit.tenant_code!r} declares "
                f"{profile.partition_kind.value!r}."
            )
        self._table.put_item(Item=unit.model_dump(mode="json"))

    def known_unit_ids(self, tenant_code: str) -> frozenset[str]:
        """Every unit id a claim may legitimately name, including the implicit one."""
        profile = self.get_partition_profile(tenant_code)
        if profile.partition_model is PartitionModel.SINGLE:
            return frozenset({IMPLICIT_SCOPE_UNIT_ID})
        return frozenset(u.scope_unit_id for u in self.list_scope_units(tenant_code))

    def _get_item(self, tenant_code: str, scope_unit_id: str) -> dict[str, Any] | None:
        """`None` means the record is absent; a failed read raises rather than imitating absence."""
        try:
            response = self._table.get_item(
                Key={"tenant_code": tenant_code, "scope_unit_id": scope_unit_id},
                ConsistentRead=True,
            )
        except ClientError as exc:
            _logger.error(
                "scope_store_read_failed_failing_closed",
                tenant_code=tenant_code,
                scope_unit_id=scope_unit_id,
                error_code=exc.response.get("Error", {}).get("Code", "Unknown"),
            )
            raise ScopeStoreUnavailableError(
                f"The scope store could not be read for tenant {tenant_code!r}. Refusing to "
                "assume a partition model: an unreadable profile is not a single-partition tenant."
            ) from exc
        item = response.get("Item")
        return dict(item) if item else None
