"""
Salesforce Bulk API 2.0 query job controller.

Manages the full lifecycle of a Salesforce Bulk API 2.0 query job:
  1. Pre-flight: check Salesforce API limits
  2. Create job with the parameterized SOQL query
  3. Poll job status with exponential backoff + jitter
  4. Fetch results in pages; validate record counts
  5. Yield ExtractionRecord per source row
  6. Close / abort the job on success or failure

Spec requirements satisfied:
  - Bulk API 2.0 only (never Bulk API 1.0)
  - Threshold: 2,000 records triggers bulk path (configured in ConnectorCapabilities)
  - API limits checked before job submission
  - Exponential backoff with jitter for polling (transient faults)
  - Timeout handling: expired job triggers controlled failure + DLQ entry
  - No hardcoded field lists — field order comes from the SOQL query
  - Record count validated: declared count from job status vs actual CSV rows

Security (OWASP A07, A09):
  - Token passed in Authorization header only; never in URL parameters or logs.
  - 401 responses trigger token invalidation and a single transparent retry.
  - Result CSV rows are never written to logs.
  - Job ID is not treated as secret but is included in structured log events.

Naming per spec: salesforce_bulk_query_job_controller
"""

from __future__ import annotations

import csv
import io
import random
import re
import time
from collections.abc import Iterator
from enum import StrEnum
from typing import Any, Final

import requests

from connector_runtime.adapters.salesforce.salesforce_auth_protocol import SalesforceAuthProtocol
from connector_runtime.interfaces.connector_interface import ExtractionRecord
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_API_VERSION: Final[str] = "v59.0"
_BULK_BASE_PATH: Final[str] = f"/services/data/{_API_VERSION}/jobs/query"
_LIMITS_PATH: Final[str] = f"/services/data/{_API_VERSION}/limits"

# ISO-8601 datetime pattern: values bound into SOQL must match this exactly.
# Any non-datetime value would indicate a programming error or injection attempt.
_ISO8601_DATETIME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?$"
)

# Polling configuration
_POLL_INITIAL_DELAY_S: Final[float] = 5.0
_POLL_MAX_DELAY_S: Final[float] = 60.0
_POLL_BACKOFF_FACTOR: Final[float] = 2.0
_POLL_JITTER_MAX_S: Final[float] = 3.0

# Result page size (max allowed by Salesforce Bulk API 2.0)
_RESULT_PAGE_SIZE: Final[int] = 50_000

# Minimum remaining API bulk query limit before aborting job submission.
# Salesforce allocates DailyBulkV2QueryFileStorageMB and DailyBulkApiBatches.
_MIN_BULK_QUERY_JOBS_REMAINING: Final[int] = 5


class BulkJobState(StrEnum):
    """Salesforce Bulk API 2.0 job state values."""

    UPLOAD_COMPLETE = "UploadComplete"
    IN_PROGRESS = "InProgress"
    ABORTED = "Aborted"
    JOB_COMPLETE = "JobComplete"
    FAILED = "Failed"


class BulkJobTimeoutError(Exception):
    """Raised when a Bulk API 2.0 job exceeds the allowed polling timeout."""


class BulkJobFailedError(Exception):
    """Raised when Salesforce marks the job as Failed or Aborted."""


class BulkApiLimitError(Exception):
    """Raised when the Salesforce API limit check blocks job submission."""


