"""
Deletion workflow, certificate, and legal hold (DL-PORT-04, DL-PORT-05).

A saga with verification: each store's deletion is a step whose completion is *verified*
before the certificate is issued. A partial deletion certificate is worse than none, so an
unverified step blocks issuance rather than being reported as done.

Security (OWASP A04, A09): deletion is irreversible, so it requires maker-checker plus an
explicit typed confirmation, and the legal-hold state is checked before execution. The audit
trail survives deletion of the data it describes.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

import boto3
from boto3.dynamodb.conditions import Key

from contracts.identifier_policy import validate_tenant_code
from contracts.platform_metrics import PlatformMetric
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger
from persistence.dynamodb_paging import iter_items
from persistence.tenant_tables import (
    TENANT_ATTRIBUTED_INDEX,
    TENANT_ATTRIBUTED_TABLES,
    TENANT_SCOPED_KEY_TABLES,
)

_logger = get_platform_logger(__name__)

_TABLE_NAME: Final[str] = "EdlDeletionCertificate"

# The operator must type this exactly; a yes/no prompt is too easy to click through.
TYPED_CONFIRMATION_TEMPLATE: Final[str] = "DELETE ALL DATA FOR {tenant_code}"

# The §24.7 transition window before deletion becomes contractually operative.
TRANSITION_WINDOW_DAYS: Final[int] = 180


class DeletionStore(StrEnum):
    """Every store a complete deletion must cover."""

    S3_RAW = "s3_raw"
    S3_CURATED = "s3_curated"
    S3_ANALYTICS = "s3_analytics"
    S3_GOVERNANCE = "s3_governance"
    S3_SCHEMA_SNAPSHOTS = "s3_schema_snapshots"
    S3_EXPORTS = "s3_exports"
    DYNAMODB_TABLES = "dynamodb_tables"
    SECRETS_MANAGER = "secrets_manager"
    SERVING_STORE = "serving_store"
    CLOUDWATCH_LOGS = "cloudwatch_logs"
    ML_ARTEFACTS = "ml_artefacts"


# The six S3 buckets plus every other store; a certificate naming fewer is incomplete.
REQUIRED_DELETION_STORES: Final[frozenset[DeletionStore]] = frozenset(DeletionStore)


class StepOutcome(StrEnum):
    """Per-store deletion outcome."""

    PENDING = "pending"
    DELETED = "deleted"
    VERIFIED = "verified"
    RETAINED_UNDER_HOLD = "retained_under_hold"
    RETAINED_LEGAL_OBLIGATION = "retained_legal_obligation"
    FAILED = "failed"


class DeletionNotAuthorisedError(Exception):
    """Raised when maker-checker or the typed confirmation is missing."""


class LegalHoldActiveError(Exception):
    """Raised when deletion is attempted over a held scope without acknowledging the hold."""


class IncompleteDeletionError(Exception):
    """Raised when a certificate is requested before every store is verified."""


@dataclass
class DeletionStep:
    """One store's deletion, with the evidence that it happened."""

    store: DeletionStore
    outcome: StepOutcome = StepOutcome.PENDING
    objects_deleted: int = 0
    verification_detail: str = ""
    retention_basis: str = ""
    error_message: str = ""

    @property
    def is_complete(self) -> bool:
        return self.outcome in (
            StepOutcome.VERIFIED,
            StepOutcome.RETAINED_UNDER_HOLD,
            StepOutcome.RETAINED_LEGAL_OBLIGATION,
        )


@dataclass
class DeletionRequest:
    """An authorised deletion of one tenant's data."""

    tenant_code: str
    requested_by: str
    approved_by: str
    typed_confirmation: str
    scope_description: str = "all customer data"
    acknowledged_holds: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_tenant_code(self.tenant_code)
        if not self.approved_by or self.approved_by == self.requested_by:
            raise DeletionNotAuthorisedError(
                "Deletion is irreversible and requires an approver distinct from the requester."
            )
        expected = TYPED_CONFIRMATION_TEMPLATE.format(tenant_code=self.tenant_code)
        if self.typed_confirmation != expected:
            raise DeletionNotAuthorisedError(
                f"Typed confirmation does not match. Type exactly: {expected!r}."
            )


