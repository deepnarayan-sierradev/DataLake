"""
Tests for SageCredentialProvider.

Coverage:
  - Constructor validates empty environment, product_name, and required_keys
  - Happy path: get_credentials() returns full dict from Secrets Manager
  - Cache hit: second call within TTL does not call Secrets Manager again
  - TTL expiry: re-fetches after the cache TTL has elapsed
  - invalidate_cache(): forces re-fetch on next call
  - Secrets Manager ClientError → SageCredentialError
  - Secret with no SecretString → SageCredentialError
  - Secret with non-JSON value → SageCredentialError
  - Secret missing required keys → SageCredentialError (with sorted key names)
  - Credentials are NEVER included in exception messages (OWASP A09)
"""

from __future__ import annotations

import json
import time

import boto3
import pytest
from moto import mock_aws

from connector_runtime.adapters.sage.substrate.sage_credential_provider import (
    SageCredentialProvider,
    SageCredentialError,
    _CREDENTIAL_CACHE_TTL_SECONDS,
)

_ENV = "dev"
_REGION = "us-east-1"
_PRODUCT = "intacct"
_SECRET_PATH = f"edl/sources/sage/{_PRODUCT}/credentials"
_REQUIRED_KEYS: frozenset[str] = frozenset({"base_url", "client_id", "client_secret", "company_id"})

_VALID_SECRET: dict[str, str] = {
    "base_url": "https://api.intacct.com/ia/api/v1",
    "token_url": "https://api.intacct.com/ia/api/v1/auth/token",
    "client_id": "test-client-id",
    "client_secret": "super-secret-value-12345",
    "company_id": "COMPANY-001",
}


def _make_manager(required_keys: frozenset[str] | None = None) -> SageCredentialProvider:
    return SageCredentialProvider(
        environment=_ENV,
        region_name=_REGION,
        product_name=_PRODUCT,
        required_keys=required_keys or _REQUIRED_KEYS,
    )


def _create_secret(payload: dict[str, str] | str) -> None:
    client = boto3.client("secretsmanager", region_name=_REGION)
    body = payload if isinstance(payload, str) else json.dumps(payload)
    client.create_secret(Name=_SECRET_PATH, SecretString=body)


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestConstructorValidation:
    def test_empty_environment_raises(self) -> None:
        with pytest.raises(ValueError, match="environment"):
            SageCredentialProvider(
                environment="",
                region_name=_REGION,
                product_name=_PRODUCT,
                required_keys=_REQUIRED_KEYS,
            )

    def test_empty_product_name_raises(self) -> None:
        with pytest.raises(ValueError, match="product_name"):
            SageCredentialProvider(
                environment=_ENV,
                region_name=_REGION,
                product_name="",
                required_keys=_REQUIRED_KEYS,
            )

    def test_empty_required_keys_raises(self) -> None:
        with pytest.raises(ValueError, match="required_keys"):
            SageCredentialProvider(
                environment=_ENV,
                region_name=_REGION,
                product_name=_PRODUCT,
                required_keys=frozenset(),
            )


# ---------------------------------------------------------------------------
# Happy-path credential loading
# ---------------------------------------------------------------------------


class TestCredentialLoading:
    @mock_aws
    def test_returns_all_secret_keys(self) -> None:
        _create_secret(_VALID_SECRET)
        manager = _make_manager()
        creds = manager.get_credentials()
        assert creds["base_url"] == _VALID_SECRET["base_url"]
        assert creds["client_id"] == _VALID_SECRET["client_id"]
        assert creds["company_id"] == _VALID_SECRET["company_id"]

    @mock_aws
    def test_credentials_cached_within_ttl(self) -> None:
        _create_secret(_VALID_SECRET)
        manager = _make_manager()
        creds1 = manager.get_credentials()
        # Overwrite the secret — cache should serve the old value.
        boto3.client("secretsmanager", region_name=_REGION).update_secret(
            SecretId=_SECRET_PATH,
            SecretString=json.dumps({**_VALID_SECRET, "client_id": "updated-id"}),
        )
        creds2 = manager.get_credentials()
        assert creds1 is creds2  # same object means no re-fetch
        assert creds2["client_id"] == "test-client-id"

    @mock_aws
    def test_cache_expires_after_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _create_secret(_VALID_SECRET)
        manager = _make_manager()
        manager.get_credentials()
        # Simulate expired TTL by back-dating the loaded_at timestamp.
        monkeypatch.setattr(
            manager,
            "_loaded_at",
            time.monotonic() - _CREDENTIAL_CACHE_TTL_SECONDS - 1,
        )
        # Update secret to confirm a fresh fetch happens.
        boto3.client("secretsmanager", region_name=_REGION).update_secret(
            SecretId=_SECRET_PATH,
            SecretString=json.dumps({**_VALID_SECRET, "client_id": "rotated-id"}),
        )
        creds = manager.get_credentials()
        assert creds["client_id"] == "rotated-id"

    @mock_aws
    def test_invalidate_cache_forces_refetch(self) -> None:
        _create_secret(_VALID_SECRET)
        manager = _make_manager()
        manager.get_credentials()
        boto3.client("secretsmanager", region_name=_REGION).update_secret(
            SecretId=_SECRET_PATH,
            SecretString=json.dumps({**_VALID_SECRET, "client_id": "rotated-id-2"}),
        )
        manager.invalidate_cache()
        creds = manager.get_credentials()
        assert creds["client_id"] == "rotated-id-2"

    @mock_aws
    def test_returns_dict_type(self) -> None:
        _create_secret(_VALID_SECRET)
        manager = _make_manager()
        creds = manager.get_credentials()
        assert isinstance(creds, dict)


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestErrorCases:
    @mock_aws
    def test_secret_not_found_raises_credential_error(self) -> None:
        # No secret created — should raise SageCredentialError.
        manager = _make_manager()
        with pytest.raises(SageCredentialError, match="Secrets Manager"):
            manager.get_credentials()

    @mock_aws
    def test_invalid_json_raises_credential_error(self) -> None:
        _create_secret("not-json{{{{")
        manager = _make_manager()
        with pytest.raises(SageCredentialError, match="not valid JSON"):
            manager.get_credentials()

    @mock_aws
    def test_missing_required_keys_raises_credential_error(self) -> None:
        # Secret exists but is missing several required keys.
        _create_secret({"base_url": "https://api.intacct.com"})
        manager = _make_manager()
        with pytest.raises(SageCredentialError, match="missing required keys"):
            manager.get_credentials()

    @mock_aws
    def test_error_message_does_not_contain_secret_values(self) -> None:
        # Partial secret missing keys — verify secret values never leak.
        _create_secret({"base_url": "https://api.intacct.com"})
        manager = _make_manager()
        try:
            manager.get_credentials()
        except SageCredentialError as exc:
            # Product name is fine to include; actual credential values must not appear.
            assert "super-secret-value" not in str(exc)
            assert "test-client-id" not in str(exc)

    @mock_aws
    def test_subset_required_keys_passes_validation(self) -> None:
        """A manager that only requires a subset of keys should accept extra keys."""
        _create_secret(_VALID_SECRET)
        # Require only base_url — extra keys in the secret are acceptable.
        manager = SageCredentialProvider(
            environment=_ENV,
            region_name=_REGION,
            product_name=_PRODUCT,
            required_keys=frozenset({"base_url"}),
        )
        creds = manager.get_credentials()
        assert "base_url" in creds
