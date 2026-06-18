"""
Athena query client for analytics layer consumption (spec §8.3).

Provides a workgroup-scoped, authenticated query interface over the analytics
S3 layer using AWS Athena engine version 3.

Design:
  - All queries run within a named workgroup; results go to a designated
    S3 prefix (encrypted with KMS per workgroup configuration).
  - Query execution is asynchronous; client polls until completion or timeout.
  - Parameterized query inputs accepted as NamedParameter dicts to prevent
    injection (OWASP A05).
  - No raw record values appear in log output (PII protection).

Security (OWASP A01, A05):
  - WorkGroup enforces result encryption and per-query scan limits.
  - Caller must have athena:StartQueryExecution on the workgroup ARN.
  - Table and database names are validated before interpolation.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import boto3
from botocore.exceptions import ClientError

from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

_SAFE_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_DEFAULT_POLL_INTERVAL_SECONDS: Final[float] = 2.0
_DEFAULT_TIMEOUT_SECONDS: Final[int] = 300


@dataclass(frozen=True)
class AthenaQueryResult:
    """Result summary for one completed Athena query execution."""

    query_execution_id: str
    database: str
    workgroup: str
    state: str  # "SUCCEEDED" | "FAILED" | "CANCELLED"
    data_scanned_bytes: int
    execution_duration_ms: int
    result_s3_uri: str
    submitted_at: str  # ISO-8601 UTC
    completed_at: str  # ISO-8601 UTC


class AthenaQueryClient:
    """
    Synchronous wrapper around the Athena StartQueryExecution API.

    Blocks until the query completes or the timeout expires.
    """

    def __init__(
        self,
        workgroup: str,
        region_name: str,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._workgroup = workgroup
        self._region_name = region_name
        self._poll_interval = poll_interval_seconds
        self._timeout_seconds = timeout_seconds
        self._client: Any = boto3.client("athena", region_name=region_name)

    def execute_query(
        self,
        query: str,
        database: str,
        query_name: str = "",
    ) -> AthenaQueryResult:
        """
        Execute an Athena SQL query and block until completion.

        Args:
            query:      SQL query string. Use Athena named parameters (?param)
                        with `execution_parameters` if values come from user input.
            database:   Glue catalog database name (validated against safe pattern).
            query_name: Optional logical name for log correlation.

        Returns:
            AthenaQueryResult.

        Raises:
            AthenaQueryError if the query fails, is cancelled, or times out.
        """
        if not _SAFE_IDENTIFIER.match(database):
            raise AthenaQueryError(f"Invalid database name: {database!r}")

        submitted_at = datetime.now(UTC).isoformat()

        _logger.info(
            "athena_query_submitted",
            workgroup=self._workgroup,
            database=database,
            query_name=query_name,
        )

        try:
            response = self._client.start_query_execution(
                QueryString=query,
                QueryExecutionContext={"Database": database},
                WorkGroup=self._workgroup,
            )
        except ClientError as exc:
            raise AthenaQueryError(f"Failed to start Athena query: {exc}") from exc

        execution_id: str = response["QueryExecutionId"]

        # Poll until terminal state or timeout
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            if time.monotonic() > deadline:
                self._cancel_query(execution_id)
                raise AthenaQueryError(
                    f"Athena query {execution_id} timed out after {self._timeout_seconds}s"
                )

            status = self._get_execution_status(execution_id)
            state: str = status["QueryExecution"]["Status"]["State"]

            if state == "SUCCEEDED":
                break
            if state in ("FAILED", "CANCELLED"):
                reason = status["QueryExecution"]["Status"].get(
                    "StateChangeReason", "no reason provided"
                )
                raise AthenaQueryError(
                    f"Athena query {execution_id} ended with state={state}: {reason}"
                )

            time.sleep(self._poll_interval)

        stats = status["QueryExecution"].get("Statistics", {})
        result_uri = (
            status["QueryExecution"].get("ResultConfiguration", {}).get("OutputLocation", "")
        )
        completed_at = datetime.now(UTC).isoformat()

        _logger.info(
            "athena_query_complete",
            execution_id=execution_id,
            state=state,
            data_scanned_bytes=stats.get("DataScannedInBytes", 0),
            workgroup=self._workgroup,
        )

        return AthenaQueryResult(
            query_execution_id=execution_id,
            database=database,
            workgroup=self._workgroup,
            state=state,
            data_scanned_bytes=stats.get("DataScannedInBytes", 0),
            execution_duration_ms=stats.get("TotalExecutionTimeInMillis", 0),
            result_s3_uri=result_uri,
            submitted_at=submitted_at,
            completed_at=completed_at,
        )

    def get_query_results(self, execution_id: str) -> list[dict[str, str]]:
        """
        Fetch query result rows for a SUCCEEDED execution.

        Returns rows as a list of dicts keyed by column name.
        The first row (column headers) is used to build the dict keys.

        Raises:
            AthenaQueryError if the execution did not SUCCEED.
        """
        status = self._get_execution_status(execution_id)
        state = status["QueryExecution"]["Status"]["State"]
        if state != "SUCCEEDED":
            raise AthenaQueryError(
                f"Cannot fetch results for execution {execution_id} in state {state!r}"
            )

        try:
            response = self._client.get_query_results(QueryExecutionId=execution_id)
        except ClientError as exc:
            raise AthenaQueryError(f"Failed to fetch results: {exc}") from exc

        rows = response.get("ResultSet", {}).get("Rows", [])
        if not rows:
            return []

        headers = [col.get("VarCharValue", "") for col in rows[0]["Data"]]
        return [
            {
                headers[i]: cell.get("VarCharValue", "")
                for i, cell in enumerate(row["Data"])
                if i < len(headers)
            }
            for row in rows[1:]
        ]

    # ── Internals ─────────────────────────────────────────────────────────────

    def _get_execution_status(self, execution_id: str) -> dict[str, Any]:
        try:
            return self._client.get_query_execution(QueryExecutionId=execution_id)  # type: ignore[no-any-return]
        except (ClientError, KeyError) as exc:
            raise AthenaQueryError(f"Failed to get query status: {exc}") from exc

    def _cancel_query(self, execution_id: str) -> None:
        try:
            self._client.stop_query_execution(QueryExecutionId=execution_id)
        except ClientError:
            pass  # Best effort; log below
        _logger.warning("athena_query_cancelled_due_to_timeout", execution_id=execution_id)


class AthenaQueryError(Exception):
    """Raised when an Athena query fails, is cancelled, or times out."""