@dataclass
class DeletionCertificate:
    """
    The written confirmation §24.7 requires.

    Enumerates what was deleted, when, and what was retained under which obligation.
    """

    tenant_code: str
    certificate_id: str
    executed_at: str
    requested_by: str
    approved_by: str
    scope_description: str
    steps: tuple[DeletionStep, ...]

    @property
    def total_objects_deleted(self) -> int:
        return sum(step.objects_deleted for step in self.steps)

    @property
    def retained_items(self) -> tuple[DeletionStep, ...]:
        return tuple(
            step
            for step in self.steps
            if step.outcome
            in (StepOutcome.RETAINED_UNDER_HOLD, StepOutcome.RETAINED_LEGAL_OBLIGATION)
        )

    @property
    def is_complete(self) -> bool:
        covered = {step.store for step in self.steps}
        return covered >= REQUIRED_DELETION_STORES and all(s.is_complete for s in self.steps)

    def render_markdown(self) -> str:
        lines = [
            f"# Data deletion certificate — {self.tenant_code}",
            "",
            f"**Certificate id:** {self.certificate_id}  ",
            f"**Executed at:** {self.executed_at}  ",
            f"**Requested by:** {self.requested_by}  ",
            f"**Approved by:** {self.approved_by}  ",
            f"**Scope:** {self.scope_description}",
            "",
            f"**Objects deleted:** {self.total_objects_deleted}",
            "",
            "## Stores",
            "",
            "| Store | Outcome | Objects deleted | Verification |",
            "|---|---|---|---|",
        ]
        for step in sorted(self.steps, key=lambda s: s.store.value):
            lines.append(
                f"| {step.store.value} | {step.outcome.value} | {step.objects_deleted} | "
                f"{step.verification_detail or '—'} |"
            )
        retained = self.retained_items
        if retained:
            lines.extend(["", "## Retained under obligation", "", "| Store | Basis |", "|---|---|"])
            for step in retained:
                lines.append(f"| {step.store.value} | {step.retention_basis or '—'} |")
        lines.append("")
        return "\n".join(lines)


StoreDeleter = Any
"""Callable (tenant_code) -> tuple[int, str]: (objects_deleted, verification_detail)."""


