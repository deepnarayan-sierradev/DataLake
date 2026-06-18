"""
Salesforce OAuth 2.0 authentication client.

Retrieves credentials exclusively from AWS Secrets Manager and manages a
short-lived access token with proactive refresh.  The token is NEVER logged,
persisted to disk, or included in exception messages.

Security requirements enforced here (OWASP A07, A09):
  - Client credentials retrieved from Secrets Manager only — never from env
    vars, constructor arguments, or config files.
  - Access token scrubbed from all exception paths before re-raising.
  - Token stored only in-memory; never written to S3, DynamoDB, or logs.
  - Proactive refresh window (default 5 min) prevents mid-run token expiry.

AWS resource used:
  - Secrets Manager secret: {environment}/sources/salesforce/credentials
    Expected JSON keys: instance_url, client_id, client_secret

Token endpoint:
  POST {instance_url}/services/oauth2/token
  grant_type=client_credentials
  client_id={client_id}
  client_secret={client_secret}
"""

from __future__ import annotations

import json
import time
from typing import Any, Final

import boto3
import requests
from botocore.exceptions import ClientError

from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

_TOKEN_URL_PATH: Final[str] = "/services/oauth2/token"  # noqa: S105
_SECRET_PATH_TEMPLATE: Final[str] = "{environment}/sources/salesforce/credentials"  # noqa: S105

# Refresh the token this many seconds before it actually expires to avoid
# mid-extraction expiry on long-running Bulk API jobs.
_PROACTIVE_REFRESH_SECONDS: Final[int] = 300


class SalesforceCredentialError(Exception):
    """Raised when Salesforce credentials cannot be retrieved or are invalid."""


class SalesforceAuthError(Exception):
    """Raised when the OAuth 2.0 token exchange fails."""


class SalesforceAuthClient:
    """
    Manages Salesforce OAuth 2.0 client-credentials token lifecycle.

    One instance per extraction run.  The token is refreshed proactively
    when fewer than _PROACTIVE_REFRESH_SECONDS remain before expiry.

    Usage::

        auth = SalesforceAuthClient(environment="dev", region_name="us-east-1")
        token = auth.get_access_token()   # fetches or returns cached token
        instance_url = auth.instance_url  # resolved after first token fetch
    """

    def __init__(self, environment: str, region_name: str) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        self._environment = environment
        self._region = region_name
        self._secrets_client = boto3.client("secretsmanager", region_name=region_name)

        # Token state — populated lazily on first get_access_token() call.
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0  # UNIX epoch seconds
        self._instance_url: str | None = None

    @property
    def instance_url(self) -> str:
        """
        Salesforce instance URL (e.g. https://myorg.my.salesforce.com).
        Available after the first successful get_access_token() call.
        """
        if self._instance_url is None:
            raise RuntimeError("instance_url is not available until get_access_token() succeeds.")
        return self._instance_url

    def get_access_token(self) -> str:
        """
        Return a valid Salesforce access token.

        Fetches a new token on first call and when the existing token is within
        _PROACTIVE_REFRESH_SECONDS of expiry.  The token is NEVER logged.

        Raises:
            SalesforceCredentialError: credentials cannot be read from Secrets Manager.
            SalesforceAuthError: Salesforce token endpoint rejects the request.
        """
        if self._access_token is not None and self._is_token_valid():
            return self._access_token
        self._refresh_token()
        if self._access_token is None:
            raise SalesforceAuthError(
                "Token refresh completed but access_token was not populated. "
                "This is an internal error — inspect _refresh_token for the unset code path."
            )
        return self._access_token

    def invalidate_token(self) -> None:
        """
        Force the next get_access_token() call to fetch a fresh token.

        Call this when a Salesforce API returns HTTP 401 to recover without
        restarting the extraction run.
        """
        self._access_token = None
        self._token_expires_at = 0.0

    # ── Private ────────────────────────────────────────────────────────────────

    def _is_token_valid(self) -> bool:
        """True when the cached token has more than the proactive refresh window remaining.

        Uses time.time() (wall clock) so that token TTL is measured in real
        elapsed seconds, not CPU execution time.  This ensures correct expiry
        detection even when the process is idle between Lambda invocations.
        """
        return (self._token_expires_at - time.time()) > _PROACTIVE_REFRESH_SECONDS

    def _refresh_token(self) -> None:
        """Fetch a fresh token from the Salesforce token endpoint."""
        credentials = self._load_credentials()
        instance_url: str = credentials["instance_url"]
        client_id: str = credentials["client_id"]
        client_secret: str = credentials["client_secret"]

        token_url = f"{instance_url.rstrip('/')}{_TOKEN_URL_PATH}"
        try:
            # Use a form-encoded body — Salesforce OAuth 2.0 client_credentials flow.
            # client_secret is passed as form data, never as a query parameter.
            response = requests.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={"Accept": "application/json"},
                timeout=30,
            )
        except requests.RequestException as exc:
            # Scrub before re-raise — the URL may contain instance domain; the
            # exception str must not expose any credential fragments.
            raise SalesforceAuthError(
                f"Token request to Salesforce failed: {type(exc).__name__}"
            ) from None

        if not response.ok:
            # Never include response body — it may contain error_description
            # with credential hints.
            raise SalesforceAuthError(
                f"Salesforce token endpoint returned HTTP {response.status_code}."
            )

        body: dict[str, Any] = response.json()

        # Salesforce client_credentials returns access_token and instance_url.
        # expires_in is not always present; default to 7200 s (Salesforce default).
        access_token: str = body["access_token"]
        expires_in: int = int(body.get("expires_in", 7200))

        self._instance_url = body.get("instance_url", instance_url)
        self._access_token = access_token
        # Use wall clock (time.time) not monotonic — token expiry is a real-time
        # concept and must survive Lambda idle periods correctly.
        self._token_expires_at = time.time() + expires_in

        _logger.info(
            "salesforce_token_refreshed",
            environment=self._environment,
            expires_in_seconds=expires_in,
            # token value intentionally omitted
        )

    def _load_credentials(self) -> dict[str, str]:
        """
        Load Salesforce credentials from AWS Secrets Manager.

        Secret JSON must contain: instance_url, client_id, client_secret.

        Raises:
            SalesforceCredentialError: secret not found or malformed.
        """
        secret_id = _SECRET_PATH_TEMPLATE.format(environment=self._environment)
        try:
            response = self._secrets_client.get_secret_value(SecretId=secret_id)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            raise SalesforceCredentialError(
                f"Failed to retrieve Salesforce credentials from Secrets Manager "
                f"(secret={secret_id!r}, code={code!r})."
            ) from None

        raw = response.get("SecretString") or ""
        try:
            payload: dict[str, str] = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise SalesforceCredentialError(
                f"Salesforce credentials secret is not valid JSON (secret={secret_id!r})."
            ) from exc

        missing = [k for k in ("instance_url", "client_id", "client_secret") if k not in payload]
        if missing:
            raise SalesforceCredentialError(
                f"Salesforce credentials secret is missing required keys: {missing}."
            )
        return payload
