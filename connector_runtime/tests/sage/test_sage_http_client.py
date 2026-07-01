"""
Tests for SageHttpClient.

Coverage:
  - GET success → parsed JSON dict
  - POST success → parsed JSON dict
  - POST form success → parsed JSON dict
  - HTTP 400 → SageInvalidRequestError (with status_code attribute)
  - HTTP 401 → SageAuthenticationError
  - HTTP 403 → SageAuthenticationError
  - HTTP 404 → SageObjectNotFoundError
  - HTTP 429 → SageRateLimitError
  - HTTP 500 → SageServiceUnavailableError
  - HTTP 503 → SageServiceUnavailableError
  - Non-2xx unknown code → SageHttpError
  - GET / POST / POST-form timeout → SageTimeoutError
  - GET / POST / POST-form connection error → SageNetworkError
  - Non-JSON 200 response → SageHttpError
  - TLS verify=True enforced (cannot be disabled)
  - Custom timeout value is respected
  - GET with query params forwarded correctly
  - Error exception hierarchy (SageAuthenticationError is SageHttpError)
  - status_code attribute populated on HTTP errors
"""

from __future__ import annotations

import pytest
import requests
import requests_mock as requests_mock_lib

from connector_runtime.adapters.sage.common.sage_http_client import (
    SageAuthenticationError,
    SageHttpClient,
    SageHttpError,
    SageInvalidRequestError,
    SageNetworkError,
    SageObjectNotFoundError,
    SageRateLimitError,
    SageServiceUnavailableError,
    SageTimeoutError,
)

_BASE_URL = "https://api.intacct.com/ia/api/v1"
_GET_URL = f"{_BASE_URL}/objects/accounts-receivable/customer"
_POST_URL = f"{_BASE_URL}/services/v1/query"
_TOKEN_URL = f"{_BASE_URL}/auth/token"
_AUTH_HEADERS = {"Authorization": "Bearer test-token", "Accept": "application/json"}


def _make_client(timeout: int = 45) -> SageHttpClient:
    return SageHttpClient(timeout_seconds=timeout)


# ---------------------------------------------------------------------------
# GET method
# ---------------------------------------------------------------------------