class DeletionSaga:
    """Executes each store's deletion, verifies it, and issues the certificate."""

    def __init__(
        self,
        environment: str,
        region_name: str,
        deleters: dict[DeletionStore, StoreDeleter],
        held_stores: dict[DeletionStore, str] | None = None,
        legally_retained: dict[DeletionStore, str] | None = None,
    ) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        self._environment = environment
        self._deleters = deleters
        self._held = held_stores or {}
        self._legally_retained = legally_retained or {}
        table_name = os.environ.get("DELETION_CERTIFICATE_TABLE") or _TABLE_NAME
        self._table = boto3.resource("dynamodb", region_name=region_name).Table(table_name)

    def execute(self, request: DeletionRequest) -> DeletionCertificate:
        """
        Delete every store, verify each, and issue the certificate.

        A hold over a store the request has not acknowledged raises rather than silently
        skipping — an operator must know their deletion will be partial before it runs.
        """
        unacknowledged = set(self._held) - {
            DeletionStore(store) for store in request.acknowledged_holds if _is_store(store)
        }
        if unacknowledged:
            raise LegalHoldActiveError(
                f"Legal hold is active over {sorted(s.value for s in unacknowledged)}. "
                "Acknowledge each held store in the request, or lift the hold first."
            )

        steps: list[DeletionStep] = []
        for store in sorted(REQUIRED_DELETION_STORES, key=lambda s: s.value):
            steps.append(self._execute_step(store, request))

        certificate = DeletionCertificate(
            tenant_code=request.tenant_code,
            certificate_id=f"dcert-{uuid.uuid4().hex[:12]}",
            executed_at=datetime.now(UTC).isoformat(),
            requested_by=request.requested_by,
            approved_by=request.approved_by,
            scope_description=request.scope_description,
            steps=tuple(steps),
        )
        if not certificate.is_complete:
            incomplete = [s.store.value for s in steps if not s.is_complete]
            # Persisted before raising: the failed attempt is itself compliance evidence.
            self._persist(certificate, complete=False)
            raise IncompleteDeletionError(
                f"Deletion did not complete for {incomplete}. A partial certificate is worse "
                "than none, so no certificate was issued."
            )
        self._persist(certificate, complete=True)
        record_platform_metric(PlatformMetric.ADMIN_ACTIONS, 1.0, Capability="data_deletion")
        record_platform_metric(
            PlatformMetric.DELETION_STEPS_COMPLETED,
            sum(1 for step in certificate.steps if step.is_complete),
        )
        record_platform_metric(PlatformMetric.LEGAL_HOLDS_ACTIVE, len(certificate.retained_items))
        record_platform_metric(
            PlatformMetric.RETENTION_RECORDS_EXPIRED, certificate.total_objects_deleted
        )
        _logger.warning(
            "deletion_certificate_issued",
            tenant_code=request.tenant_code,
            certificate_id=certificate.certificate_id,
            objects_deleted=certificate.total_objects_deleted,
            retained_stores=[s.store.value for s in certificate.retained_items],
        )
        return certificate

    def _execute_step(self, store: DeletionStore, request: DeletionRequest) -> DeletionStep:
        if store in self._held:
            return DeletionStep(
                store=store,
                outcome=StepOutcome.RETAINED_UNDER_HOLD,
                retention_basis=self._held[store],
            )
        if store in self._legally_retained:
            return DeletionStep(
                store=store,
                outcome=StepOutcome.RETAINED_LEGAL_OBLIGATION,
                retention_basis=self._legally_retained[store],
            )
        deleter = self._deleters.get(store)
        if deleter is None:
            return DeletionStep(
                store=store,
                outcome=StepOutcome.FAILED,
                error_message=(
                    f"No deleter configured for {store.value}; a store with no deleter cannot "
                    "be certified as deleted."
                ),
            )
        try:
            deleted, verification = deleter(request.tenant_code)
        except Exception as exc:
            return DeletionStep(
                store=store,
                outcome=StepOutcome.FAILED,
                error_message=f"{type(exc).__name__}: {exc}",
            )
        if not verification:
            return DeletionStep(
                store=store,
                outcome=StepOutcome.DELETED,
                objects_deleted=int(deleted),
                error_message="deletion reported but not verified",
            )
        return DeletionStep(
            store=store,
            outcome=StepOutcome.VERIFIED,
            objects_deleted=int(deleted),
            verification_detail=str(verification),
        )

    def _persist(self, certificate: DeletionCertificate, *, complete: bool) -> None:
        self._table.put_item(
            Item={
                "tenant_code": certificate.tenant_code,
                "certificate_id": certificate.certificate_id,
                "executed_at": certificate.executed_at,
                "requested_by": certificate.requested_by,
                "approved_by": certificate.approved_by,
                "scope_description": certificate.scope_description,
                "complete": complete,
                "objects_deleted": certificate.total_objects_deleted,
                "steps": [
                    {
                        "store": step.store.value,
                        "outcome": step.outcome.value,
                        "objects_deleted": step.objects_deleted,
                        "verification_detail": step.verification_detail,
                        "retention_basis": step.retention_basis,
                        "error_message": step.error_message,
                    }
                    for step in certificate.steps
                ],
                "certificate_markdown": certificate.render_markdown(),
                "environment": self._environment,
            }
        )

    def list_certificates(self, tenant_code: str) -> list[dict[str, Any]]:
        response = self._table.query(
            KeyConditionExpression="tenant_code = :tc",
            ExpressionAttributeValues={":tc": validate_tenant_code(tenant_code)},
        )
        return [dict(item) for item in response.get("Items", [])]


def _is_store(value: str) -> bool:
    return value in {store.value for store in DeletionStore}


