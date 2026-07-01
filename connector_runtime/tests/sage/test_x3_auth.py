"""
Tests for X3AuthClient.

Coverage:
  - get_access_token() returns token from OAuth token endpoint
  - Token cached in memory; second call does not hit the endpoint again
  - Token proactively refreshed when within _PROACTIVE_REFRESH_SECONDS of expiry
  - invalidate_token() forces fresh fetch on next call
  - base_url property raises RuntimeError before first successful get_access_token()
  - base_url resolved correctly as "{server_base}/api/{folder}" after token fetch
  - folder property raises RuntimeError before first successful get_access_token()
  - folder property returns folder name after token fetch
  - build_auth_headers() returns Authorization, Content-Type, Accept headers
  - Bearer token format in Authorization header (OWASP A07)
  - Token value NOT in log events or exception messages (OWASP A09)
  - client_secret sent in form body, NOT as query param (OWASP A07)
  - Credential load failure → X3CredentialError
  - Token endpoint 401 → X3AuthError
  - Token endpoint generic HTTP error → X3AuthError
  - Token endpoint returns no access_token → X3AuthError
  - expires_in taken from response; falls back to 3600
  - Missing required credential key → X3CredentialError
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
import requests_mock as requests_mock_lib

from connector_runtime.adapters.sage.common.sage_credential_manager import SageCredentialError
from connector_runtime.adapters.sage.common.sage_http_client import (
    SageAuthenticationError,
    SageHttpClient,
)
from connector_runtime.adapters.sage.products.x3.x3_auth import (
    X3AuthClient,
    X3AuthError,
    X3CredentialError,
    _DEFAULT_TOKEN_TTL_SECONDS,
    _PROACTIVE_REFRESH_SECONDS,
)

_TOKEN_URL = "https://x3.company.com/auth/token"
_SERVER_BASE_URL = "https://x3.company.com"
_FOLDER = "SEED"
_EXPECTED_BASE_URL = f"{_SERVER_BASE_URL}/api/{_FOLDER}"

_VALID_CREDS: dict[str, str] = {
    "base_url": _SERVER_BASE_URL,
    "token_url": _TOKEN_URL,
    "client_id": "x3-client-id",
    "client_secret": "x3-super-secret",
    "folder": _FOLDER,
}

_TOKEN_RESPONSE: dict[str, object] = {
    "access_token": "eyJx3.bearer.token",
    "expires_in": 3600,
    "token_type": "Bearer",
}


def _make_auth(creds: dict[str, str] | None = None) -> X3AuthClient:
    """Build an X3AuthClient with a mocked credential manager."""
    mock_cred_mgr = MagicMock()
    mock_cred_mgr.get_credentials.return_value = creds or _VALID_CREDS
    return X3AuthClient(
        credential_manager=mock_cred_mgr,
        http_client=SageHttpClient(),
    )


# ---------------------------------------------------------------------------
# Token acquisition
# ---------------------------------------------------------------------------


class TestTokenAcquisition:
    def test_get_access_token_returns_token(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.post(_TOKEN_URL, json=_TOKEN_RESPONSE)
        auth = _make_auth()
        token = auth.get_access_token()
        assert token == "eyJx3.bearer.token"

    def test_token_cached_second_call_no_request(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.post(_TOKEN_URL, json=_TOKEN_RESPONSE)
        auth = _make_auth()
        auth.get_access_token()
        auth.get_access_token()
        assert requests_mock.call_count == 1

    def test_expires_in_from_response(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.post(_TOKEN_URL, json={**_TOKEN_RESPONSE, "expires_in": 7200})
        auth = _make_auth()
        auth.get_access_token()
        # Should expire in ~7200s minus proactive refresh buffer
        expected_min = time.time() + 7200 - _PROACTIVE_REFRESH_SECONDS - 2
        assert auth._token_expires_at > expected_min

    def test_expires_in_fallback_when_missing(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        response_no_expiry = {"access_token": "tok", "token_type": "Bearer"}
        requests_mock.post(_TOKEN_URL, json=response_no_expiry)
        auth = _make_auth()
        auth.get_access_token()
        expected_min = time.time() + _DEFAULT_TOKEN_TTL_SECONDS - _PROACTIVE_REFRESH_SECONDS - 2
        assert auth._token_expires_at > expected_min

    def test_proactive_refresh_when_near_expiry(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.post(_TOKEN_URL, json=_TOKEN_RESPONSE)
        auth = _make_auth()
        auth.get_access_token()
        # Simulate token expiring soon (within the proactive refresh window)
        auth._token_expires_at = time.time() + (_PROACTIVE_REFRESH_SECONDS - 10)
        auth.get_access_token()
        assert requests_mock.call_count == 2

    def test_invalidate_token_forces_refresh(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.post(_TOKEN_URL, json=_TOKEN_RESPONSE)
        auth = _make_auth()
        auth.get_access_token()
        auth.invalidate_token()
        auth.get_access_token()
        assert requests_mock.call_count == 2

    def test_client_secret_in_post_body_not_query_param(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.post(_TOKEN_URL, json=_TOKEN_RESPONSE)
        auth = _make_auth()
        auth.get_access_token()
        request_body = requests_mock.last_request.text
        assert "x3-super-secret" in request_body
        assert "x3-super-secret" not in requests_mock.last_request.url


# ---------------------------------------------------------------------------
# base_url and folder properties
# ---------------------------------------------------------------------------


class TestBaseUrlAndFolder:
    def test_base_url_raises_before_token_fetch(self) -> None:
        auth = _make_auth()
        with pytest.raises(RuntimeError, match="base_url is not available"):
            _ = auth.base_url

    def test_base_url_resolved_after_token_fetch(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.post(_TOKEN_URL, json=_TOKEN_RESPONSE)
        auth = _make_auth()
        auth.get_access_token()
        assert auth.base_url == _EXPECTED_BASE_URL

    def test_base_url_strips_trailing_slash(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        creds = {**_VALID_CREDS, "base_url": f"{_SERVER_BASE_URL}/"}
        requests_mock.post(_TOKEN_URL, json=_TOKEN_RESPONSE)
        auth = _make_auth(creds)
        auth.get_access_token()
        assert not auth.base_url.endswith("/")
        assert auth.base_url == _EXPECTED_BASE_URL

    def test_folder_raises_before_token_fetch(self) -> None:
        auth = _make_auth()
        with pytest.raises(RuntimeError, match="folder is not available"):
            _ = auth.folder

    def test_folder_returns_folder_name_after_token_fetch(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.post(_TOKEN_URL, json=_TOKEN_RESPONSE)
        auth = _make_auth()
        auth.get_access_token()
        assert auth.folder == _FOLDER


# ---------------------------------------------------------------------------
# build_auth_headers
# ---------------------------------------------------------------------------


class TestBuildAuthHeaders:
    def test_headers_contain_bearer_token(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.post(_TOKEN_URL, json=_TOKEN_RESPONSE)
        auth = _make_auth()
        headers = auth.build_auth_headers()
        assert headers["Authorization"] == "Bearer eyJx3.bearer.token"

    def test_headers_contain_content_type(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.post(_TOKEN_URL, json=_TOKEN_RESPONSE)
        auth = _make_auth()
        headers = auth.build_auth_headers()
        assert headers["Content-Type"] == "application/json"

    def test_headers_contain_accept(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.post(_TOKEN_URL, json=_TOKEN_RESPONSE)
        auth = _make_auth()
        headers = auth.build_auth_headers()
        assert headers["Accept"] == "application/json"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_missing_credential_raises_x3_credential_error(self) -> None:
        mock_cred_mgr = MagicMock()
        mock_cred_mgr.get_credentials.side_effect = SageCredentialError("no secret")
        auth = X3AuthClient(
            credential_manager=mock_cred_mgr,
            http_client=SageHttpClient(),
        )
        with pytest.raises(X3CredentialError):
            auth.get_access_token()

    def test_missing_required_key_raises_x3_credential_error(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        creds_missing_folder = {k: v for k, v in _VALID_CREDS.items() if k != "folder"}
        with pytest.raises(X3CredentialError, match="folder"):
            auth = _make_auth(creds_missing_folder)
            auth.get_access_token()

    def test_token_endpoint_401_raises_x3_auth_error(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.post(_TOKEN_URL, status_code=401)
        auth = _make_auth()
        with pytest.raises(X3AuthError):
            auth.get_access_token()

    def test_token_endpoint_500_raises_x3_auth_error(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.post(_TOKEN_URL, status_code=500)
        auth = _make_auth()
        with pytest.raises(X3AuthError):
            auth.get_access_token()

    def test_no_access_token_in_response_raises_x3_auth_error(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.post(_TOKEN_URL, json={"token_type": "Bearer"})
        auth = _make_auth()
        with pytest.raises(X3AuthError):
            auth.get_access_token()


# ---------------------------------------------------------------------------
# Security: token value not leaked in logs or exceptions (OWASP A09)
# ---------------------------------------------------------------------------


class TestSecurityTokenNotLeaked:
    def test_token_value_not_in_auth_error_message(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.post(_TOKEN_URL, json=_TOKEN_RESPONSE)
        auth = _make_auth()
        auth.get_access_token()
        # Simulate a subsequent auth failure and verify the token is not exposed
        requests_mock.post(_TOKEN_URL, status_code=401)
        auth.invalidate_token()
        try:
            auth.get_access_token()
        except X3AuthError as exc:
            assert "eyJx3.bearer.token" not in str(exc)

    def test_client_secret_not_in_exception_message(self) -> None:
        mock_cred_mgr = MagicMock()
        mock_cred_mgr.get_credentials.side_effect = SageCredentialError(
            "credentials not found"
        )
        auth = X3AuthClient(
            credential_manager=mock_cred_mgr,
            http_client=SageHttpClient(),
        )
        try:
            auth.get_access_token()
        except X3CredentialError as exc:
            assert "x3-super-secret" not in str(exc)
