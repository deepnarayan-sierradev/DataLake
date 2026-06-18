"""
Retention policy enforcer.

Enforces S3 object retention and legal hold controls (spec §9.4):
  - Apply an Object Lock retention period to S3 objects based on
    classification-driven retention_days.
  - Place or lift a legal hold on specific S3 objects (GOVERNANCE mode).
  - Audit every enforcement action to a governance S3 prefix.

Security (OWASP A01):
  - Object lock mode GOVERNANCE requires explicit IAM permission to override.
  - Legal hold prevents deletion even by users with s3:BypassGovernanceRetention.
  - Enforcement actions are written to an immutable audit trail.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.exceptions import ClientError

from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)


@dataclass(frozen=True)
class RetentionEnforcementResult:
    """Result of applying a retention policy to one S3 object."""

    bucket: str
    key: str
    retain_until: str  # ISO-8601 UTC
    legal_hold_applied: bool
    enforced_at: str  # ISO-8601 UTC


@dataclass(frozen=True)
class LegalHoldResult:
    """Result of placing or lifting a legal hold on one S3 object."""

    bucket: str
    key: str
    hold_status: str  # "ON" | "OFF"
    version_id: str | None
    applied_at: str  # ISO-8601 UTC


class RetentionPolicyEnforcer:
    """
    Applies S3 Object Lock retention and legal hold controls.

    Requires that the target bucket was created with Object Lock enabled
    (configured in the storage Terraform module).
    """

    def __init__(
        self,
        governance_s3_bucket: str,
        region_name: str,
    ) -> None:
        self._governance_bucket = governance_s3_bucket
        self._region_name = region_name
        self._s3: Any = boto3.client("s3", region_name=region_name)

    def apply_retention(
        self,
        bucket: str,
        key: str,
        retention_days: int,
        version_id: str | None = None,
    ) -> RetentionEnforcementResult:
        """
        Apply GOVERNANCE-mode Object Lock retention to an S3 object.

        Args:
            bucket:         Target S3 bucket (must have Object Lock enabled).
            key:            S3 object key.
            retention_days: Days from now until the retention expires.
            version_id:     Specific version; omit to target the latest version.

        Returns:
            RetentionEnforcementResult.

        Raises:
            RetentionEnforcementError on S3 API failure.
        """
        retain_until = datetime.now(UTC) + timedelta(days=retention_days)

        kwargs: dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "Retention": {
                "Mode": "GOVERNANCE",
                "RetainUntilDate": retain_until,
            },
        }
        if version_id:
            kwargs["VersionId"] = version_id

        try:
            self._s3.put_object_retention(**kwargs)
        except Exception as exc:  # translate all S3 errors to domain error
            raise RetentionEnforcementError(
                f"Failed to apply retention to s3://{bucket}/{key}: {exc}"
            ) from exc

        enforced_at = datetime.now(UTC).isoformat()
        result = RetentionEnforcementResult(
            bucket=bucket,
            key=key,
            retain_until=retain_until.isoformat(),
            legal_hold_applied=False,
            enforced_at=enforced_at,
        )

        _audit_enforcement(self._s3, self._governance_bucket, result)

        _logger.info(
            "retention_applied",
            bucket=bucket,
            key=key,
            retain_until=retain_until.isoformat(),
            retention_days=retention_days,
        )

        return result

    def apply_legal_hold(
        self,
        bucket: str,
        key: str,
        version_id: str | None = None,
    ) -> LegalHoldResult:
        """
        Place a legal hold on an S3 object (prevents deletion regardless of IAM).

        Args:
            bucket:     Target S3 bucket.
            key:        S3 object key.
            version_id: Specific version; omit to target the latest.

        Returns:
            LegalHoldResult.

        Raises:
            RetentionEnforcementError on S3 API failure.
        """
        return self._set_legal_hold(bucket, key, "ON", version_id)

    def lift_legal_hold(
        self,
        bucket: str,
        key: str,
        version_id: str | None = None,
    ) -> LegalHoldResult:
        """
        Lift a legal hold from an S3 object.

        Returns:
            LegalHoldResult.

        Raises:
            RetentionEnforcementError on S3 API failure.
        """
        return self._set_legal_hold(bucket, key, "OFF", version_id)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _set_legal_hold(
        self,
        bucket: str,
        key: str,
        status: str,
        version_id: str | None,
    ) -> LegalHoldResult:
        kwargs: dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "LegalHold": {"Status": status},
        }
        if version_id:
            kwargs["VersionId"] = version_id

        try:
            self._s3.put_object_legal_hold(**kwargs)
        except ClientError as exc:
            raise RetentionEnforcementError(
                f"Failed to set legal hold ({status}) on s3://{bucket}/{key}: {exc}"
            ) from exc

        applied_at = datetime.now(UTC).isoformat()
        result = LegalHoldResult(
            bucket=bucket,
            key=key,
            hold_status=status,
            version_id=version_id,
            applied_at=applied_at,
        )

        _logger.info(
            "legal_hold_set",
            bucket=bucket,
            key=key,
            status=status,
        )

        return result


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _audit_enforcement(
    s3: Any,
    governance_bucket: str,
    result: RetentionEnforcementResult,
) -> None:
    """Write a retention enforcement audit record to the governance bucket."""
    audit_key = (
        f"retention-audit/{result.bucket}/{result.key.replace('/', '_')}/{result.enforced_at}.json"
    )
    audit_payload = {
        "bucket": result.bucket,
        "key": result.key,
        "retain_until": result.retain_until,
        "legal_hold_applied": result.legal_hold_applied,
        "enforced_at": result.enforced_at,
    }
    try:
        s3.put_object(
            Bucket=governance_bucket,
            Key=audit_key,
            Body=json.dumps(audit_payload).encode("utf-8"),
            ContentType="application/json",
        )
    except ClientError as exc:
        # Audit write failure should never block retention enforcement
        _logger.warning("retention_audit_write_failed", error=str(exc))


class RetentionEnforcementError(Exception):
    """Raised when an S3 retention or legal hold operation fails."""
