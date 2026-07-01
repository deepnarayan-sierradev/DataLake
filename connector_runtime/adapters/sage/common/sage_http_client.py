"""
SageHttpClient — shared HTTP client with typed errors for all Sage product adapters.

All Sage REST API HTTP calls flow through this client.  It provides:
  - Consistent timeout enforcement across all product adapters.
  - Typed exception hierarchy so SageConnector.classify_extraction_error()
    can map failures to the platform's ExtractionErrorClassification taxonomy
    without any product-specific branching in the classifier.
  - A single location for setting TLS verification (always True — OWASP A05).
  - Separation of transport concerns from business logic in each adapter.

Design note: retry logic (exponential backoff) is intentionally NOT implemented
here.  The platform reliability framework (ExtractionRetryPolicy + Step Functions)
handles retries at the orchestration level.  This client raises typed errors;
the connector classifies them; Step Functions retries on TRANSIENT classifications.

Security (OWASP A05):
  - TLS verification is always enabled; there is no verify=False code path.
  - Authorization header value is accepted as an opaque string and forwarded
    as-is — it is never logged or written to storage by this client.
"""

from __future__ import annotations

from typing import Any, Final

import requests
from requests import Response

from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# Default timeout for all Sage REST API calls.
# Intacct documentation recommends 30–60 seconds; 45 s is a conservative middle ground.
_DEFAULT_TIMEOUT_SECONDS: Final[int] = 45


# ---------------------------------------------------------------------------
# Typed exception hierarchy
# ---------------------------------------------------------------------------


