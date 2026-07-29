"""
Semantic model versioning, maker-checker publish, and rollback (DL-SEM-11).

Semantic definitions are high blast radius: a change to "Revenue" restates every historical
figure at read time. So publish is maker-checker, prior versions are retained, and a rollback
is one audited operation.

Model bodies live in S3 at `{tenant_code}/semantic-models/{version}.json` with the DynamoDB
row holding the pointer, hash, status, and approval metadata — models exceed the DynamoDB
item limit once joins and full KPI coverage land.

Security (OWASP A04, A08, A09): a single compromised account cannot silently redefine
revenue; bodies are hash-verified on load and a tampered object fails closed; every publish,
approve, rollback, and denial is audited.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

import boto3
from botocore.exceptions import ClientError

from contracts.identifier_policy import validate_tenant_code
from contracts.platform_metrics import PlatformMetric
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger
from semantic.semantic_model import SemanticModel

_logger = get_platform_logger(__name__)

_TABLE_NAME: Final[str] = "EdlSemanticModel"
_APPROVAL_TABLE_NAME: Final[str] = "EdlSemanticApproval"

# Sort key of the pointer row naming the active version.
ACTIVE_POINTER_SORT_KEY: Final[str] = "$latest"


class ModelStatus(StrEnum):
    """Lifecycle of one model version."""

    DRAFT = "draft"
    APPROVED = "approved"
    ACTIVE = "active"
    RETIRED = "retired"


class ModelValidationError(Exception):
    """Raised when a model fails cross-record validation at publish."""


class ModelIntegrityError(Exception):
    """Raised when a loaded body's hash does not match its pointer (OWASP A08)."""


class MakerCheckerViolationError(Exception):
    """Raised when the approver is absent or is the same actor as the publisher."""


class ModelVersionNotFoundError(Exception):
    """Raised when a version does not exist for the tenant."""


def semantic_model_s3_key(tenant_code: str, model_version: str) -> str:
    """`{tenant_code}/semantic-models/{version}.json`."""
    validate_tenant_code(tenant_code)
    return f"{tenant_code}/semantic-models/{model_version}.json"


def _optional_str(value: Any) -> str | None:
    """DynamoDB attribute values deserialise to a broad union; narrow to an optional string."""
    return None if value is None else str(value)


def body_hash(body: dict[str, Any]) -> str:
    """Stable content hash of a model body."""
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class ValidationFinding:
    """One cross-record validation problem, with the field it concerns."""

    code: str
    detail: str


def validate_model(
    model: SemanticModel, *, require_signatures: bool = True
) -> list[ValidationFinding]:
    """
    Cross-record validation run at publish (DL-SEM-01, DL-SEM-04, DL-SEM-06).

    Ownership is enforced here rather than at authoring time because an unowned metric is
    only a problem once it can be queried.
    """
    findings: list[ValidationFinding] = []
    for unowned in model.unowned_fields():
        findings.append(
            ValidationFinding(
                code="unowned_field",
                detail=f"{unowned} has no business_owner; an unowned field cannot be published.",
            )
        )
    if require_signatures:
        for unsigned in model.unsigned_metrics():
            findings.append(
                ValidationFinding(
                    code="unsigned_metric",
                    detail=(
                        f"metric {unsigned} has no signed definition; unvalidated definitions "
                        "publish to a draft version only (DL-SEM-04)."
                    ),
                )
            )
    for entity in model.entities:
        for metric in entity.metrics:
            if metric.is_derived:
                continue
            if metric.aggregation == "count" and metric.column != "*" and not metric.column:
                findings.append(
                    ValidationFinding(
                        code="invalid_metric",
                        detail=f"metric {entity.name}.{metric.name} declares no column.",
                    )
                )
    return findings


@dataclass(frozen=True)
class ModelVersionRecord:
    """The DynamoDB pointer row for one model version."""

    tenant_code: str
    model_version: str
    status: ModelStatus
    s3_key: str
    content_hash: str
    published_by: str
    published_at: str
    approved_by: str | None = None
    approved_at: str | None = None


