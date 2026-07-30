"""
Tests for MySqlRdsCredentialsClient.

Coverage:
  - Credential loading from Secrets Manager (happy path)
  - Credentials cached after first load
  - Missing secret → MySqlRdsCredentialError
  - Invalid JSON → MySqlRdsCredentialError
  - Missing required keys → MySqlRdsCredentialError
  - Non-integer port → MySqlRdsCredentialError
  - Password never appears in log output (OWASP A09)
"""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from conftest import RESOURCE_NAME_ENVIRONMENT
from connector_runtime.adapters.mysql_rds.mysql_rds_credentials_client import (
    MySqlConnectionParameters,
    MySqlRdsCredentialError,
    MySqlRdsCredentialsClient,
)

_ENV = "dev"
_REGION = "us-east-1"
_SECRET_NAME = f"{RESOURCE_NAME_ENVIRONMENT['SECRET_PATH_PREFIX']}/sources/mysql-rds/credentials"

_VALID_SECRET = {
    "host": "mydb.cluster.us-east-1.rds.amazonaws.com",
    "port": "3306",
    "username": "extraction_user",
    "password": "s3cr3t-pass",
    "database": "production",
}


def _create_secret(payload: dict | str) -> None:
    client = boto3.client("secretsmanager", region_name=_REGION)
    body = payload if isinstance(payload, str) else json.dumps(payload)
    client.create_secret(Name=_SECRET_NAME, SecretString=body)


class TestCredentialLoading:
    @mock_aws
    def test_returns_typed_connection_parameters(self) -> None:
        _create_secret(_VALID_SECRET)
        client = MySqlRdsCredentialsClient(environment=_ENV, region_name=_REGION)
        params = client.get_connection_parameters()
        assert isinstance(params, MySqlConnectionParameters)
        assert params.host == _VALID_SECRET["host"]
        assert params.port == 3306
        assert params.username == _VALID_SECRET["username"]
        assert params.database == _VALID_SECRET["database"]

    @mock_aws
    def test_parameters_cached_on_second_call(self) -> None:
        _create_secret(_VALID_SECRET)
        creds_client = MySqlRdsCredentialsClient(environment=_ENV, region_name=_REGION)
        p1 = creds_client.get_connection_parameters()
        p2 = creds_client.get_connection_parameters()
        assert p1 is p2  # Same object — cached

    @mock_aws
    def test_secret_not_found_raises_credential_error(self) -> None:
        client = MySqlRdsCredentialsClient(environment=_ENV, region_name=_REGION)
        with pytest.raises(MySqlRdsCredentialError, match="Secrets Manager"):
            client.get_connection_parameters()

    @mock_aws
    def test_invalid_json_raises_credential_error(self) -> None:
        _create_secret("NOT JSON {{{")
        client = MySqlRdsCredentialsClient(environment=_ENV, region_name=_REGION)
        with pytest.raises(MySqlRdsCredentialError, match="not valid JSON"):
            client.get_connection_parameters()

    @mock_aws
    def test_missing_keys_raises_credential_error(self) -> None:
        _create_secret({"host": "myhost"})  # missing port, username, etc.
        client = MySqlRdsCredentialsClient(environment=_ENV, region_name=_REGION)
        with pytest.raises(MySqlRdsCredentialError, match="missing required keys"):
            client.get_connection_parameters()

    @mock_aws
    def test_non_integer_port_raises_credential_error(self) -> None:
        payload = {**_VALID_SECRET, "port": "not-a-number"}
        _create_secret(payload)
        client = MySqlRdsCredentialsClient(environment=_ENV, region_name=_REGION)
        with pytest.raises(MySqlRdsCredentialError, match="port"):
            client.get_connection_parameters()

    def test_empty_environment_raises(self) -> None:
        with pytest.raises(ValueError, match="environment"):
            MySqlRdsCredentialsClient(environment="", region_name=_REGION)


class TestSecurityRequirements:
    @mock_aws
    def test_password_not_in_log_output(self, caplog: pytest.LogCaptureFixture) -> None:
        """Password must never appear in our application log records (OWASP A09).

        Note: botocore DEBUG logs the raw Secrets Manager HTTP response, which
        includes the full secret JSON. We intentionally filter to only our own
        application logger records — botocore's wire-level debug output is outside
        the platform's logging contract.
        """
        _create_secret(_VALID_SECRET)
        import logging

        with caplog.at_level(logging.DEBUG):
            client = MySqlRdsCredentialsClient(environment=_ENV, region_name=_REGION)
            client.get_connection_parameters()
        app_records = [r for r in caplog.records if r.name.startswith("connector_runtime")]
        for record in app_records:
            assert _VALID_SECRET["password"] not in record.getMessage(), (
                f"Password found in application log record: {record.getMessage()!r}"
            )

    @mock_aws
    def test_connection_parameters_is_frozen(self) -> None:
        _create_secret(_VALID_SECRET)
        client = MySqlRdsCredentialsClient(environment=_ENV, region_name=_REGION)
        params = client.get_connection_parameters()
        with pytest.raises((AttributeError, TypeError)):
            params.host = "changed"  # type: ignore[misc]

    @mock_aws
    def test_password_not_in_repr(self) -> None:
        """MySqlConnectionParameters.__repr__ must not expose the password (OWASP A01)."""
        _create_secret(_VALID_SECRET)
        client = MySqlRdsCredentialsClient(environment=_ENV, region_name=_REGION)
        params = client.get_connection_parameters()
        params_repr = repr(params)
        assert _VALID_SECRET["password"] not in params_repr
        assert "password" not in params_repr