def s3_prefix_deleter(s3_client: Any, bucket: str, store: DeletionStore) -> StoreDeleter:
    """
    Build a deleter for one bucket's tenant prefix that verifies the prefix is empty after.

    Verification is a re-list rather than a trust of the delete response: an S3 delete can
    partially succeed, and the certificate must reflect reality.
    """

    def delete(tenant_code: str) -> tuple[int, str]:
        prefix = f"{tenant_code}/"
        deleted = 0
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if not keys:
                continue
            s3_client.delete_objects(Bucket=bucket, Delete={"Objects": keys})
            deleted += len(keys)
        remaining = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix).get("KeyCount", 0)
        if remaining:
            raise RuntimeError(
                f"{store.value}: {remaining} object(s) remain under {prefix!r} after deletion."
            )
        return deleted, f"s3://{bucket}/{prefix} verified empty"

    return delete


def _key_projection(key_names: list[str]) -> dict[str, Any]:
    """Project only the key attributes: a delete needs the key, not the row."""
    return {
        "ProjectionExpression": ", ".join(f"#{name}" for name in key_names),
        "ExpressionAttributeNames": {f"#{name}": name for name in key_names},
    }


def _tenant_items(table: Any, table_name: str, tenant_code: str, key_names: list[str]) -> Any:
    """
    Every item belonging to one tenant, by whichever of the three key shapes this table uses.

    The first version of this branched on `hash_key == "tenant_code"` and swept everything else with
    `begins_with(hash_key, "tenant#")`. That covers tables keyed `tenant_code` and tables keyed
    `tenant_scoped_key(...)` — but `EdlRunAuditLog` is keyed on `run_id`, which is neither, so the
    filter matched nothing while the caller reported success. An unrecognised shape now raises: the
    sweep must never be able to report zero because it looked in the wrong place.
    """
    hash_key = key_names[0]
    if hash_key == "tenant_code":
        return iter_items(
            table,
            KeyConditionExpression=Key("tenant_code").eq(tenant_code),
            **_key_projection(key_names),
        )
    if table_name in TENANT_SCOPED_KEY_TABLES:
        # `tenant_scoped_key(...)` stores `tenant#...`, which cannot be queried by equality.
        return iter_items(
            table,
            use_query=False,
            FilterExpression=f"begins_with(#{hash_key}, :prefix)",
            ExpressionAttributeValues={":prefix": f"{tenant_code}#"},
            **_key_projection(key_names),
        )
    if table_name in TENANT_ATTRIBUTED_TABLES:
        # Keyed on something else entirely, with `tenant_code` as an ordinary attribute. Read
        # through the tenant GSI, which exists for exactly this.
        return iter_items(
            table,
            IndexName=TENANT_ATTRIBUTED_INDEX,
            KeyConditionExpression=Key("tenant_code").eq(tenant_code),
            **_key_projection(key_names),
        )
    raise IncompleteDeletionError(
        f"{table_name}: hash key {hash_key!r} is not a recognised tenant shape, so this deleter "
        "cannot prove it removed the tenant's rows. Declare the table in "
        "persistence/tenant_tables.py rather than letting the sweep silently skip it."
    )


def dynamodb_tenant_item_deleter(
    dynamodb_resource: Any, table_names: tuple[str, ...]
) -> StoreDeleter:
    """
    Delete every item belonging to the tenant across the platform's tenant-scoped tables.

    One deleter for all of them, because `DeletionStore` treats DynamoDB as a single store and a
    certificate must not claim completeness for a subset.

    **Verified, not trusted.** The first version returned 0 for `EdlRunAuditLog` — see
    `_tenant_items` — and returning 0 with no error meant the saga counted the step complete and
    issued the certificate. A deletion certificate is a compliance artefact handed to a customer
    (SOW §24.7); that one asserted deletion of rows still present. Failing loudly, as this did
    before any deleter existed, was strictly safer than succeeding wrongly. So the sweep re-reads
    after deleting, exactly as `s3_prefix_deleter` does.
    """

    def delete(tenant_code: str) -> tuple[int, str]:
        deleted = 0
        for table_name in table_names:
            table = dynamodb_resource.Table(table_name)
            key_names = [element["AttributeName"] for element in table.key_schema]
            with table.batch_writer() as batch:
                for item in _tenant_items(table, table_name, tenant_code, key_names):
                    batch.delete_item(Key={name: item[name] for name in key_names})
                    deleted += 1
            remaining = sum(1 for _ in _tenant_items(table, table_name, tenant_code, key_names))
            if remaining:
                raise IncompleteDeletionError(
                    f"{table_name}: {remaining} item(s) for tenant {tenant_code!r} remain after "
                    "deletion. No certificate is issued for a partial sweep."
                )
        tables = len(table_names)
        return deleted, f"{deleted} item(s) removed and verified across {tables} table(s)"

    return delete


