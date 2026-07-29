"""Tests for the credential expiry notifier Lambda (SEC-6)."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from connector_runtime.credential_rotation.credential_expiry_notifier_handler import (
    lambda_handler,
)

_REGION = "us-east-1"


@pytest.fixture()
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_REGION", _REGION)
    monkeypatch.setenv("PLATFORM_ENVIRONMENT", "dev")
    with mock_aws():
        yield


def _create_secret(sm_client, name: str, created_days_ago: int) -> str:
    response = sm_client.create_secret(Name=name, SecretString='{"placeholder": "x"}')
    # moto stamps CreatedDate at call time; we can't easily backdate it, so
    # tests assert relative behavior via the rotation_warning_days/secret_rotation_days
    # knobs instead of trying to fake CreatedDate directly.
    return response["ARN"]


class TestCredentialExpiryNotifier:
    def test_fresh_secret_is_not_flagged(self, aws_env, monkeypatch) -> None:
        sm = boto3.client("secretsmanager", region_name=_REGION)
        sns = boto3.client("sns", region_name=_REGION)
        topic_arn = sns.create_topic(Name="platform-alerts")["TopicArn"]
        arn = _create_secret(sm, "dev/sources/salesforce/credentials", created_days_ago=0)

        monkeypatch.setenv("SOURCE_CREDENTIAL_SECRET_ARNS", arn)
        monkeypatch.setenv("ALERT_SNS_TOPIC_ARN", topic_arn)

        result = lambda_handler({}, context=None)

        assert result["checked"] == 1
        assert result["stale"] == 0

    def test_secret_past_warning_threshold_is_flagged(self, aws_env, monkeypatch) -> None:
        sm = boto3.client("secretsmanager", region_name=_REGION)
        sns = boto3.client("sns", region_name=_REGION)
        topic_arn = sns.create_topic(Name="platform-alerts")["TopicArn"]
        arn = _create_secret(sm, "dev/sources/salesforce/credentials", created_days_ago=0)

        monkeypatch.setenv("SOURCE_CREDENTIAL_SECRET_ARNS", arn)
        monkeypatch.setenv("ALERT_SNS_TOPIC_ARN", topic_arn)

        # A freshly-created secret (age 0) with a 0-day rotation window and
        # 0-day warning threshold must be flagged immediately (0 >= 0).
        result = lambda_handler(
            {"secret_rotation_days": 0, "rotation_warning_days": 0}, context=None
        )

        assert result["checked"] == 1
        assert result["stale"] == 1
        assert "dev/sources/salesforce/credentials" in result["stale_secret_names"]

    def test_unreachable_secret_does_not_block_remaining_checks(self, aws_env, monkeypatch) -> None:
        sm = boto3.client("secretsmanager", region_name=_REGION)
        sns = boto3.client("sns", region_name=_REGION)
        topic_arn = sns.create_topic(Name="platform-alerts")["TopicArn"]
        valid_arn = _create_secret(sm, "dev/sources/netsuite/credentials", created_days_ago=0)
        fake_arn = valid_arn.rsplit(":", 1)[0] + ":does-not-exist-XXXXXX"

        monkeypatch.setenv("SOURCE_CREDENTIAL_SECRET_ARNS", f"{fake_arn},{valid_arn}")
        monkeypatch.setenv("ALERT_SNS_TOPIC_ARN", topic_arn)

        result = lambda_handler({}, context=None)

        # Only the valid secret is counted; the bad ARN is skipped, not fatal.
        assert result["checked"] == 1

    def test_no_secrets_configured_returns_zero(self, aws_env, monkeypatch) -> None:
        """A whitespace-only ARN list parses to zero entries, not a hard failure."""
        sns = boto3.client("sns", region_name=_REGION)
        topic_arn = sns.create_topic(Name="platform-alerts")["TopicArn"]
        monkeypatch.setenv("SOURCE_CREDENTIAL_SECRET_ARNS", " , ,")
        monkeypatch.setenv("ALERT_SNS_TOPIC_ARN", topic_arn)

        result = lambda_handler({}, context=None)

        assert result == {"checked": 0, "stale": 0, "stale_secret_names": []}
