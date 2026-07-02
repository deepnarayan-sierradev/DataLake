"""
Shared Lambda utility helpers for the Enterprise Data Lake platform.

Centralises small cross-cutting concerns that every Lambda handler needs
so the same logic is not re-implemented in each handler module.
"""

from __future__ import annotations

import os
from typing import Any


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
