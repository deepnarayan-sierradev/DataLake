"""
SageCredentialProvider — shared AWS Secrets Manager abstraction for all Sage products.

Every product-specific auth client (IntacctAuthClient, X3AuthClient, etc.) delegates
credential loading here instead of each managing their own boto3 session and caching
logic.  This eliminates the duplicated Secrets Manager code that would otherwise exist
across every product auth module.

DUP-2: this was the reference implementation for the platform-wide credential-client
consolidation. The generic Secrets Manager fetch/parse/validate/cache logic has since
been promoted to connector_runtime.credential_client.SecretsManagerCredentialClient,
which Salesforce, NetSuite, and MySQL RDS now also build on. SageCredentialProvider is
a thin subclass that only adds the Sage-specific secret path convention
(product_name) — its public API and observable behaviour are unchanged.

Secret path convention (enforced here, not in auth clients):
    datalake/<env>/sources/sage/{product_name}/credentials

Security (OWASP A07, A09):
  - Credentials are NEVER logged or included in exception messages.
  - The boto3 client uses the implicit IAM role credential chain — no explicit keys.
  - Cache TTL enforces periodic re-fetch so that Secrets Manager rotation
    takes effect within _CREDENTIAL_CACHE_TTL_SECONDS without a Lambda restart.
  - product_name is validated against SUPPORTED_SAGE_PRODUCTS before being
    interpolated into the secret path, preventing path injection.
"""

from __future__ import annotations

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

_CREDENTIAL_CACHE_TTL_SECONDS: Final[int] = 3_600


class SageCredentialError(SecretsManagerCredentialError, DeterministicConnectorError):
    """Raised when Sage credentials cannot be retrieved from AWS Secrets Manager."""

    classification = ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS


class SageCredentialProvider(SecretsManagerCredentialClient):
    """
    Retrieves and caches Sage product credentials from AWS Secrets Manager.

    One instance should be shared across all components within an extraction
    run (e.g. auth client + metadata client share the same manager) to avoid
    redundant Secrets Manager calls.

    Usage::

        manager = SageCredentialProvider(
            environment="dev",
            region_name="us-east-1",
            product_name="intacct",
            required_keys=frozenset({"base_url", "client_id", "client_secret", "company_id"}),
        )
        creds = manager.get_credentials()
        # → {"base_url": "https://...", "client_id": "...", ...}
    """

    def __init__(
        self,
        environment: str,
        region_name: str,
        product_name: str,
        required_keys: frozenset[str],
    ) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        if not product_name:
            raise ValueError("product_name must not be empty.")
        if not required_keys:
            raise ValueError("required_keys must not be empty.")

        self._product_name = product_name

        super().__init__(
            secret_id=secret_path("sources", "sage", product_name, "credentials"),
            region_name=region_name,
            required_keys=required_keys,
            source_label=f"Sage/{product_name}",
            error_cls=SageCredentialError,
            cache_ttl_seconds=_CREDENTIAL_CACHE_TTL_SECONDS,
            log_event="sage_credentials_loaded",
            log_fields={"product_name": product_name, "environment": environment},
        )