class SalesforceBulkQueryJobController:
    """
    Manages a Salesforce Bulk API 2.0 query job from creation to result retrieval.

    One instance per extraction run per entity.

    Usage::

        controller = SalesforceBulkQueryJobController(
            auth_client=auth,
            max_poll_seconds=1800,
        )
        records = controller.execute(
            soql="SELECT Id, Name FROM Account WHERE ...",
            query_parameters={"lower_bound": "...", "upper_bound": "..."},
        )
        for record in records:
            process(record)
    """

    def __init__(
        self,
        auth_client: SalesforceAuthProtocol,
        max_poll_seconds: float = 1800.0,
    ) -> None:
        self._auth = auth_client
        self._max_poll_seconds = max_poll_seconds
        self._current_job_id: str | None = None

    def execute(
        self,
        soql: str,
        query_parameters: dict[str, str],
    ) -> Iterator[ExtractionRecord]:
        """
        Execute a Bulk API 2.0 query job and yield ExtractionRecord per row.

        The SOQL query parameters are substituted into the query string before
        job submission.  Salesforce Bulk API 2.0 does not support server-side
        parameter binding — substitution is performed client-side on validated,
        typed values that originated from the watermark repository.

        Steps:
          1. Check API limits — abort if bulk job headroom is insufficient.
          2. Substitute query_parameters into the SOQL string (safe: values are
             ISO-8601 datetime strings from the watermark repository, never
             user-controlled free text).
          3. Create the Bulk API 2.0 query job.
          4. Poll until JobComplete or timeout.
          5. Fetch result pages; yield ExtractionRecord per CSV row.
          6. Validate actual row count against the declared count from Salesforce.
          7. Close job on success; abort job on any error path.

        Yields:
            ExtractionRecord for each source record.

        Raises:
            BulkApiLimitError: insufficient API limit headroom.
            BulkJobTimeoutError: job did not complete within max_poll_seconds.
            BulkJobFailedError: Salesforce reported job as Failed or Aborted.
            requests.HTTPError: on unexpected API errors.
        """
        self._check_api_limits(self._auth)
        bound_soql = self._bind_parameters(soql, query_parameters)
        job_id = self._create_job(self._auth, bound_soql)
        self._current_job_id = job_id

        try:
            declared_count = self._poll_until_complete(self._auth, job_id)
            actual_count = 0
            for record in self._fetch_results(self._auth, job_id):
                actual_count += 1
                yield record
            self._validate_record_count(job_id, declared_count, actual_count)
            self._close_job(self._auth, job_id)
        except Exception:
            self._abort_job_best_effort(self._auth, job_id)
            raise
        finally:
            self._current_job_id = None

    # ── 401-aware request helper ───────────────────────────────────────────────

    def _request_with_401_retry(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        """
        Make an HTTP request; on HTTP 401 invalidate the token and retry once.

        Token expiry mid-run is handled transparently without requiring a full
        extraction restart.  Only one retry is attempted — a second 401 indicates
        a persistent credential problem and should propagate as an error.
        """
        token = self._auth.get_access_token()
        headers: dict[str, str] = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {token}"

        response = requests.request(method, url, headers=headers, **kwargs)  # noqa: S113
        if response.status_code == 401:
            _logger.warning(
                "salesforce_token_expired_mid_request",
                url=url,
                method=method,
            )
            self._auth.invalidate_token()
            token = self._auth.get_access_token()
            headers["Authorization"] = f"Bearer {token}"
            response = requests.request(method, url, headers=headers, **kwargs)  # noqa: S113
        return response

    # ── API limit pre-flight ───────────────────────────────────────────────────

    def _check_api_limits(self, auth: SalesforceAuthProtocol) -> None:
        """
        Verify sufficient Bulk API 2.0 quota before job submission.

        Raises BulkApiLimitError if fewer than _MIN_BULK_QUERY_JOBS_REMAINING
        jobs are available in the DailyBulkApiBatches limit.
        """
        url = f"{auth.instance_url}{_LIMITS_PATH}"
        response = self._request_with_401_retry(
            "GET",
            url,
            headers={"Accept": "application/json"},
            timeout=15,
        )
        response.raise_for_status()
        limits: dict[str, Any] = response.json()

        bulk_limit = limits.get("DailyBulkApiBatches", {})
        remaining: int = int(bulk_limit.get("Remaining", 0))

        if remaining < _MIN_BULK_QUERY_JOBS_REMAINING:
            raise BulkApiLimitError(
                f"Salesforce DailyBulkApiBatches remaining={remaining}, "
                f"minimum required={_MIN_BULK_QUERY_JOBS_REMAINING}. "
                "Job submission aborted to preserve API quota."
            )

        _logger.info(
            "salesforce_api_limit_checked",
            bulk_api_batches_remaining=remaining,
        )

    # ── Job creation ───────────────────────────────────────────────────────────

    def _create_job(self, auth: SalesforceAuthProtocol, soql: str) -> str:
        """
        Create a Bulk API 2.0 query job.

        Returns the job ID.
        """
        url = f"{auth.instance_url}{_BULK_BASE_PATH}"
        response = self._request_with_401_retry(
            "POST",
            url,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={"operation": "query", "query": soql},
            timeout=30,
        )
        response.raise_for_status()
        job_id: str = response.json()["id"]

        _logger.info(
            "salesforce_bulk_job_created",
            job_id=job_id,
        )
        return job_id

    # ── Polling ────────────────────────────────────────────────────────────────

    def _poll_until_complete(self, auth: SalesforceAuthProtocol, job_id: str) -> int:
        """
        Poll job status until JobComplete, Failed, or Aborted.

        Uses exponential backoff with jitter to reduce API call volume during
        long-running jobs.

        Returns:
            numberRecordsProcessed as declared by Salesforce in the final status.

        Raises:
            BulkJobTimeoutError: if max_poll_seconds elapses before completion.
            BulkJobFailedError: if Salesforce reports Failed or Aborted.
        """
        deadline = time.monotonic() + self._max_poll_seconds
        delay = _POLL_INITIAL_DELAY_S

        while True:
            if time.monotonic() > deadline:
                raise BulkJobTimeoutError(
                    f"Bulk job {job_id!r} did not complete within "
                    f"{self._max_poll_seconds:.0f} seconds."
                )

            url = f"{auth.instance_url}{_BULK_BASE_PATH}/{job_id}"
            response = self._request_with_401_retry(
                "GET",
                url,
                headers={"Accept": "application/json"},
                timeout=15,
            )
            response.raise_for_status()
            body = response.json()
            status: str = body.get("state", "")
            records_processed: int = int(body.get("numberRecordsProcessed", 0))

            _logger.info(
                "salesforce_bulk_job_polled",
                job_id=job_id,
                state=status,
                records_processed=records_processed,
            )

            if status == BulkJobState.JOB_COMPLETE:
                return records_processed
            if status in (BulkJobState.FAILED, BulkJobState.ABORTED):
                raise BulkJobFailedError(f"Bulk job {job_id!r} terminated with state={status!r}.")

            # Exponential backoff with full jitter
            jitter = random.uniform(0, _POLL_JITTER_MAX_S)  # noqa: S311 — jitter, not crypto
            time.sleep(min(delay + jitter, _POLL_MAX_DELAY_S))
            delay = min(delay * _POLL_BACKOFF_FACTOR, _POLL_MAX_DELAY_S)

    # ── Result retrieval ───────────────────────────────────────────────────────

    def _fetch_results(
        self, auth: SalesforceAuthProtocol, job_id: str
    ) -> Iterator[ExtractionRecord]:
        """
        Fetch paginated CSV results and yield one ExtractionRecord per row.

        Salesforce Bulk API 2.0 returns results as RFC 4180 CSV.  Each page
        is up to _RESULT_PAGE_SIZE rows.  The locator token from each response
        is used to fetch the next page.
        """
        locator: str | None = None
        page = 0

        while True:
            url = f"{auth.instance_url}{_BULK_BASE_PATH}/{job_id}/results"
            params: dict[str, str | int] = {"maxRecords": _RESULT_PAGE_SIZE}
            if locator:
                params["locator"] = locator

            response = self._request_with_401_retry(
                "GET",
                url,
                headers={"Accept": "text/csv"},
                params=params,
                timeout=120,
            )
            response.raise_for_status()

            page += 1
            next_locator = response.headers.get("Sforce-Locator", "null")
            row_count = 0

            reader = csv.DictReader(io.StringIO(response.text))
            for row in reader:
                row_count += 1
                yield ExtractionRecord(payload=dict(row))

            _logger.info(
                "salesforce_bulk_result_page_fetched",
                job_id=job_id,
                page=page,
                row_count=row_count,
            )

            if next_locator in ("", "null", None):
                break
            locator = next_locator

    # ── Record count validation ───────────────────────────────────────────────────

    @staticmethod
    def _validate_record_count(job_id: str, declared: int, actual: int) -> None:
        """
        Warn when actual CSV rows differ from the count declared by Salesforce.

        A mismatch does not abort the job — the records yielded are the ground
        truth.  The warning is emitted as a structured log event for downstream
        alerting and audit.
        """
        if declared != actual:
            _logger.warning(
                "salesforce_bulk_record_count_mismatch",
                job_id=job_id,
                declared_count=declared,
                actual_count=actual,
                delta=actual - declared,
            )

    # ── Job close / abort ─────────────────────────────────────────────────────

    def _close_job(self, auth: SalesforceAuthProtocol, job_id: str) -> None:
        """Mark the job as closed after successful result retrieval."""
        url = f"{auth.instance_url}{_BULK_BASE_PATH}/{job_id}"
        try:
            self._request_with_401_retry(
                "PATCH",
                url,
                headers={"Content-Type": "application/json"},
                json={"state": "Closed"},
                timeout=15,
            ).raise_for_status()
        except requests.RequestException:
            _logger.warning("salesforce_bulk_job_close_failed", job_id=job_id)

    def _abort_job_best_effort(self, auth: SalesforceAuthProtocol, job_id: str) -> None:
        """Attempt to abort the job; log on failure but never propagate."""
        try:
            url = f"{auth.instance_url}{_BULK_BASE_PATH}/{job_id}"
            self._request_with_401_retry(
                "PATCH",
                url,
                headers={"Content-Type": "application/json"},
                json={"state": "Aborted"},
                timeout=15,
            )
        except Exception:
            _logger.warning("salesforce_bulk_job_abort_failed", job_id=job_id)

    # ── Parameter binding ─────────────────────────────────────────────────────

    @staticmethod
    def _bind_parameters(soql: str, parameters: dict[str, str]) -> str:
        """
        Substitute named :param placeholders with ISO-8601 datetime literals.

        Salesforce Bulk API 2.0 does not support server-side parameter binding.
        This substitution is safe because:
          - All parameter values are validated as ISO-8601 datetime strings
            before substitution.  Any other format raises ValueError immediately,
            preventing injection of arbitrary SOQL fragments.
          - Values originate exclusively from the watermark repository (typed
            datetimes serialised to ISO-8601 strings), never from user input.
          - Substitution replaces :param_name tokens; no shell or SQL metachar
            injection is possible in a SOQL WHERE clause with datetime values.

        Raises:
            ValueError: if any parameter value is empty or not a valid ISO-8601
                        datetime string (possible injection attempt).
        """
        bound = soql
        for name, value in parameters.items():
            if not value:
                raise ValueError(f"Query parameter {name!r} is empty — cannot bind into SOQL.")
            if not _ISO8601_DATETIME_PATTERN.match(value):
                raise ValueError(
                    f"SOQL parameter {name!r} value {value!r} is not a valid ISO-8601 "
                    "datetime string.  Only watermark datetime values may be substituted "
                    "into SOQL queries.  Possible injection attempt — aborting job submission."
                )
            bound = bound.replace(f":{name}", value)
        return bound