def secrets_manager_tenant_deleter(secrets_client: Any) -> StoreDeleter:
    """
    Delete every secret under `edl/tenants/{tenant_code}/`, with no recovery window.

    `ForceDeleteWithoutRecovery` is deliberate: a 7-30 day recovery window means the credential
    still exists after the certificate says it does not, which would make the certificate false.
    """

    def delete(tenant_code: str) -> tuple[int, str]:
        prefix = f"edl/tenants/{tenant_code}/"
        deleted = 0
        paginator = secrets_client.get_paginator("list_secrets")
        for page in paginator.paginate(
            Filters=[{"Key": "name", "Values": [prefix]}], MaxResults=100
        ):
            for secret in page.get("SecretList", []):
                name = str(secret.get("Name", ""))
                if not name.startswith(prefix):
                    continue
                secrets_client.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
                deleted += 1
        return deleted, f"{deleted} secret(s) removed under {prefix}"

    return delete


def cloudwatch_logs_tenant_deleter(
    logs_client: Any, log_group_names: tuple[str, ...]
) -> StoreDeleter:
    """
    Delete the tenant's log streams, not the shared log groups the platform still needs.

    Streams are named with the tenant prefix; deleting the group would take every other tenant's
    history with it, which is why this is a stream-level sweep.
    """

    def delete(tenant_code: str) -> tuple[int, str]:
        deleted = 0
        absent = 0
        for group in log_group_names:
            paginator = logs_client.get_paginator("describe_log_streams")
            try:
                for page in paginator.paginate(
                    logGroupName=group, logStreamNamePrefix=f"{tenant_code}/"
                ):
                    for stream in page.get("logStreams", []):
                        logs_client.delete_log_stream(
                            logGroupName=group, logStreamName=stream["logStreamName"]
                        )
                        deleted += 1
            except logs_client.exceptions.ResourceNotFoundException:
                # A function that never ran in this environment has no log group, so it holds none
                # of the tenant's data. Counted and reported rather than swallowed: the certificate
                # must say what it covered, and this is a real zero rather than a failed step.
                absent += 1
        covered = len(log_group_names) - absent
        return deleted, (
            f"{deleted} log stream(s) removed across {covered} group(s); {absent} group(s) absent"
        )

    return delete


def serving_store_tenant_deleter(drop_container: Any) -> StoreDeleter:
    """
    Drop the tenant's serving-store container (database or schema) via the engine's own loader.

    Takes a callable rather than a connection so this module holds no engine driver: the loader
    registry already resolves the right one per engine, and `decide_isolation()` already decides
    whether the container is a database or a schema.
    """

    def delete(tenant_code: str) -> tuple[int, str]:
        dropped = drop_container(tenant_code)
        return int(dropped), f"serving-store container dropped for {tenant_code}"

    return delete


def no_artefacts_deleter(store: DeletionStore, reason: str) -> StoreDeleter:
    """
    A store that is complete because it holds nothing for this tenant, saying so explicitly.

    Used for `ML_ARTEFACTS` while DL-05 is deferred: there is no ML platform, so there are no
    artefacts to delete. That is a real, defensible zero — but it must be *stated*, because the
    alternative is omitting the store from the deleter map, which makes the saga refuse to certify
    and turns a complete deletion into a permanent failure.
    """

    def delete(_tenant_code: str) -> tuple[int, str]:
        return 0, f"{store.value}: nothing to delete — {reason}"

    return delete
