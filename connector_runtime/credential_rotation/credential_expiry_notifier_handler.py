"""
Credential expiry notifier Lambda (SEC-6).

Automatic Secrets Manager rotation (aws_secretsmanager_secret_rotation in
infrastructure/modules/secrets/main.tf) is only active when a per-connector
rotation Lambda ARN is configured — none are today, for any connector, in any
environment (confirmed: no environment sets salesforce_rotation_lambda_arn,
netsuite_rotation_lambda_arn, or mysql_rds_rotation_lambda_arn). Building a
rotation Lambda that can safely regenerate credentials against Salesforce,
NetSuite, MySQL RDS, and Sage's respective auth systems is a separate,
per-connector integration effort.

Until that lands, this Lambda closes the observability half of the gap: on a
daily EventBridge schedule, it checks every source-credential secret's age
since creation/last rotation and publishes an SNS notification for any secret
approaching or past its configured rotation window — so a stale credential is
a visible, actionable alert instead of a silent, indefinite risk.

Required Lambda environment variables:
  AWS_REGION                — injected automatically by the Lambda runtime
  PLATFORM_ENVIRONMENT      — deployment environment (dev/staging/prod)
  SOURCE_CREDENTIAL_SECRET_ARNS — comma-separated list of secret ARNs to check
  ALERT_SNS_TOPIC_ARN       — ARN of the platform alerts SNS topic
  ROTATION_WARNING_DAYS     — days before secret_rotation_days to start warning
                              (default: 14)
  SECRET_ROTATION_DAYS      — the rotation window each secret is expected to
                              respect (default: 90 — matches
                              var.secret_rotation_days in Terraform)

Security (OWASP A09):
  - Never reads or logs secret values — only Secrets Manager metadata
    (CreatedDate / LastRotatedDate), which contains no credential material.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

import boto3

from observability.lambda_runtime import require_env
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

_DEFAULT_ROTATION_WARNING_DAYS: Final[int] = 14
_DEFAULT_SECRET_ROTATION_DAYS: Final[int] = 90


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    AWS Lambda entry point, invoked on a daily EventBridge schedule.

    Returns:
        Summary dict: {"checked": int, "stale": int, "stale_secret_names": [...]}.
        Never raises for an individual secret's check failure — a single
        DescribeSecret error must not prevent checking the remaining secrets.
    """
    region_name = require_env("AWS_REGION")
    environment = require_env("PLATFORM_ENVIRONMENT")
    secret_arns_raw = require_env("SOURCE_CREDENTIAL_SECRET_ARNS")
    sns_topic_arn = require_env("ALERT_SNS_TOPIC_ARN")

    # `or` would silently replace an explicit 0 with the default (0 is falsy)
    # — use `is None` so a caller can legitimately request a 0-day threshold.
    _raw_warning_days = event.get("rotation_warning_days")
    rotation_warning_days = (
        int(_raw_warning_days) if _raw_warning_days is not None else _DEFAULT_ROTATION_WARNING_DAYS
    )
    _raw_rotation_days = event.get("secret_rotation_days")
    secret_rotation_days = (
        int(_raw_rotation_days) if _raw_rotation_days is not None else _DEFAULT_SECRET_ROTATION_DAYS
    )
    warning_threshold_days = secret_rotation_days - rotation_warning_days

    secret_arns = [arn.strip() for arn in secret_arns_raw.split(",") if arn.strip()]

    secretsmanager = boto3.client("secretsmanager", region_name=region_name)
    sns = boto3.client("sns", region_name=region_name)

    stale_secrets: list[dict[str, Any]] = []
    checked = 0

    for secret_arn in secret_arns:
        try:
            age_days, secret_name = _check_secret_age(secretsmanager, secret_arn)
        except Exception as exc:
            _logger.warning(
                "credential_expiry_check_failed",
                secret_arn=secret_arn,
                error=str(exc),
            )
            continue

        checked += 1
        if age_days >= warning_threshold_days:
            stale_secrets.append({"secret_name": secret_name, "age_days": age_days})

    if stale_secrets:
        _publish_notification(sns, sns_topic_arn, environment, stale_secrets, secret_rotation_days)

    _logger.info(
        "credential_expiry_check_complete",
        environment=environment,
        checked=checked,
        stale=len(stale_secrets),
    )

    return {
        "checked": checked,
        "stale": len(stale_secrets),
        "stale_secret_names": [s["secret_name"] for s in stale_secrets],
    }


def _check_secret_age(secretsmanager: Any, secret_arn: str) -> tuple[int, str]:
    """
    Return (age_in_days, secret_name) for a secret.

    Age is measured from LastRotatedDate when the secret has ever been
    rotated, otherwise from CreatedDate. Never reads SecretString — only
    calls DescribeSecret, which returns metadata only.
    """
    response = secretsmanager.describe_secret(SecretId=secret_arn)
    reference_date = response.get("LastRotatedDate") or response["CreatedDate"]
    # Clamp to zero: a secret created moments ago must never appear "negative
    # days old" due to clock skew between this Lambda and Secrets Manager.
    age_days = max(0, (datetime.now(tz=UTC) - reference_date.astimezone(UTC)).days)
    return age_days, response["Name"]


def _publish_notification(
    sns: Any,
    topic_arn: str,
    environment: str,
    stale_secrets: list[dict[str, Any]],
    secret_rotation_days: int,
) -> None:
    """Publish an SNS alert listing every secret approaching or past rotation age."""
    lines = "\n".join(f"  - {s['secret_name']}: {s['age_days']} days old" for s in stale_secrets)
    message = (
        f"[{environment}] {len(stale_secrets)} credential secret(s) are approaching or "
        f"past the {secret_rotation_days}-day rotation window:\n{lines}\n\n"
        "Automatic rotation is not yet configured for these connectors — rotate "
        "manually per the operations runbook."
    )
    try:
        sns.publish(
            TopicArn=topic_arn,
            Subject=f"[{environment}] Credential rotation overdue — {len(stale_secrets)} secret(s)",
            Message=message,
            MessageAttributes={
                "environment": {"DataType": "String", "StringValue": environment},
                "alert_type": {"DataType": "String", "StringValue": "credential_expiry"},
            },
        )
    except Exception as exc:
        _logger.error(
            "credential_expiry_notification_publish_failed",
            environment=environment,
            error=str(exc),
        )
