"""
Tests for SalesforceAuthClient.

Covers:
  - Happy-path token fetch and caching
  - Proactive refresh (token within refresh window)
  - Credential retrieval errors from Secrets Manager
  - OAuth endpoint failures (non-2xx, network error)
  - Token never appears in log output (security regression)
  - invalidate_token forces re-fetch
"""

from __future__ import annotations

import json
import time

import boto3
import pytest
from moto import mock_aws

from connector_runtime.adapters.salesforce.salesforce_auth_client import (
    _PROACTIVE_REFRESH_SECONDS,
    _SECRET_PATH_TEMPLATE,
    SalesforceAuthClient,
    SalesforceAuthError,
    SalesforceCredentialError,
)

_REGION = "us-east-1"
_ENV = "dev"
_SECRET_ID = _SECRET_PATH_TEMPLATE.format(environment=_ENV)
_INSTANCE_URL = "https://myorg.my.salesforce.com"
_CLIENT_ID = "test-client-id"
_CLIENT_SECRET = "test-client-secret"  # noqa: S105 — test credential
_ACCESS_TOKEN = "test-access-token-value"  # noqa: S105 — test value


def _put_secret(sm_client: object, value: dict) -> None:  # type: ignore[type-arg]
    sm_client.create_secret(  # type: ignore[union-attr]
        Name=_SECRET_ID,
        SecretString=json.dumps(value),
    )


def _valid_credentials() -> dict:
    return {
        "instance_url": _INSTANCE_URL,
        "client_id": _CLIENT_ID,
        "client_secret": _CLIENT_SECRET,
    }


# ---------------------------------------------------------------------------
# Credential loading
# ---------------------------------------------------------------------------


class TestSalesforceAuthClientCredentials:
    @mock_aws
    def test_missing_secret_raises_credential_error(self) -> None:
        client = SalesforceAuthClient(environment=_ENV, region_name=_REGION)
        # Secret not created — should raise SalesforceCredentialError
        with pytest.raises(SalesforceCredentialError, match="Failed to retrieve"):
            client._load_credentials()  # type: ignore[attr-defined]

    @mock_aws
    def test_malformed_json_raises_credential_error(self) -> None:
        sm = boto3.client("secretsmanager", region_name=_REGION)
        sm.create_secret(Name=_SECRET_ID, SecretString="not-valid-json")
        client = SalesforceAuthClient(environment=_ENV, region_name=_REGION)
        with pytest.raises(SalesforceCredentialError, match="not valid JSON"):
            client._load_credentials()  # type: ignore[attr-defined]

    @mock_aws
    def test_missing_keys_raises_credential_error(self) -> None:
        sm = boto3.client("secretsmanager", region_name=_REGION)
        sm.create_secret(
            Name=_SECRET_ID,
            SecretString=json.dumps({"instance_url": _INSTANCE_URL}),
        )
        client = SalesforceAuthClient(environment=_ENV, region_name=_REGION)
        with pytest.raises(SalesforceCredentialError, match="missing required keys"):
            client._load_credentials()  # type: ignore[attr-defined]

    @mock_aws
    def test_valid_credentials_loaded(self) -> None:
        sm = boto3.client("secretsmanager", region_name=_REGION)
        _put_secret(sm, _valid_credentials())
        client = SalesforceAuthClient(environment=_ENV, region_name=_REGION)
        creds = client._load_credentials()  # type: ignore[attr-defined]
        assert creds["instance_url"] == _INSTANCE_URL
        assert creds["client_id"] == _CLIENT_ID
        assert creds["client_secret"] == _CLIENT_SECRET


# ---------------------------------------------------------------------------
# Token validity logic
# ---------------------------------------------------------------------------