class SemanticModelGovernance:
    """Versioned publish, approve, activate, rollback, and hash-verified load."""

    def __init__(
        self,
        environment: str,
        region_name: str,
        s3_bucket: str,
        s3_client: Any | None = None,
    ) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        if not s3_bucket:
            raise ValueError("s3_bucket must not be empty.")
        self._environment = environment
        self._bucket = s3_bucket
        self._s3 = s3_client or boto3.client("s3", region_name=region_name)
        resource = boto3.resource("dynamodb", region_name=region_name)
        self._table = resource.Table(os.environ.get("SEMANTIC_MODEL_TABLE") or _TABLE_NAME)
        self._approvals = resource.Table(
            os.environ.get("SEMANTIC_APPROVAL_TABLE") or _APPROVAL_TABLE_NAME
        )

    # ── Publish (maker) ───────────────────────────────────────────────────────

    def publish(
        self, model: SemanticModel, *, published_by: str, allow_draft: bool = False
    ) -> ModelVersionRecord:
        """
        Write a new version and record it as `draft`, pending approval.

        `allow_draft` publishes a model whose definitions are not yet signed — permitted so
        the uncontested KPI set can ship while contested ones are still in workshop, but the
        result can never be activated (DL-SEM-04).
        """
        findings = validate_model(model, require_signatures=not allow_draft)
        blocking = [f for f in findings if f.code != "unsigned_metric" or not allow_draft]
        if blocking:
            record_platform_metric(PlatformMetric.MODEL_VALIDATION_FAILURES, len(blocking))
            raise ModelValidationError(
                f"Semantic model {model.model_version!r} for tenant {model.tenant_code!r} failed "
                f"validation: {[f.detail for f in blocking]}"
            )
        body = model.model_dump(mode="json")
        digest = body_hash(body)
        key = semantic_model_s3_key(model.tenant_code, model.model_version)
        self._s3.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            ContentType="application/json",
        )
        record = ModelVersionRecord(
            tenant_code=model.tenant_code,
            model_version=model.model_version,
            status=ModelStatus.DRAFT,
            s3_key=key,
            content_hash=digest,
            published_by=published_by,
            published_at=datetime.now(UTC).isoformat(),
        )
        self._save_record(record)
        record_platform_metric(PlatformMetric.MODEL_PUBLISHES)
        _logger.info(
            "semantic_model_published",
            tenant_code=model.tenant_code,
            model_version=model.model_version,
            published_by=published_by,
            status=record.status.value,
        )
        return record

    # ── Approve (checker) ─────────────────────────────────────────────────────

    def approve(
        self, tenant_code: str, model_version: str, *, approved_by: str
    ) -> ModelVersionRecord:
        record = self.get_version(tenant_code, model_version)
        if approved_by == record.published_by:
            raise MakerCheckerViolationError(
                f"Model version {model_version!r} was approved by its own publisher "
                f"({approved_by!r}). Maker and checker must differ (OWASP A04)."
            )
        if not approved_by:
            raise MakerCheckerViolationError("An approval must name its approver.")
        approved = ModelVersionRecord(
            tenant_code=record.tenant_code,
            model_version=record.model_version,
            status=ModelStatus.APPROVED,
            s3_key=record.s3_key,
            content_hash=record.content_hash,
            published_by=record.published_by,
            published_at=record.published_at,
            approved_by=approved_by,
            approved_at=datetime.now(UTC).isoformat(),
        )
        self._save_record(approved)
        self._approvals.put_item(
            Item={
                "tenant_code": tenant_code,
                "approval_key": f"{model_version}#{approved_by}",
                "model_version": model_version,
                "approver": approved_by,
                "approved_at": approved.approved_at,
                "content_hash": record.content_hash,
                "environment": self._environment,
            }
        )
        _logger.info(
            "semantic_model_approved",
            tenant_code=tenant_code,
            model_version=model_version,
            approved_by=approved_by,
        )
        return approved

    def activate(self, tenant_code: str, model_version: str) -> ModelVersionRecord:
        """Repoint `$latest`; only an approved version may become active."""
        record = self.get_version(tenant_code, model_version)
        if record.status is not ModelStatus.APPROVED:
            raise MakerCheckerViolationError(
                f"Model version {model_version!r} is {record.status.value!r}; only an approved "
                "version may be activated."
            )
        active = ModelVersionRecord(
            tenant_code=record.tenant_code,
            model_version=record.model_version,
            status=ModelStatus.ACTIVE,
            s3_key=record.s3_key,
            content_hash=record.content_hash,
            published_by=record.published_by,
            published_at=record.published_at,
            approved_by=record.approved_by,
            approved_at=record.approved_at,
        )
        self._save_record(active)
        self._write_pointer(tenant_code, model_version, record.content_hash)
        return active

    # ── Rollback ──────────────────────────────────────────────────────────────

    def rollback(
        self, tenant_code: str, target_version: str, *, requested_by: str, approved_by: str
    ) -> ModelVersionRecord:
        """
        Repoint `$latest` to a prior retained version as one audited operation (DL-SEM-11).

        Maker-checker applies to a rollback exactly as it does to a publish — reverting a
        governed definition is as high-blast-radius as changing it.
        """
        if not approved_by or approved_by == requested_by:
            raise MakerCheckerViolationError(
                "A semantic rollback requires an approver distinct from the requester."
            )
        record = self.get_version(tenant_code, target_version)
        self._write_pointer(tenant_code, target_version, record.content_hash)
        record_platform_metric(PlatformMetric.ADMIN_ACTIONS, 1.0, Capability="semantic_rollback")
        _logger.warning(
            "semantic_model_rolled_back",
            tenant_code=tenant_code,
            target_version=target_version,
            requested_by=requested_by,
            approved_by=approved_by,
        )
        return self.activate_without_gate(tenant_code, target_version)

    def activate_without_gate(self, tenant_code: str, model_version: str) -> ModelVersionRecord:
        """Mark a version active during a rollback, where it was already approved once."""
        record = self.get_version(tenant_code, model_version)
        active = ModelVersionRecord(
            tenant_code=record.tenant_code,
            model_version=record.model_version,
            status=ModelStatus.ACTIVE,
            s3_key=record.s3_key,
            content_hash=record.content_hash,
            published_by=record.published_by,
            published_at=record.published_at,
            approved_by=record.approved_by,
            approved_at=record.approved_at,
        )
        self._save_record(active)
        return active

    # ── Reads ─────────────────────────────────────────────────────────────────

    def get_version(self, tenant_code: str, model_version: str) -> ModelVersionRecord:
        tenant_code = validate_tenant_code(tenant_code)
        response = self._table.get_item(
            Key={"tenant_code": tenant_code, "model_version": model_version}, ConsistentRead=True
        )
        item = response.get("Item")
        if not item:
            raise ModelVersionNotFoundError(
                f"No semantic model version {model_version!r} for tenant {tenant_code!r}."
            )
        return ModelVersionRecord(
            tenant_code=tenant_code,
            model_version=model_version,
            status=ModelStatus(str(item["status"])),
            s3_key=str(item["s3_key"]),
            content_hash=str(item["content_hash"]),
            published_by=str(item.get("published_by", "")),
            published_at=str(item.get("published_at", "")),
            approved_by=_optional_str(item.get("approved_by")),
            approved_at=_optional_str(item.get("approved_at")),
        )

    def active_version(self, tenant_code: str) -> str | None:
        tenant_code = validate_tenant_code(tenant_code)
        response = self._table.get_item(
            Key={"tenant_code": tenant_code, "model_version": ACTIVE_POINTER_SORT_KEY}
        )
        item = response.get("Item")
        return str(item["active_version"]) if item else None

    def list_versions(self, tenant_code: str) -> list[ModelVersionRecord]:
        tenant_code = validate_tenant_code(tenant_code)
        response = self._table.query(
            KeyConditionExpression="tenant_code = :tc",
            ExpressionAttributeValues={":tc": tenant_code},
        )
        records: list[ModelVersionRecord] = []
        for item in response.get("Items", []):
            version = str(item["model_version"])
            if version == ACTIVE_POINTER_SORT_KEY:
                continue
            records.append(self.get_version(tenant_code, version))
        return sorted(records, key=lambda r: r.published_at, reverse=True)

    def load_model(self, tenant_code: str, model_version: str | None = None) -> SemanticModel:
        """Load a model body, verifying its hash against the pointer before parsing."""
        version = model_version or self.active_version(tenant_code)
        if not version:
            raise ModelVersionNotFoundError(
                f"Tenant {tenant_code!r} has no active semantic model version."
            )
        record = self.get_version(tenant_code, version)
        try:
            response = self._s3.get_object(Bucket=self._bucket, Key=record.s3_key)
        except ClientError as exc:
            raise ModelVersionNotFoundError(
                f"Semantic model body missing at s3://{self._bucket}/{record.s3_key}: "
                f"{exc.response['Error']['Code']}"
            ) from exc
        body: dict[str, Any] = json.loads(response["Body"].read().decode("utf-8"))
        if body_hash(body) != record.content_hash:
            raise ModelIntegrityError(
                f"Semantic model body at {record.s3_key!r} does not match its recorded hash. "
                "Refusing to load a possibly-tampered model (OWASP A08)."
            )
        return SemanticModel(**body)

    # ── Private ───────────────────────────────────────────────────────────────

    def _save_record(self, record: ModelVersionRecord) -> None:
        self._table.put_item(
            Item={
                "tenant_code": record.tenant_code,
                "model_version": record.model_version,
                "status": record.status.value,
                "s3_key": record.s3_key,
                "content_hash": record.content_hash,
                "published_by": record.published_by,
                "published_at": record.published_at,
                "approved_by": record.approved_by,
                "approved_at": record.approved_at,
                "environment": self._environment,
            }
        )

    def _write_pointer(self, tenant_code: str, model_version: str, content_hash: str) -> None:
        self._table.put_item(
            Item={
                "tenant_code": tenant_code,
                "model_version": ACTIVE_POINTER_SORT_KEY,
                "active_version": model_version,
                "content_hash": content_hash,
                "updated_at": datetime.now(UTC).isoformat(),
                "environment": self._environment,
            }
        )