class SageHttpError(Exception):
    """Base class for all SageHttpClient errors."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SageAuthenticationError(SageHttpError):
    """HTTP 401 / 403 — credentials are invalid or the token has expired."""


class SageObjectNotFoundError(SageHttpError):
    """HTTP 404 — the requested Sage object path does not exist."""


class SageRateLimitError(SageHttpError):
    """HTTP 429 — per-minute or per-hour request rate limit exceeded."""


class SageServiceUnavailableError(SageHttpError):
    """HTTP 503 / 5xx — Sage service is temporarily unavailable."""


class SageInvalidRequestError(SageHttpError):
    """HTTP 400 — the request body is malformed or contains invalid parameters."""


class SageTimeoutError(SageHttpError):
    """Request timed out before the Sage service responded."""


class SageNetworkError(SageHttpError):
    """Network-level connectivity error (DNS failure, connection refused, etc.)."""


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class SageHttpClient:
    """
    Thin HTTP client that wraps requests.Session for all Sage REST API calls.

    Maps HTTP status codes and transport exceptions to typed errors so that
    callers never need to inspect raw status codes or exception types directly.

    One instance can be shared across all components within an extraction run.

    Usage::

        client = SageHttpClient()
        response_body = client.get(
            url="https://api.intacct.com/ia/api/v1/objects/accounts-receivable/customer",
            headers={"Authorization": "Bearer ..."},
        )
        # → dict parsed from JSON response body
    """

    def __init__(self, timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout_seconds
        self._session = requests.Session()
        # TLS verification always on — no way to disable it (OWASP A05).
        self._session.verify = True

    def get(
        self,
        url: str,
        headers: dict[str, str],
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Execute a GET request and return the parsed JSON response body.

        Args:
            url:     Full request URL.
            headers: Request headers (must include Authorization).
            params:  Optional query string parameters.

        Returns:
            Parsed JSON response body as a dict.

        Raises:
            SageAuthenticationError: HTTP 401 or 403.
            SageObjectNotFoundError: HTTP 404.
            SageRateLimitError:      HTTP 429.
            SageServiceUnavailableError: HTTP 5xx.
            SageInvalidRequestError: HTTP 400.
            SageTimeoutError:        Request timed out.
            SageNetworkError:        Connectivity failure.
        """
        try:
            response = self._session.get(
                url,
                headers=headers,
                params=params,
                timeout=self._timeout,
            )
        except requests.Timeout as exc:
            raise SageTimeoutError(
                f"GET request timed out after {self._timeout}s: {type(exc).__name__}"
            ) from None
        except requests.ConnectionError as exc:
            raise SageNetworkError(
                f"GET request failed with a connection error: {type(exc).__name__}"
            ) from None

        return self._parse_response(response)

    def post(
        self,
        url: str,
        headers: dict[str, str],
        json_body: dict[str, Any] | list[Any],
    ) -> dict[str, Any]:
        """
        Execute a POST request with a JSON body and return the parsed JSON response.

        Args:
            url:       Full request URL.
            headers:   Request headers (must include Authorization and Content-Type).
            json_body: Request body — serialised to JSON internally.

        Returns:
            Parsed JSON response body as a dict.

        Raises same typed errors as get().
        """
        try:
            response = self._session.post(
                url,
                headers=headers,
                json=json_body,
                timeout=self._timeout,
            )
        except requests.Timeout as exc:
            raise SageTimeoutError(
                f"POST request timed out after {self._timeout}s: {type(exc).__name__}"
            ) from None
        except requests.ConnectionError as exc:
            raise SageNetworkError(
                f"POST request failed with a connection error: {type(exc).__name__}"
            ) from None

        return self._parse_response(response)

    def post_form(
        self,
        url: str,
        headers: dict[str, str],
        form_data: dict[str, str],
    ) -> dict[str, Any]:
        """
        Execute a POST request with form-encoded body (used for OAuth token exchange).

        Args:
            url:       Full request URL (token endpoint).
            headers:   Request headers.
            form_data: Form fields — serialised as application/x-www-form-urlencoded.

        Returns:
            Parsed JSON response body as a dict.
        """
        try:
            response = self._session.post(
                url,
                headers=headers,
                data=form_data,
                timeout=self._timeout,
            )
        except requests.Timeout as exc:
            raise SageTimeoutError(
                f"Token request timed out after {self._timeout}s: {type(exc).__name__}"
            ) from None
        except requests.ConnectionError as exc:
            raise SageNetworkError(
                f"Token request failed with a connection error: {type(exc).__name__}"
            ) from None

        return self._parse_response(response)

    # ── Private ────────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_response(response: Response) -> dict[str, Any]:
        """
        Validate the HTTP status and return the parsed JSON body.

        Status codes are mapped to typed exceptions here so that callers
        never need to inspect raw status codes.  Error response bodies are
        NOT logged — they may contain PII or diagnostic info that should not
        appear in CloudWatch (OWASP A09).

        Raises typed SageHttpError subclasses on non-2xx responses.
        """
        status = response.status_code

        if status == 400:
            raise SageInvalidRequestError(
                "Sage API returned HTTP 400 Bad Request. "
                "Check the query body or object path for invalid parameters.",
                status_code=status,
            )
        if status in (401, 403):
            raise SageAuthenticationError(
                f"Sage API returned HTTP {status}. "
                "The access token may be expired or the credentials lack permission.",
                status_code=status,
            )
        if status == 404:
            raise SageObjectNotFoundError(
                "Sage API returned HTTP 404. "
                "The requested object path does not exist in this Sage environment.",
                status_code=status,
            )
        if status == 429:
            # Surface the Retry-After value so operators can tune Step Functions
            # retry intervals.  The value is only logged — never used to sleep
            # (retry timing is the orchestration layer's responsibility).
            retry_after = response.headers.get("Retry-After")
            _logger.info(
                "sage_rate_limit_exceeded",
                retry_after_seconds=retry_after,
            )
            raise SageRateLimitError(
                "Sage API returned HTTP 429 Too Many Requests. "
                "The per-minute or per-hour rate limit has been exceeded."
                + (f" Retry-After: {retry_after}s." if retry_after else ""),
                status_code=status,
            )
        if status >= 500:
            raise SageServiceUnavailableError(
                f"Sage API returned HTTP {status} server error. "
                "The service is temporarily unavailable.",
                status_code=status,
            )
        if not response.ok:
            raise SageHttpError(
                f"Sage API returned unexpected HTTP {status}.",
                status_code=status,
            )

        try:
            return response.json()  # type: ignore[no-any-return]
        except ValueError as exc:
            raise SageHttpError(
                f"Sage API returned HTTP {status} but the response body is not valid JSON: "
                f"{type(exc).__name__}"
            ) from None