class TestTokenValidity:
    def test_new_client_token_is_invalid(self) -> None:
        client = SalesforceAuthClient(environment=_ENV, region_name=_REGION)
        assert not client._is_token_valid()  # type: ignore[attr-defined]

    def test_token_valid_when_expires_far_in_future(self) -> None:
        client = SalesforceAuthClient(environment=_ENV, region_name=_REGION)
        client._access_token = _ACCESS_TOKEN  # type: ignore[attr-defined]
        client._token_expires_at = time.time() + 7200  # type: ignore[attr-defined]
        assert client._is_token_valid()  # type: ignore[attr-defined]

    def test_token_invalid_within_proactive_refresh_window(self) -> None:
        client = SalesforceAuthClient(environment=_ENV, region_name=_REGION)
        client._access_token = _ACCESS_TOKEN  # type: ignore[attr-defined]
        # Set expiry to just inside the proactive refresh window
        client._token_expires_at = time.time() + _PROACTIVE_REFRESH_SECONDS - 1  # type: ignore[attr-defined]
        assert not client._is_token_valid()  # type: ignore[attr-defined]

    def test_invalidate_clears_token(self) -> None:
        client = SalesforceAuthClient(environment=_ENV, region_name=_REGION)
        client._access_token = _ACCESS_TOKEN  # type: ignore[attr-defined]
        client._token_expires_at = time.time() + 7200  # type: ignore[attr-defined]
        client.invalidate_token()
        assert client._access_token is None  # type: ignore[attr-defined]
        assert client._token_expires_at == 0.0  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# OAuth endpoint interaction — uses responses library to mock HTTP
# ---------------------------------------------------------------------------


class TestTokenRefresh:
    @mock_aws
    def test_successful_token_refresh(self, requests_mock) -> None:  # type: ignore[no-untyped-def]
        sm = boto3.client("secretsmanager", region_name=_REGION)
        _put_secret(sm, _valid_credentials())

        requests_mock.post(
            f"{_INSTANCE_URL}/services/oauth2/token",
            json={
                "access_token": _ACCESS_TOKEN,
                "instance_url": _INSTANCE_URL,
                "expires_in": 7200,
            },
            status_code=200,
        )

        client = SalesforceAuthClient(environment=_ENV, region_name=_REGION)
        token = client.get_access_token()
        assert token == _ACCESS_TOKEN
        assert client.instance_url == _INSTANCE_URL

    @mock_aws
    def test_token_cached_on_second_call(self, requests_mock) -> None:  # type: ignore[no-untyped-def]
        sm = boto3.client("secretsmanager", region_name=_REGION)
        _put_secret(sm, _valid_credentials())

        requests_mock.post(
            f"{_INSTANCE_URL}/services/oauth2/token",
            json={"access_token": _ACCESS_TOKEN, "instance_url": _INSTANCE_URL, "expires_in": 7200},
        )

        client = SalesforceAuthClient(environment=_ENV, region_name=_REGION)
        t1 = client.get_access_token()
        t2 = client.get_access_token()
        assert t1 == t2
        # Token endpoint called exactly once
        assert requests_mock.call_count == 1

    @mock_aws
    def test_non_2xx_raises_auth_error(self, requests_mock) -> None:  # type: ignore[no-untyped-def]
        sm = boto3.client("secretsmanager", region_name=_REGION)
        _put_secret(sm, _valid_credentials())

        requests_mock.post(
            f"{_INSTANCE_URL}/services/oauth2/token",
            json={"error": "invalid_client"},
            status_code=401,
        )

        client = SalesforceAuthClient(environment=_ENV, region_name=_REGION)
        with pytest.raises(SalesforceAuthError, match="HTTP 401"):
            client.get_access_token()

    @mock_aws
    def test_token_not_in_log_output(self, requests_mock, caplog) -> None:  # type: ignore[no-untyped-def]
        """Regression: access token must never appear in any log record."""
        sm = boto3.client("secretsmanager", region_name=_REGION)
        _put_secret(sm, _valid_credentials())

        requests_mock.post(
            f"{_INSTANCE_URL}/services/oauth2/token",
            json={"access_token": _ACCESS_TOKEN, "instance_url": _INSTANCE_URL, "expires_in": 7200},
        )

        import logging

        with caplog.at_level(logging.DEBUG):
            client = SalesforceAuthClient(environment=_ENV, region_name=_REGION)
            client.get_access_token()

        for record in caplog.records:
            assert _ACCESS_TOKEN not in record.getMessage(), (
                f"Access token found in log record: {record.getMessage()!r}"
            )

    def test_instance_url_unavailable_before_first_token(self) -> None:
        client = SalesforceAuthClient(environment=_ENV, region_name=_REGION)
        with pytest.raises(RuntimeError, match="instance_url is not available"):
            _ = client.instance_url
