"""
MySQL RDS credentials client.

Retrieves MySQL RDS connection parameters exclusively from AWS Secrets Manager
and presents them as a typed, frozen dataclass.  The password is never logged
or included in any exception message (OWASP A07, A09).

Credential storage:
  - Secret path: datalake/<env>/sources/mysql-rds/credentials
  - Expected JSON keys: host, port, username, password, database

Private VPC connectivity:
  - The MySQL RDS instance is deployed in a private subnet with no public
    endpoint.  Connections originate from the extraction runtime Lambda or
    ECS task running inside the same VPC (or a peered VPC with routing).
  - SSL is enforced via the pymysql ssl_ca parameter at connection time.

Security (OWASP A02, A07, A09):
  - Credentials loaded from Secrets Manager only — not from env vars or code.
  - Password absent from all log events and exception messages.
  - MySqlConnectionParameters is frozen — no mutation after construction.

Credential retrieval (DUP-2) is delegated to the shared
SecretsManagerCredentialClient rather than hand-rolling boto3/Secrets Manager
boilerplate here — see connector_runtime/credential_client.py. The 'port'
field still requires MySQL-specific int coercion beyond what the shared
client validates (required-key presence + JSON parsing), so that step
remains here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Final

from connector_runtime.credential_client import (
    SecretsManagerCredentialClient,
    SecretsManagerCredentialError,
)
from connector_runtime.interfaces.connector_interface import (
    DeterministicConnectorError,
    ExtractionErrorClassification,
)
from contracts.resource_naming import secret_path
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

_DEFAULT_PORT: Final[int] = 3306
_CREDENTIAL_CACHE_TTL_SECONDS: Final[int] = 3_600

_REQUIRED_CREDENTIAL_KEYS: Final[frozenset[str]] = frozenset(
    {"host", "port", "username", "password", "database"}
)


class MySqlRdsCredentialError(SecretsManagerCredentialError, DeterministicConnectorError):
    """Raised when MySQL RDS credentials cannot be retrieved from Secrets Manager."""

    classification = ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS


@dataclass(frozen=True)
class MySqlConnectionParameters:
    """
    Typed, immutable MySQL RDS connection parameters.

    Constructed only from Secrets Manager values — never from constructor
    arguments, environment variables, or configuration files.

    The password field is present here for use by pymysql.connect() only.
    It must NEVER be included in log events, exception messages, or
    any other observable output.
    """

    host: str
    port: int
    username: str
    password: str = field(repr=False)  # repr=False prevents password leaking in str()/repr() calls
    database: str


class MySqlRdsCredentialsClient:
    """
    Loads MySQL RDS connection parameters from AWS Secrets Manager.

    Credentials are loaded lazily on the first get_connection_parameters()
    call and cached in-memory for the lifetime of the instance.

    Usage::

        creds_client = MySqlRdsCredentialsClient(
            environment="dev",
            region_name="us-east-1",
        )
        params = creds_client.get_connection_parameters()
        # Use params.host, params.port, params.username, params.password,
        # params.database with pymysql.connect()
    """

    def __init__(self, environment: str, region_name: str) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        self._environment = environment
        self._region = region_name
        self._credentials_client = SecretsManagerCredentialClient(
            secret_id=secret_path("sources", "mysql-rds", "credentials"),
            region_name=region_name,
            required_keys=_REQUIRED_CREDENTIAL_KEYS,
            source_label="MySQL RDS",
            error_cls=MySqlRdsCredentialError,
            cache_ttl_seconds=_CREDENTIAL_CACHE_TTL_SECONDS,
        )
        self._cached_params: MySqlConnectionParameters | None = None
        self._cached_at: float = 0.0  # monotonic timestamp of last successful conversion

    def get_connection_parameters(self) -> MySqlConnectionParameters:
        """
        Return MySQL RDS connection parameters from Secrets Manager.

        Parameters are cached in-memory and refreshed after
        _CREDENTIAL_CACHE_TTL_SECONDS to honour automatic secret rotation.

        Raises:
            MySqlRdsCredentialError: secret absent, access denied, or malformed JSON.
        """
        now = time.monotonic()
        if (
            self._cached_params is not None
            and (now - self._cached_at) < _CREDENTIAL_CACHE_TTL_SECONDS
        ):
            return self._cached_params

        self._cached_params = self._build_connection_parameters()
        self._cached_at = time.monotonic()
        return self._cached_params

    def _build_connection_parameters(self) -> MySqlConnectionParameters:
        """
        Fetch the validated credential dict and coerce it into a typed
        MySqlConnectionParameters, applying the MySQL-specific 'port' int
        conversion the shared credential client does not know about.

        Raises:
            MySqlRdsCredentialError: on Secrets Manager error, missing/malformed
                secret, or a non-integer 'port' value.
        """
        payload = self._credentials_client.get_credentials()

        try:
            port = int(payload["port"])
        except (ValueError, TypeError) as exc:
            raise MySqlRdsCredentialError(
                "MySQL RDS credentials secret 'port' value is not a valid integer."
            ) from exc

        params = MySqlConnectionParameters(
            host=str(payload["host"]),
            port=port,
            username=str(payload["username"]),
            password=str(payload["password"]),
            database=str(payload["database"]),
        )

        _logger.info(
            "mysql_rds_credentials_loaded",
            environment=self._environment,
            host=params.host,
            database=params.database,
        )
        return params
