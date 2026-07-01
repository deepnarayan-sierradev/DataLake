"""
Sage X3 OAuth 2.0 authentication client.

Sage X3 Enterprise Management (v12+) uses the OAuth 2.0 client_credentials
grant for REST API authentication.  The 'folder' credential scopes the
token to a specific X3 company folder (e.g. "SEED", "PROD").  All API paths
are prefixed with the folder: {base_url}/api/{folder}/{endpoint}.

Required secret keys (stored at {env}/sources/sage/x3/credentials):
    base_url      — X3 REST API base URL, excluding the /api/{folder} suffix
                    (e.g. "https://x3.company.com")
    token_url     — OAuth 2.0 token endpoint
                    (e.g. "https://x3.company.com/auth/token")
    client_id     — OAuth application client ID
    client_secret — OAuth application client secret
    folder        — X3 company folder name (e.g. "SEED", "PROD")

Token lifecycle:
    - Tokens fetched on first use and cached in memory.
    - Proactive refresh fires when fewer than _PROACTIVE_REFRESH_SECONDS remain.
    - invalidate_token() forces an immediate refresh on the next call.
    - Token TTL read from the token endpoint response (expires_in field).

Resolved base_url property:
    After the first successful get_access_token(), base_url returns
    "{configured_base_url}/api/{folder}" — the root all X3 REST paths derive
    from.  The metadata client and connector append "/{endpoint}" to this.

Security (OWASP A07, A09):
    - client_secret and access_token are never logged or included in exception
      messages.  Only exception type names appear in error messages.
    - client_secret is sent in the POST body, never as a URL query parameter.
    - TLS verification enforced by SageHttpClient (verify=True always).
"""

from __future__ import annotations

import time
from typing import Final

from connector_runtime.adapters.sage.common.sage_credential_manager import (
    SageCredentialError,
    SageCredentialManager,
)
from connector_runtime.adapters.sage.common.sage_http_client import (
    SageAuthenticationError,
    SageHttpClient,
    SageHttpError,
)
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

_PRODUCT_NAME: Final[str] = "x3"

# Refresh the token this many seconds before expiry to avoid mid-extraction expiry.
_PROACTIVE_REFRESH_SECONDS: Final[int] = 300

# Fallback TTL when the token endpoint does not return expires_in.
_DEFAULT_TOKEN_TTL_SECONDS: Final[int] = 3_600

# Required keys that must be present in the X3 credentials secret.
_REQUIRED_CREDENTIAL_KEYS: Final[frozenset[str]] = frozenset(
    {"base_url", "token_url", "client_id", "client_secret", "folder"}
)


class X3CredentialError(SageCredentialError):
    """Raised when X3 credentials are absent or invalid in Secrets Manager."""


class X3AuthError(Exception):
    """Raised when the X3 OAuth 2.0 token endpoint rejects the request."""


