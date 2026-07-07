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

Credential retrieval (DUP-2) is delegated to the shared
SecretsManagerCredentialClient rather than hand-rolling boto3/Secrets Manager
boilerplate here — see connector_runtime/credential_client.py.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse
import uuid
from typing import Final

from connector_runtime.credential_client import (
    SecretsManagerCredentialClient,
    SecretsManagerCredentialError,
)
from connector_runtime.interfaces.connector_interface import (
    DeterministicConnectorError,
    ExtractionErrorClassification,
)

_SECRET_PATH_TEMPLATE: Final[str] = "{environment}/sources/netsuite/credentials"  # noqa: S105
_OAUTH_VERSION: Final[str] = "1.0"
_SIGNATURE_METHOD: Final[str] = "HMAC-SHA256"

# Required secret keys — enforced by the shared SecretsManagerCredentialClient.
_REQUIRED_CREDENTIAL_KEYS: Final[frozenset[str]] = frozenset(
    {"account_id", "consumer_key", "consumer_secret", "token_id", "token_secret"}
)


class NetSuiteCredentialError(SecretsManagerCredentialError, DeterministicConnectorError):
    """Raised when NetSuite credentials cannot be retrieved from Secrets Manager."""

    classification = ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS


class NetSuiteAuthError(DeterministicConnectorError):
    """Raised when an OAuth 1.0a signature cannot be generated."""

    classification = ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS


class NetSuiteAuthClient:
    """
    Generates per-request OAuth 1.0a TBA Authorization headers for NetSuite.

    One instance can be shared across all requests within an extraction run.
    Credentials are loaded lazily on the first get_auth_headers() call (via
    the shared SecretsManagerCredentialClient's own TTL cache) and re-used
    for the lifetime of the instance within that cache window.

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
        self._credentials_client = SecretsManagerCredentialClient(
            secret_id=_SECRET_PATH_TEMPLATE.format(environment=environment),
            region_name=region_name,
            required_keys=_REQUIRED_CREDENTIAL_KEYS,
            source_label="NetSuite",
            error_cls=NetSuiteCredentialError,
            log_event="netsuite_credentials_loaded",
            log_fields={"environment": environment},
        )

    @property
    def account_id(self) -> str:
        """
        NetSuite account ID.  Available after first get_auth_headers() call.
        """
        return self._credentials_client.get_credentials()["account_id"]

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
        credentials = self._credentials_client.get_credentials()
        consumer_key = credentials["consumer_key"]
        consumer_secret = credentials["consumer_secret"]
        token_id = credentials["token_id"]
        token_secret = credentials["token_secret"]
        account_id = credentials["account_id"]

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
