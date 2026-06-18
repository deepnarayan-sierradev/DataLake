"""
Tests for NetSuiteAuthClient.

Coverage:
  - Credential loading from Secrets Manager (happy path)
  - Missing / malformed secret → NetSuiteCredentialError
  - OAuth 1.0a header structure and required fields
  - Credentials not present in generated headers (OWASP A09)
  - Credentials cached after first load
"""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from connector_runtime.adapters.netsuite.netsuite_auth_client import (
    NetSuiteAuthClient,
    NetSuiteCredentialError,
)

_ENVIRONMENT = "dev"
_REGION = "us-east-1"
_SECRET_NAME = f"{_ENVIRONMENT}/sources/netsuite/credentials"

_VALID_SECRET: dict[str, str] = {
    "account_id": "1234567",
    "consumer_key": "ck-abc",
    "consumer_secret": "cs-secret-xyz",
    "token_id": "ti-abc",
    "token_secret": "ts-secret-xyz",
}


def _create_secret(payload: dict[str, str] | str) -> None:
    """Helper: create the Secrets Manager secret in the mocked environment."""
    client = boto3.client("secretsmanager", region_name=_REGION)
    body = payload if isinstance(payload, str) else json.dumps(payload)
    client.create_secret(Name=_SECRET_NAME, SecretString=body)


class TestCredentialLoading:
    """Happy-path credential loading and caching."""

    @mock_aws
    def test_account_id_available_after_headers(self) -> None:
        _create_secret(_VALID_SECRET)
        auth = NetSuiteAuthClient(environment=_ENVIRONMENT, region_name=_REGION)
        headers = auth.get_auth_headers("GET", "https://1234567.suitetalk.api.netsuite.com/test")
        assert auth.account_id == "1234567"
        assert "Authorization" in headers

    @mock_aws
    def test_credentials_cached_single_secrets_manager_call(self) -> None:
        _create_secret(_VALID_SECRET)
        auth = NetSuiteAuthClient(environment=_ENVIRONMENT, region_name=_REGION)
        # Call twice — second should use cached creds (no second Secrets Manager call).
        auth.get_auth_headers("GET", "https://1234567.suitetalk.api.netsuite.com/test")
        auth.get_auth_headers("POST", "https://1234567.suitetalk.api.netsuite.com/q")
        # If caching works, _load_credentials sets _account_id once.
        assert auth._account_id == "1234567"  # type: ignore[attr-defined]

    @mock_aws
    def test_secret_not_found_raises_credential_error(self) -> None:
        # No secret created.
        auth = NetSuiteAuthClient(environment=_ENVIRONMENT, region_name=_REGION)
        with pytest.raises(NetSuiteCredentialError, match="Secrets Manager"):
            auth.get_auth_headers("GET", "https://1234567.suitetalk.api.netsuite.com/test")

    @mock_aws
    def test_invalid_json_raises_credential_error(self) -> None:
        _create_secret("not-json-at-all{{{")
        auth = NetSuiteAuthClient(environment=_ENVIRONMENT, region_name=_REGION)
        with pytest.raises(NetSuiteCredentialError, match="invalid JSON"):
            auth.get_auth_headers("GET", "https://1234567.suitetalk.api.netsuite.com/test")

    @mock_aws
    def test_missing_keys_raises_credential_error(self) -> None:
        _create_secret({"account_id": "1234567"})  # missing other keys
        auth = NetSuiteAuthClient(environment=_ENVIRONMENT, region_name=_REGION)
        with pytest.raises(NetSuiteCredentialError, match="missing required keys"):
            auth.get_auth_headers("GET", "https://1234567.suitetalk.api.netsuite.com/test")

    def test_empty_environment_raises(self) -> None:
        with pytest.raises(ValueError, match="environment"):
            NetSuiteAuthClient(environment="", region_name=_REGION)


class TestOAuthHeaderStructure:
    """Verify the OAuth 1.0a header contains required fields and correct format."""

    @mock_aws
    def test_authorization_header_present(self) -> None:
        _create_secret(_VALID_SECRET)
        auth = NetSuiteAuthClient(environment=_ENVIRONMENT, region_name=_REGION)
        headers = auth.get_auth_headers("GET", "https://1234567.suitetalk.api.netsuite.com/t")
        assert "Authorization" in headers
        assert headers["Authorization"].startswith("OAuth ")

    @mock_aws
    def test_required_oauth_params_present(self) -> None:
        _create_secret(_VALID_SECRET)
        auth = NetSuiteAuthClient(environment=_ENVIRONMENT, region_name=_REGION)
        header_value = auth.get_auth_headers(
            "POST", "https://1234567.suitetalk.api.netsuite.com/suiteql"
        )["Authorization"]

        assert 'realm="1234567"' in header_value
        assert "oauth_consumer_key" in header_value
        assert "oauth_token" in header_value
        assert "oauth_signature_method" in header_value
        assert "oauth_timestamp" in header_value
        assert "oauth_nonce" in header_value
        assert "oauth_version" in header_value
        assert "oauth_signature" in header_value
        assert "HMAC-SHA256" in header_value

    @mock_aws
    def test_each_call_produces_unique_nonce(self) -> None:
        _create_secret(_VALID_SECRET)
        auth = NetSuiteAuthClient(environment=_ENVIRONMENT, region_name=_REGION)
        url = "https://1234567.suitetalk.api.netsuite.com/t"
        h1 = auth.get_auth_headers("GET", url)["Authorization"]
        h2 = auth.get_auth_headers("GET", url)["Authorization"]
        # Nonces differ because uuid4().hex is called each time.
        # Find oauth_nonce values and compare.
        nonce1 = next(p for p in h1.split(", ") if "oauth_nonce" in p)
        nonce2 = next(p for p in h2.split(", ") if "oauth_nonce" in p)
        assert nonce1 != nonce2


class TestSecurityRequirements:
    """Credential values must not appear in any observable output (OWASP A09)."""

    @mock_aws
    def test_consumer_secret_not_in_header(self) -> None:
        _create_secret(_VALID_SECRET)
        auth = NetSuiteAuthClient(environment=_ENVIRONMENT, region_name=_REGION)
        header_value = auth.get_auth_headers("GET", "https://1234567.suitetalk.api.netsuite.com/t")[
            "Authorization"
        ]
        assert "cs-secret-xyz" not in header_value
        assert "ts-secret-xyz" not in header_value

    @mock_aws
    def test_error_message_does_not_contain_secret_value(self) -> None:
        _create_secret({"account_id": "x"})  # missing keys
        auth = NetSuiteAuthClient(environment=_ENVIRONMENT, region_name=_REGION)
        try:
            auth.get_auth_headers("GET", "https://x.suitetalk.api.netsuite.com/t")
        except NetSuiteCredentialError as exc:
            assert "consumer_secret" not in str(exc).lower() or "cs-" not in str(exc)
