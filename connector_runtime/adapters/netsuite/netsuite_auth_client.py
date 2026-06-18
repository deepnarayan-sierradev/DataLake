"""
NetSuite Token-Based Authentication (TBA) client.

Implements OAuth 1.0a HMAC-SHA256 request signing for NetSuite's REST APIs
(SuiteAnalytics Connect / SuiteQL and the Record Metadata Catalog).

Credential storage:
  - Credentials are retrieved exclusively from AWS Secrets Manager.
  - Secret path: {environment}/sources/netsuite/credentials
  - Expected JSON keys: account_id, consumer_key, consumer_secret,
    token_id, token_secret

TBA is stateless — each HTTP request receives a freshly computed signature.
There is no token to cache or refresh; the only network I/O is the one-time
Secrets Manager fetch on first use.

Security (OWASP A07, A09):
  - Credential values are never logged or included in exception messages.
  - HMAC-SHA256 signatures computed in-memory; not persisted anywhere.
  - token_secret and consumer_secret absent from all log events.
  - Secrets Manager call uses IAM role credentials (boto3 implicit chain).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import uuid
from typing import Any, Final

import boto3
from botocore.exceptions import ClientError

from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

_SECRET_PATH_TEMPLATE: Final[str] = "{environment}/sources/netsuite/credentials"  # noqa: S105
_OAUTH_VERSION: Final[str] = "1.0"
_SIGNATURE_METHOD: Final[str] = "HMAC-SHA256"
# NetSuite TBA credentials do not expire automatically, but we still re-fetch
# periodically so that automatic rotation (FINDING-02) takes effect within
# one hour without requiring a Lambda restart (OWASP A07).
_CREDENTIAL_CACHE_TTL_SECONDS: Final[int] = 3_600


class NetSuiteCredentialError(Exception):
    """Raised when NetSuite credentials cannot be retrieved from Secrets Manager."""


class NetSuiteAuthError(Exception):
    """Raised when an OAuth 1.0a signature cannot be generated."""


class NetSuiteAuthClient:
    """
    Generates per-request OAuth 1.0a TBA Authorization headers for NetSuite.

    One instance can be shared across all requests within an extraction run.
    Credentials are loaded lazily on the first get_auth_headers() call and
    cached in-memory for the lifetime of the instance.

    Usage::

        auth = NetSuiteAuthClient(environment="dev", region_name="us-east-1")
        headers = auth.get_auth_headers("POST", "https://1234567.suitetalk.api.netsuite.com/...")
        # → {"Authorization": "OAuth realm=\\"1234567\\", oauth_consumer_key=\\"...\\", ..."}
    """

    def __init__(self, environment: str, region_name: str) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        self._environment = environment
        self._region = region_name
        self._secrets_client = boto3.client("secretsmanager", region_name=region_name)

        # Credentials — loaded lazily; never logged.
        self._account_id: str | None = None
        self._consumer_key: str | None = None
        self._consumer_secret: str | None = None
        self._token_id: str | None = None
        self._token_secret: str | None = None
        self._credentials_loaded_at: float = 0.0  # monotonic timestamp of last successful fetch

    @property
    def account_id(self) -> str:
        """
        NetSuite account ID.  Available after first get_auth_headers() call.
        """
        if self._credentials_expired():
            self._load_credentials()
        assert self._account_id is not None  # noqa: S101
        return self._account_id

    def get_auth_headers(self, method: str, url: str) -> dict[str, str]:
        """
        Compute and return a signed OAuth 1.0a Authorization header.

        Each call generates a unique nonce and current timestamp, producing
        a fresh signature regardless of how recently the previous call was made.

        Args:
            method: HTTP verb in upper-case (GET, POST, …).
            url: The full request URL.  Query string parameters are included in
                 the signature base string automatically.

        Returns:
            Dict {"Authorization": "<signed oauth header>"}.

        Raises:
            NetSuiteCredentialError: credentials absent from Secrets Manager.
            NetSuiteAuthError: signature computation fails unexpectedly.
        """
        if self._credentials_expired():
            self._load_credentials()

        # Mypy narrowing — _load_credentials guarantees these are set.
        consumer_key: str = self._consumer_key  # type: ignore[assignment]
        consumer_secret: str = self._consumer_secret  # type: ignore[assignment]
        token_id: str = self._token_id  # type: ignore[assignment]
        token_secret: str = self._token_secret  # type: ignore[assignment]
        account_id: str = self._account_id  # type: ignore[assignment]

        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex

        oauth_params: dict[str, str] = {
            "oauth_consumer_key": consumer_key,
            "oauth_nonce": nonce,
            "oauth_signature_method": _SIGNATURE_METHOD,
            "oauth_timestamp": timestamp,
            "oauth_token": token_id,
            "oauth_version": _OAUTH_VERSION,
        }

        signature = self._compute_signature(
            method=method.upper(),
            url=url,
            oauth_params=oauth_params,
            consumer_secret=consumer_secret,
            token_secret=token_secret,
        )

        # Build the Authorization header value with realm first.
        auth_parts = [f'realm="{account_id}"']
        auth_parts.extend(
            f'{k}="{urllib.parse.quote(v, safe="")}"' for k, v in sorted(oauth_params.items())
        )
        auth_parts.append(f'oauth_signature="{urllib.parse.quote(signature, safe="")}"')

        return {"Authorization": f"OAuth {', '.join(auth_parts)}"}

    # ── Private ────────────────────────────────────────────────────────────────

    def _credentials_expired(self) -> bool:
        """True when credentials are not yet loaded or have exceeded the TTL."""
        return (
            self._account_id is None
            or (time.monotonic() - self._credentials_loaded_at) >= _CREDENTIAL_CACHE_TTL_SECONDS
        )

    @staticmethod
    def _compute_signature(
        method: str,
        url: str,
        oauth_params: dict[str, str],
        consumer_secret: str,
        token_secret: str,
    ) -> str:
        """
        Compute the OAuth 1.0a HMAC-SHA256 signature.

        Follows the OAuth 1.0a spec (RFC 5849):
          1. Parse query string from URL and merge with oauth_params.
          2. Percent-encode and sort parameters alphabetically.
          3. Build the signature base string.
          4. Build the signing key from consumer_secret & token_secret.
          5. HMAC-SHA256 sign and base64-encode.

        Credential values (consumer_secret, token_secret) are never logged
        or included in any exception message.
        """
        # 1. Collect all parameters (URL query + OAuth).
        parsed = urllib.parse.urlparse(url)
        base_url = urllib.parse.urlunparse(parsed._replace(query="", fragment=""))
        query_params: dict[str, str] = dict(urllib.parse.parse_qsl(parsed.query))

        all_params: dict[str, str] = {**query_params, **oauth_params}

        # 2. Percent-encode each key/value and sort.
        encoded_pairs = sorted(
            (urllib.parse.quote(k, safe=""), urllib.parse.quote(v, safe=""))
            for k, v in all_params.items()
        )
        param_string = "&".join(f"{k}={v}" for k, v in encoded_pairs)

        # 3. Signature base string.
        signature_base = "&".join(
            [
                urllib.parse.quote(method, safe=""),
                urllib.parse.quote(base_url, safe=""),
                urllib.parse.quote(param_string, safe=""),
            ]
        )

        # 4. Signing key.
        signing_key = (
            f"{urllib.parse.quote(consumer_secret, safe='')}"
            f"&{urllib.parse.quote(token_secret, safe='')}"
        )

        # 5. HMAC-SHA256 and base64 encode.
        digest = hmac.new(
            signing_key.encode("utf-8"),
            signature_base.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("ascii")

    def _load_credentials(self) -> None:
        """
        Load NetSuite TBA credentials from AWS Secrets Manager.

        Credentials are stored once in-memory and never refreshed — they are
        long-lived OAuth tokens that do not expire automatically.

        Raises:
            NetSuiteCredentialError: secret absent, access denied, or malformed JSON.
        """
        secret_id = _SECRET_PATH_TEMPLATE.format(environment=self._environment)
        try:
            response = self._secrets_client.get_secret_value(SecretId=secret_id)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            raise NetSuiteCredentialError(
                f"Failed to retrieve NetSuite credentials from Secrets Manager "
                f"(secret={secret_id!r}, code={code!r})."
            ) from None

        raw = response.get("SecretString") or ""
        try:
            payload: dict[str, Any] = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise NetSuiteCredentialError(
                "NetSuite credentials secret contains invalid JSON."
            ) from exc

        required = {"account_id", "consumer_key", "consumer_secret", "token_id", "token_secret"}
        missing = required - payload.keys()
        if missing:
            raise NetSuiteCredentialError(
                f"NetSuite credentials secret is missing required keys: {sorted(missing)}."
            )

        self._account_id = payload["account_id"]
        self._consumer_key = payload["consumer_key"]
        self._consumer_secret = payload["consumer_secret"]
        self._token_id = payload["token_id"]
        self._token_secret = payload["token_secret"]
        self._credentials_loaded_at = time.monotonic()

        _logger.info(
            "netsuite_credentials_loaded",
            environment=self._environment,
            account_id=self._account_id,
            # credential values intentionally omitted
        )
