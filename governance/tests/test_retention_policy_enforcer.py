"""
Tests for governance/retention_policy_enforcer.py.
"""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from governance.retention_policy_enforcer import (
    LegalHoldResult,
    RetentionEnforcementError,
    RetentionEnforcementResult,
    RetentionPolicyEnforcer,
)

BUCKET = "test-data-bucket"
GOVERNANCE_BUCKET = "test-governance-bucket"
REGION = "us-east-1"


def _create_object_lock_bucket(s3: object, bucket_name: str) -> None:
    """Create an S3 bucket with Object Lock enabled (required by the enforcer)."""
    s3.create_bucket(  # type: ignore[attr-defined]
        Bucket=bucket_name,
        ObjectLockEnabledForBucket=True,
    )


@pytest.fixture()
def s3_setup():
    """Set up moto S3 with object lock enabled."""
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        _create_object_lock_bucket(s3, BUCKET)
        _create_object_lock_bucket(s3, GOVERNANCE_BUCKET)
        # Put a test object
        s3.put_object(Bucket=BUCKET, Key="curated/entity/file.parquet", Body=b"data")
        yield s3


class TestApplyRetention:
    def test_apply_retention_returns_result(self, s3_setup: object) -> None:
        enforcer = RetentionPolicyEnforcer(
            governance_s3_bucket=GOVERNANCE_BUCKET, region_name=REGION
        )
        result = enforcer.apply_retention(
            bucket=BUCKET,
            key="curated/entity/file.parquet",
            retention_days=365,
        )
        assert isinstance(result, RetentionEnforcementResult)
        assert result.bucket == BUCKET
        assert result.key == "curated/entity/file.parquet"
        assert "T" in result.retain_until  # ISO-8601
        assert result.legal_hold_applied is False

    def test_apply_retention_writes_audit_record(self, s3_setup: object) -> None:
        enforcer = RetentionPolicyEnforcer(
            governance_s3_bucket=GOVERNANCE_BUCKET, region_name=REGION
        )
        enforcer.apply_retention(
            bucket=BUCKET,
            key="curated/entity/file.parquet",
            retention_days=90,
        )
        # Verify an audit object was written to the governance bucket
        s3 = boto3.client("s3", region_name=REGION)
        objects = s3.list_objects_v2(Bucket=GOVERNANCE_BUCKET, Prefix="retention-audit/")
        assert objects["KeyCount"] >= 1
        # Verify audit JSON payload
        key = objects["Contents"][0]["Key"]
        body = s3.get_object(Bucket=GOVERNANCE_BUCKET, Key=key)["Body"].read()
        payload = json.loads(body)
        assert payload["bucket"] == BUCKET
        assert payload["key"] == "curated/entity/file.parquet"
        assert payload["legal_hold_applied"] is False


class TestLegalHold:
    def test_apply_legal_hold_returns_result(self, s3_setup: object) -> None:
        enforcer = RetentionPolicyEnforcer(
            governance_s3_bucket=GOVERNANCE_BUCKET, region_name=REGION
        )
        result = enforcer.apply_legal_hold(
            bucket=BUCKET,
            key="curated/entity/file.parquet",
        )
        assert isinstance(result, LegalHoldResult)
        assert result.hold_status == "ON"
        assert result.bucket == BUCKET
        assert result.key == "curated/entity/file.parquet"

    def test_lift_legal_hold_returns_result(self, s3_setup: object) -> None:
        enforcer = RetentionPolicyEnforcer(
            governance_s3_bucket=GOVERNANCE_BUCKET, region_name=REGION
        )
        # First apply, then lift
        enforcer.apply_legal_hold(bucket=BUCKET, key="curated/entity/file.parquet")
        result = enforcer.lift_legal_hold(bucket=BUCKET, key="curated/entity/file.parquet")
        assert result.hold_status == "OFF"


class TestRetentionEnforcementError:
    def test_apply_retention_nonexistent_object_raises_error(self, s3_setup: object) -> None:
        enforcer = RetentionPolicyEnforcer(
            governance_s3_bucket=GOVERNANCE_BUCKET, region_name=REGION
        )
        with pytest.raises(RetentionEnforcementError, match="Failed to apply retention"):
            enforcer.apply_retention(
                bucket=BUCKET,
                key="nonexistent/key.parquet",
                retention_days=30,
            )

    def test_error_message_is_human_readable(self) -> None:
        err = RetentionEnforcementError("test error message")
        assert "test error message" in str(err)
