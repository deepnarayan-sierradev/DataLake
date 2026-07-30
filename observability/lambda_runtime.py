"""
Shared Lambda utility helpers for the Enterprise Data Lake platform.

Centralises small cross-cutting concerns that every Lambda handler needs
so the same logic is not re-implemented in each handler module.
"""

from __future__ import annotations

import os
from typing import Any

from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)


def require_env(name: str) -> str:
    """
    Return the value of a required Lambda environment variable.

    Args:
        name: The environment variable name to look up.

    Returns:
        The variable's value (guaranteed non-empty).

    Raises:
        RuntimeError: When the variable is absent or set to an empty string.
    """
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(
            f"Required Lambda environment variable '{name}' is not set. "
            "Ensure the Lambda function is deployed with this variable configured."
        )
    return value


def check_lambda_timeout(context: Any, min_remaining_ms: int) -> None:
    """
    Raise RuntimeError early if insufficient Lambda execution time remains.

    Prevents starting an expensive pipeline operation (S3 reads, DynamoDB writes,
    source API calls) when the Lambda is about to be forcibly killed, which would
    leave no time to enqueue a DLQ entry or emit an audit record.

    This is a pre-execution guard only.  It cannot catch timeout mid-execution,
    but it prevents starting a run that is already doomed.

    Args:
        context:          Lambda context object (typed Any to avoid aws_lambda
                          dependency).  When None or lacking the runtime method
                          (e.g. local test runs), the check is skipped silently.
        min_remaining_ms: Minimum milliseconds required to proceed.

    Raises:
        RuntimeError: When remaining time is below the threshold.
    """
    if context is None or not hasattr(context, "get_remaining_time_in_millis"):
        return
    remaining = context.get_remaining_time_in_millis()
    if remaining < min_remaining_ms:
        raise RuntimeError(
            f"Insufficient Lambda execution time remaining ({remaining} ms). "
            f"Minimum required to start: {min_remaining_ms} ms. "
            "The run was not started — no partial state was written."
        )


def check_lambda_timeout_periodic(
    context: Any,
    min_remaining_ms: int,
    operation_name: str = "operation",
) -> None:
    """
    Check remaining Lambda time mid-execution and raise if below threshold.

    Unlike `check_lambda_timeout` (which is a pre-run guard), this function
    is designed to be called INSIDE long-running loops (DuckDB merge, batch
    S3 writes) to detect and handle timeouts before the Lambda runtime kills
    the process without allowing cleanup.

    Args:
        context:          Lambda context object.
        min_remaining_ms: Milliseconds required to safely abort (enqueue DLQ
                          entry, write partial audit record).
        operation_name:   Display name for the operation (included in the error
                          message for operator debugging).

    Raises:
        RuntimeError: When remaining execution time is below the threshold.
                      Callers should catch this and enqueue a DLQ entry before
                      re-raising to Step Functions.
    """
    if context is None or not hasattr(context, "get_remaining_time_in_millis"):
        return
    remaining = context.get_remaining_time_in_millis()
    if remaining < min_remaining_ms:
        raise RuntimeError(
            f"{operation_name}: insufficient Lambda time remaining ({remaining} ms < "
            f"{min_remaining_ms} ms). Aborting to allow cleanup before Lambda timeout."
        )


def configure_xray(
    tenant_code: str = "",
    source_id: str = "",
    entity_id: str = "",
    run_id: str = "",
) -> None:
    """
    Initialise AWS X-Ray SDK: patch all supported libraries and add annotations.

    Patches boto3, requests, and pymysql so their calls appear as subsegments.
    Adds tenant_code, source_id, entity_id, and run_id as X-Ray annotations
    for trace filtering.

    This function is a no-op if aws-xray-sdk is not importable (e.g. in unit
    tests without the SDK installed), so it never fails a Lambda invocation.

    Args:
        tenant_code: Tenant identifier for X-Ray annotation filtering.
        source_id:   Source identifier for X-Ray annotation filtering.
        entity_id:   Entity identifier for X-Ray annotation filtering.
        run_id:      Run identifier for X-Ray annotation filtering.
    """
    try:
        from aws_xray_sdk.core import patch_all, xray_recorder  # type: ignore[import-untyped]

        patch_all()
        if tenant_code:
            xray_recorder.put_annotation("tenant_code", tenant_code)
        if source_id:
            xray_recorder.put_annotation("source_id", source_id)
        if entity_id:
            xray_recorder.put_annotation("entity_id", entity_id)
        if run_id:
            xray_recorder.put_annotation("run_id", run_id)
    except ImportError:
        pass
    except Exception as exc:
        _logger.debug("xray_configuration_failed", error=str(exc))
