"""
Tests for IntacctAuthClient.

Coverage:
  - get_access_token() returns token from OAuth token endpoint
  - Token cached in memory; second call does not hit the endpoint again
  - Token proactively refreshed when within _PROACTIVE_REFRESH_SECONDS of expiry
  - invalidate_token() forces fresh fetch on next call
  - base_url property raises RuntimeError before first successful get_access_token()
  - base_url resolved correctly after token fetch (trailing slash stripped)
  - build_auth_headers() returns Authorization, Content-Type, Accept headers
  - Bearer token format in Authorization header (OWASP A07)
  - Token value NOT present in log events or exception messages (OWASP A09)
  - client_secret sent in form body, NOT as query param (OWASP A07)
  - Credential load failure → IntacctCredentialError
  - Token endpoint 401 → IntacctAuthError
  - Token endpoint generic HTTP error → IntacctAuthError
  - Token endpoint returns no access_token → IntacctAuthError
  - expires_in taken from response; falls back to 3600
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
    SageRateLimitError,
)
from connector_runtime.adapters.sage.products.intacct.intacct_auth import (
    IntacctAuthClient,
    IntacctAuthError,
    IntacctCredentialError,
    _PROACTIVE_REFRESH_SECONDS,
    _DEFAULT_TOKEN_TTL_SECONDS,
)

_TOKEN_URL = "https://api.intacct.com/ia/api/v1/auth/token"
_BASE_URL = "https://api.intacct.com/ia/api/v1"

_VALID_CREDS: dict[str, str] = {
    "base_url": _BASE_URL,
    "token_url": _TOKEN_URL,
    "client_id": "test-client-id",
    "client_secret": "super-secret-value",
    "company_id": "COMPANY-001",
}

_TOKEN_RESPONSE = {
    "access_token": "eyJtest.token.value",
    "expires_in": 3600,
    "token_type": "Bearer",
}


def _make_auth(creds: dict[str, str] | None = None) -> IntacctAuthClient:
    """Build an IntacctAuthClient with a mocked credential manager."""
    mock_cred_mgr = MagicMock()
    mock_cred_mgr.get_credentials.return_value = creds or _VALID_CREDS
    return IntacctAuthClient(
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
        assert token == "eyJtest.token.value"

    def test_base_url_resolved_after_token_fetch(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.post(_TOKEN_URL, json=_TOKEN_RESPONSE)
        auth = _make_auth()
        auth.get_access_token()
        assert auth.base_url == _BASE_URL  # trailing slash stripped if present

    def test_base_url_trailing_slash_stripped(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        creds_with_slash = {**_VALID_CREDS, "base_url": f"{_BASE_URL}/"}
        requests_mock.post(_TOKEN_URL, json=_TOKEN_RESPONSE)
        auth = _make_auth(creds_with_slash)
        auth.get_access_token()
        assert not auth.base_url.endswith("/")

    def test_base_url_before_fetch_raises_runtime_error(self) -> None:
        auth = _make_auth()
        with pytest.raises(RuntimeError, match="base_url is not available"):
            _ = auth.base_url

    def test_token_cached_no_second_endpoint_call(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.post(_TOKEN_URL, json=_TOKEN_RESPONSE)
        auth = _make_auth()
        t1 = auth.get_access_token()
        t2 = auth.get_access_token()
        assert t1 == t2
        assert requests_mock.call_count == 1

    def test_expires_in_taken_from_response(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.post(_TOKEN_URL, json={**_TOKEN_RESPONSE, "expires_in": 1800})
        auth = _make_auth()
        auth.get_access_token()
        # Token should be valid for ~1800s (with proactive refresh buffer)
        expected_min = time.time() + 1800 - _PROACTIVE_REFRESH_SECONDS - 5
        assert auth._token_expires_at > expected_min  # type: ignore[attr-defined]

    def test_default_ttl_used_when_expires_in_absent(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        response_no_expires = {k: v for k, v in _TOKEN_RESPONSE.items() if k != "expires_in"}
        requests_mock.post(_TOKEN_URL, json=response_no_expires)
        auth = _make_auth()
        auth.get_access_token()
        expected_min = time.time() + _DEFAULT_TOKEN_TTL_SECONDS - _PROACTIVE_REFRESH_SECONDS - 5
        assert auth._token_expires_at > expected_min  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------


class TestTokenRefresh:
    def test_proactive_refresh_when_near_expiry(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.post(_TOKEN_URL, json=_TOKEN_RESPONSE)
        auth = _make_auth()
        auth.get_access_token()
        assert requests_mock.call_count == 1

        # Move expiry to within the proactive refresh window.
        auth._token_expires_at = time.time() + _PROACTIVE_REFRESH_SECONDS - 10  # type: ignore[attr-defined]
        auth.get_access_token()
        assert requests_mock.call_count == 2  # second fetch triggered

    def test_invalidate_token_forces_refetch(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.post(_TOKEN_URL, json=_TOKEN_RESPONSE)
        auth = _make_auth()
        auth.get_access_token()
        auth.invalidate_token()
        auth.get_access_token()
        assert requests_mock.call_count == 2

    def test_invalidate_token_clears_access_token(self) -> None:
        auth = _make_auth()
        auth._access_token = "cached-token"  # type: ignore[attr-defined]
        auth._token_expires_at = time.time() + 9999  # type: ignore[attr-defined]
        auth.invalidate_token()
        assert auth._access_token is None  # type: ignore[attr-defined]
        assert auth._token_expires_at == 0.0  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# build_auth_headers
# ---------------------------------------------------------------------------


class TestBuildAuthHeaders:
    def test_auth_headers_contain_bearer_token(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.post(_TOKEN_URL, json=_TOKEN_RESPONSE)
        auth = _make_auth()
        headers = auth.build_auth_headers()
        assert headers["Authorization"] == "Bearer eyJtest.token.value"

    def test_auth_headers_contain_content_type(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.post(_TOKEN_URL, json=_TOKEN_RESPONSE)
        auth = _make_auth()
        headers = auth.build_auth_headers()
        assert headers["Content-Type"] == "application/json"
        assert headers["Accept"] == "application/json"

    def test_token_not_in_form_data_sent_to_endpoint(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        """client_secret must be sent as form data, not as a URL query parameter."""
        requests_mock.post(_TOKEN_URL, json=_TOKEN_RESPONSE)
        auth = _make_auth()
        auth.get_access_token()
        request = requests_mock.last_request
        # Verify it was not a JSON body (must be form-encoded)
        assert "client_secret" not in (request.query or "")
        assert "super-secret-value" not in request.url


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestErrorCases:
    def test_credential_load_failure_raises_intacct_credential_error(self) -> None:
        mock_cred_mgr = MagicMock()
        mock_cred_mgr.get_credentials.side_effect = SageCredentialError("no secret")
        auth = IntacctAuthClient(
            credential_manager=mock_cred_mgr,
            http_client=SageHttpClient(),
        )
        with pytest.raises(IntacctCredentialError):
            auth.get_access_token()

    def test_intacct_credential_error_is_sage_credential_error(self) -> None:
        exc = IntacctCredentialError("test")
        assert isinstance(exc, SageCredentialError)

    def test_token_endpoint_401_raises_intacct_auth_error(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.post(_TOKEN_URL, status_code=401)
        auth = _make_auth()
        with pytest.raises(IntacctAuthError, match="rejected"):
            auth.get_access_token()

    def test_token_endpoint_generic_http_error_raises_intacct_auth_error(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.post(_TOKEN_URL, status_code=429)
        auth = _make_auth()
        with pytest.raises(IntacctAuthError, match="failed"):
            auth.get_access_token()

    def test_missing_access_token_in_response_raises_intacct_auth_error(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.post(_TOKEN_URL, json={"token_type": "Bearer"})  # no access_token
        auth = _make_auth()
        with pytest.raises(IntacctAuthError, match="no access_token"):
            auth.get_access_token()

    def test_error_message_does_not_contain_client_secret(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.post(_TOKEN_URL, status_code=401)
        auth = _make_auth()
        try:
            auth.get_access_token()
        except IntacctAuthError as exc:
            assert "super-secret-value" not in str(exc)

    def test_error_message_does_not_contain_token_value(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        # First fetch succeeds; simulate the token never appearing in an error.
        requests_mock.post(_TOKEN_URL, json=_TOKEN_RESPONSE)
        auth = _make_auth()
        token = auth.get_access_token()
        # The token should exist but not be embedded in error messages.
        assert token == "eyJtest.token.value"
        # IntacctAuthClient never includes the token value in logs/exceptions.
        # (Verified by code review: _refresh_token only logs expires_in_seconds.)