class TestGetMethod:
    def test_get_success_returns_parsed_json(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        payload = {"ia::result": {"object": "customer", "fields": []}}
        requests_mock.get(_GET_URL, json=payload)
        client = _make_client()
        result = client.get(_GET_URL, headers=_AUTH_HEADERS)
        assert result == payload

    def test_get_with_query_params(self, requests_mock: requests_mock_lib.Mocker) -> None:
        requests_mock.get(_GET_URL, json={"ok": True})
        client = _make_client()
        result = client.get(_GET_URL, headers=_AUTH_HEADERS, params={"limit": "10"})
        assert result == {"ok": True}
        assert "limit=10" in requests_mock.last_request.url

    def test_get_401_raises_auth_error(self, requests_mock: requests_mock_lib.Mocker) -> None:
        requests_mock.get(_GET_URL, status_code=401)
        client = _make_client()
        with pytest.raises(SageAuthenticationError) as exc_info:
            client.get(_GET_URL, headers=_AUTH_HEADERS)
        assert exc_info.value.status_code == 401

    def test_get_403_raises_auth_error(self, requests_mock: requests_mock_lib.Mocker) -> None:
        requests_mock.get(_GET_URL, status_code=403)
        client = _make_client()
        with pytest.raises(SageAuthenticationError) as exc_info:
            client.get(_GET_URL, headers=_AUTH_HEADERS)
        assert exc_info.value.status_code == 403

    def test_get_404_raises_not_found(self, requests_mock: requests_mock_lib.Mocker) -> None:
        requests_mock.get(_GET_URL, status_code=404)
        client = _make_client()
        with pytest.raises(SageObjectNotFoundError) as exc_info:
            client.get(_GET_URL, headers=_AUTH_HEADERS)
        assert exc_info.value.status_code == 404

    def test_get_429_raises_rate_limit(self, requests_mock: requests_mock_lib.Mocker) -> None:
        requests_mock.get(_GET_URL, status_code=429)
        client = _make_client()
        with pytest.raises(SageRateLimitError) as exc_info:
            client.get(_GET_URL, headers=_AUTH_HEADERS)
        assert exc_info.value.status_code == 429

    def test_get_400_raises_invalid_request(self, requests_mock: requests_mock_lib.Mocker) -> None:
        requests_mock.get(_GET_URL, status_code=400)
        client = _make_client()
        with pytest.raises(SageInvalidRequestError) as exc_info:
            client.get(_GET_URL, headers=_AUTH_HEADERS)
        assert exc_info.value.status_code == 400

    def test_get_500_raises_service_unavailable(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.get(_GET_URL, status_code=500)
        client = _make_client()
        with pytest.raises(SageServiceUnavailableError) as exc_info:
            client.get(_GET_URL, headers=_AUTH_HEADERS)
        assert exc_info.value.status_code == 500

    def test_get_503_raises_service_unavailable(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.get(_GET_URL, status_code=503)
        client = _make_client()
        with pytest.raises(SageServiceUnavailableError) as exc_info:
            client.get(_GET_URL, headers=_AUTH_HEADERS)
        assert exc_info.value.status_code == 503

    def test_get_unknown_4xx_raises_http_error(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        """Status codes not explicitly mapped fall through to generic SageHttpError."""
        requests_mock.get(_GET_URL, status_code=418)
        client = _make_client()
        with pytest.raises(SageHttpError) as exc_info:
            client.get(_GET_URL, headers=_AUTH_HEADERS)
        assert exc_info.value.status_code == 418

    def test_get_non_json_200_raises_http_error(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.get(_GET_URL, text="<html>Not JSON</html>", status_code=200)
        client = _make_client()
        with pytest.raises(SageHttpError, match="not valid JSON"):
            client.get(_GET_URL, headers=_AUTH_HEADERS)

    def test_get_timeout_raises_timeout_error(self) -> None:
        client = _make_client()
        with (
            pytest.raises(SageTimeoutError),
            requests_mock_lib.Mocker() as m,
        ):
            m.get(_GET_URL, exc=requests.Timeout())
            client.get(_GET_URL, headers=_AUTH_HEADERS)

    def test_get_connection_error_raises_network_error(self) -> None:
        client = _make_client()
        with (
            pytest.raises(SageNetworkError),
            requests_mock_lib.Mocker() as m,
        ):
            m.get(_GET_URL, exc=requests.ConnectionError())
            client.get(_GET_URL, headers=_AUTH_HEADERS)


# ---------------------------------------------------------------------------
# POST method
# ---------------------------------------------------------------------------


class TestPostMethod:
    def test_post_success_returns_parsed_json(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        response_payload = {"ia::result": [{"key": "1", "id": "C001"}], "ia::meta": {"next": None}}
        requests_mock.post(_POST_URL, json=response_payload)
        client = _make_client()
        result = client.post(
            _POST_URL,
            headers=_AUTH_HEADERS,
            json_body={"object": "accounts-receivable/customer", "fields": ["key"]},
        )
        assert result == response_payload

    def test_post_401_raises_auth_error(self, requests_mock: requests_mock_lib.Mocker) -> None:
        requests_mock.post(_POST_URL, status_code=401)
        client = _make_client()
        with pytest.raises(SageAuthenticationError):
            client.post(_POST_URL, headers=_AUTH_HEADERS, json_body={})

    def test_post_timeout_raises_timeout_error(self) -> None:
        client = _make_client()
        with (
            pytest.raises(SageTimeoutError),
            requests_mock_lib.Mocker() as m,
        ):
            m.post(_POST_URL, exc=requests.Timeout())
            client.post(_POST_URL, headers=_AUTH_HEADERS, json_body={})

    def test_post_connection_error_raises_network_error(self) -> None:
        client = _make_client()
        with (
            pytest.raises(SageNetworkError),
            requests_mock_lib.Mocker() as m,
        ):
            m.post(_POST_URL, exc=requests.ConnectionError())
            client.post(_POST_URL, headers=_AUTH_HEADERS, json_body={})

    def test_post_sends_json_body(self, requests_mock: requests_mock_lib.Mocker) -> None:
        requests_mock.post(_POST_URL, json={"ok": True})
        client = _make_client()
        body = {"object": "customer", "fields": ["key"]}
        client.post(_POST_URL, headers=_AUTH_HEADERS, json_body=body)
        import json
        sent = json.loads(requests_mock.last_request.text)
        assert sent == body


# ---------------------------------------------------------------------------
# POST form method
# ---------------------------------------------------------------------------


class TestPostFormMethod:
    def test_post_form_success_returns_parsed_json(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        token_resp = {"access_token": "eyJ...", "expires_in": 3600, "token_type": "Bearer"}
        requests_mock.post(_TOKEN_URL, json=token_resp)
        client = _make_client()
        result = client.post_form(
            _TOKEN_URL,
            headers={"Accept": "application/json"},
            form_data={"grant_type": "client_credentials", "client_id": "cid", "client_secret": "s"},
        )
        assert result["access_token"] == "eyJ..."

    def test_post_form_sends_form_encoded(self, requests_mock: requests_mock_lib.Mocker) -> None:
        requests_mock.post(_TOKEN_URL, json={"access_token": "tok"})
        client = _make_client()
        client.post_form(
            _TOKEN_URL,
            headers={"Accept": "application/json"},
            form_data={"grant_type": "client_credentials"},
        )
        assert "application/x-www-form-urlencoded" in requests_mock.last_request.headers.get(
            "Content-Type", ""
        )

    def test_post_form_401_raises_auth_error(self, requests_mock: requests_mock_lib.Mocker) -> None:
        requests_mock.post(_TOKEN_URL, status_code=401)
        client = _make_client()
        with pytest.raises(SageAuthenticationError):
            client.post_form(_TOKEN_URL, headers={}, form_data={})

    def test_post_form_timeout_raises_timeout_error(self) -> None:
        client = _make_client()
        with (
            pytest.raises(SageTimeoutError),
            requests_mock_lib.Mocker() as m,
        ):
            m.post(_TOKEN_URL, exc=requests.Timeout())
            client.post_form(_TOKEN_URL, headers={}, form_data={})

    def test_post_form_connection_error_raises_network_error(self) -> None:
        client = _make_client()
        with (
            pytest.raises(SageNetworkError),
            requests_mock_lib.Mocker() as m,
        ):
            m.post(_TOKEN_URL, exc=requests.ConnectionError())
            client.post_form(_TOKEN_URL, headers={}, form_data={})


# ---------------------------------------------------------------------------
# TLS & security
# ---------------------------------------------------------------------------


class TestTlsAndSecurity:
    def test_tls_verify_is_true(self) -> None:
        """TLS verification must always be enabled — no way to disable it."""
        client = _make_client()
        assert client._session.verify is True  # type: ignore[attr-defined]

    def test_custom_timeout_respected(self, requests_mock: requests_mock_lib.Mocker) -> None:
        requests_mock.get(_GET_URL, json={})
        client = SageHttpClient(timeout_seconds=10)
        client.get(_GET_URL, headers=_AUTH_HEADERS)
        # Just verify the client was constructed and call completed without error.
        assert client._timeout == 10  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class TestExceptionHierarchy:
    def test_auth_error_is_http_error(self) -> None:
        exc = SageAuthenticationError("401", status_code=401)
        assert isinstance(exc, SageHttpError)
        assert exc.status_code == 401

    def test_not_found_error_is_http_error(self) -> None:
        exc = SageObjectNotFoundError("404", status_code=404)
        assert isinstance(exc, SageHttpError)

    def test_rate_limit_error_is_http_error(self) -> None:
        exc = SageRateLimitError("429", status_code=429)
        assert isinstance(exc, SageHttpError)

    def test_service_unavailable_is_http_error(self) -> None:
        exc = SageServiceUnavailableError("503", status_code=503)
        assert isinstance(exc, SageHttpError)

    def test_invalid_request_is_http_error(self) -> None:
        exc = SageInvalidRequestError("400", status_code=400)
        assert isinstance(exc, SageHttpError)

    def test_timeout_error_is_http_error(self) -> None:
        exc = SageTimeoutError("timeout")
        assert isinstance(exc, SageHttpError)
        assert exc.status_code is None

    def test_network_error_is_http_error(self) -> None:
        exc = SageNetworkError("network")
        assert isinstance(exc, SageHttpError)