class X3AuthClient:
    """
    Manages the Sage X3 OAuth 2.0 client-credentials token lifecycle.

    Implements SageAuthProtocol (structural typing via Protocol — no inheritance).

    One instance per extraction run.  The access token is cached in-memory and
    refreshed proactively when approaching expiry.

    The resolved base_url includes the company folder:
        "{configured_base_url}/api/{folder}"
    so callers can append "/{endpoint}" directly without folder knowledge.

    Usage::

        auth = X3AuthClient(
            credential_manager=SageCredentialManager(...),
            http_client=SageHttpClient(),
        )
        token = auth.get_access_token()   # returns or refreshes Bearer token
        api_root = auth.base_url          # "https://x3.company.com/api/SEED"
        folder = auth.folder              # "SEED"
    """

    def __init__(
        self,
        credential_manager: SageCredentialManager,
        http_client: SageHttpClient,
    ) -> None:
        self._credentials = credential_manager
        self._http = http_client

        # Token state — populated lazily on first get_access_token() call.
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0  # UNIX epoch seconds
        self._base_url: str | None = None    # "{server_base}/api/{folder}"
        self._folder: str | None = None

    # ── SageAuthProtocol interface ─────────────────────────────────────────────

    @property
    def base_url(self) -> str:
        """
        X3 REST API root including the company folder.
        (e.g. "https://x3.company.com/api/SEED")

        Populated after the first successful get_access_token() call.
        """
        if self._base_url is None:
            raise RuntimeError(
                "base_url is not available until get_access_token() succeeds."
            )
        return self._base_url

    @property
    def folder(self) -> str:
        """
        X3 company folder name (e.g. "SEED", "PROD").
        Populated after the first successful get_access_token() call.
        """
        if self._folder is None:
            raise RuntimeError(
                "folder is not available until get_access_token() succeeds."
            )
        return self._folder

    def get_access_token(self) -> str:
        """
        Return a valid X3 OAuth 2.0 Bearer access token.

        Returns the cached token if still valid with the proactive refresh
        buffer; otherwise fetches a new token from the X3 token endpoint.

        Raises:
            X3CredentialError: credentials absent or malformed in Secrets Manager.
            X3AuthError: the X3 token endpoint rejected the request.
        """
        if self._access_token is not None and self._is_token_valid():
            return self._access_token
        self._refresh_token()
        if self._access_token is None:
            raise X3AuthError(
                "Token refresh completed but access_token was not set. "
                "This is an internal implementation error."
            )
        return self._access_token

    def invalidate_token(self) -> None:
        """Force the next get_access_token() call to fetch a fresh token."""
        self._access_token = None
        self._token_expires_at = 0.0
        _logger.info("sage_x3_token_invalidated")

    def build_auth_headers(self) -> dict[str, str]:
        """
        Return the Authorization and content-negotiation headers for an API call.
        """
        return {
            "Authorization": f"Bearer {self.get_access_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ── Private ────────────────────────────────────────────────────────────────

    def _is_token_valid(self) -> bool:
        return (self._token_expires_at - time.time()) > _PROACTIVE_REFRESH_SECONDS

    def _refresh_token(self) -> None:
        """
        Fetch a fresh access token from the X3 OAuth 2.0 token endpoint.

        Raises:
            X3CredentialError: credentials cannot be loaded from Secrets Manager.
            X3AuthError: token endpoint returned an error or unexpected body.
        """
        try:
            creds = self._credentials.get_credentials()
        except SageCredentialError as exc:
            raise X3CredentialError(str(exc)) from exc

        missing = _REQUIRED_CREDENTIAL_KEYS - creds.keys()
        if missing:
            raise X3CredentialError(
                f"X3 credentials secret is missing required keys: {sorted(missing)}. "
                f"All of {sorted(_REQUIRED_CREDENTIAL_KEYS)} must be present."
            )

        token_url: str = creds["token_url"]
        client_id: str = creds["client_id"]
        client_secret: str = creds["client_secret"]
        server_base_url: str = creds["base_url"].rstrip("/")
        folder: str = creds["folder"]

        # client_secret is form body — never a query parameter (OWASP A07).
        try:
            response_body = self._http.post_form(
                url=token_url,
                headers={"Accept": "application/json"},
                form_data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
        except SageAuthenticationError as exc:
            raise X3AuthError(
                "X3 token endpoint rejected the client credentials: "
                f"{type(exc).__name__}"
            ) from None
        except SageHttpError as exc:
            raise X3AuthError(
                f"X3 token request failed: {type(exc).__name__}"
            ) from None

        access_token = response_body.get("access_token")
        if not access_token:
            raise X3AuthError(
                "X3 token endpoint returned a response with no access_token field."
            )

        expires_in = int(response_body.get("expires_in", _DEFAULT_TOKEN_TTL_SECONDS))
        self._access_token = access_token
        self._token_expires_at = time.time() + expires_in
        self._base_url = f"{server_base_url}/api/{folder}"
        self._folder = folder

        _logger.info(
            "sage_x3_token_acquired",
            expires_in_seconds=expires_in,
            folder=folder,
            # Token value intentionally NOT logged (OWASP A09).
        )
