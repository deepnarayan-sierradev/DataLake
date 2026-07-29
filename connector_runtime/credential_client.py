"""
Shared AWS Secrets Manager credential client (DUP-2 / DP-2).

Every source adapter that reads credentials from AWS Secrets Manager previously
hand-rolled the same boilerplate: ``boto3.client("secretsmanager")`` construction,
the ``get_secret_value`` call, ``ClientError`` → custom credential-error mapping,
``json.loads`` with ``JSONDecodeError`` handling, required-key validation, and a
TTL-based in-memory cache.  Sage already solved this with ``SageCredentialProvider``;
this module promotes that proven pattern to a single, parameterized base so that
Salesforce, NetSuite, MySQL RDS, and Sage all share one implementation.

Secret path convention is caller-owned: each adapter resolves its own
``secret_id`` (e.g. ``edl/sources/salesforce/credentials``) and passes
it in.  This client only knows how to fetch, parse, validate, and cache — it
never constructs source-specific secret paths itself.

Security (OWASP A07, A09):
  - Credential values are NEVER logged or included in exception messages —
    only the (non-sensitive) source label and the AWS error code appear.
  - The boto3 client uses the implicit IAM role credential chain — no keys.
  - Cache TTL enforces periodic re-fetch so that Secrets Manager rotation
    takes effect within ``cache_ttl_seconds`` without a Lambda restart.
"""

from __future__ import annotations

import json
import time
from typing import Any, Final

import boto3
from botocore.exceptions import ClientError

from contracts.platform_metrics import PlatformMetric
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# Re-fetch credentials this often to pick up Secrets Manager rotation (OWASP A07).
# Reduced from one hour to five minutes (DL-CFG-06): the previous bound meant a rotated
# or corrected credential could wait an hour, and it was unalarmed. Sized against the
# per-run invocation rate rather than picked as a round number — one extra
# GetSecretValue per connection per five minutes is negligible against the Secrets
# Manager request budget, and `force_refresh()` covers the rotation case outright.
DEFAULT_CREDENTIAL_CACHE_TTL_SECONDS: Final[int] = 300


class SecretsManagerCredentialError(Exception):
    """
    Base class for credential-retrieval failures from AWS Secrets Manager.

    Each adapter's own credential-error type (SalesforceCredentialError,
    NetSuiteCredentialError, MySqlRdsCredentialError, SageCredentialError)
    subclasses this so callers may catch either the shared base or the
    adapter-specific type.
    """


class SecretsManagerCredentialClient:
    """
    Retrieves and caches a JSON secret from AWS Secrets Manager.

    Parameterized by the resolved ``secret_id``, the set of ``required_keys``
    the secret must contain, a human-readable ``source_label`` used only in
    (credential-free) error messages, and the concrete ``error_cls`` to raise
    so each adapter preserves its own exception type.

    One instance should be shared across all components within an extraction
    run to avoid redundant Secrets Manager calls.

    Usage::

        creds_client = SecretsManagerCredentialClient(
            secret_id="edl/sources/salesforce/credentials",
            region_name="us-east-1",
            required_keys=frozenset({"instance_url", "client_id", "client_secret"}),
            source_label="Salesforce",
            error_cls=SalesforceCredentialError,
        )
        creds = creds_client.get_credentials()
        # → {"instance_url": "https://...", "client_id": "...", ...}
    """

    def __init__(
        self,
        *,
        secret_id: str,
        region_name: str,
        required_keys: frozenset[str],
        source_label: str,
        error_cls: type[Exception] = SecretsManagerCredentialError,
        cache_ttl_seconds: int = DEFAULT_CREDENTIAL_CACHE_TTL_SECONDS,
        log_event: str | None = None,
        log_fields: dict[str, Any] | None = None,
    ) -> None:
        if not secret_id:
            raise ValueError("secret_id must not be empty.")
        if not required_keys:
            raise ValueError("required_keys must not be empty.")

        self._secret_id = secret_id
        self._required_keys = required_keys
        self._source_label = source_label
        self._error_cls = error_cls
        self._cache_ttl_seconds = cache_ttl_seconds
        self._log_event = log_event
        self._log_fields = log_fields or {}
        self._secrets_client = boto3.client("secretsmanager", region_name=region_name)

        # Cache state — populated lazily on first get_credentials() call.
        self._cached: dict[str, str] | None = None
        self._loaded_at: float = 0.0  # monotonic timestamp of last successful fetch

    def get_credentials(self) -> dict[str, str]:
        """
        Return the secret's key/value pairs as a plain dict.

        Fetches from Secrets Manager on first call and when the TTL has expired.
        Subsequent calls within the TTL window return the cached copy immediately
        (the same dict object, so identity is stable across cache hits).

        Raises:
            self._error_cls: secret absent, insufficient permissions, malformed
                JSON, or required keys missing from the secret.
        """
        if self._cached is not None and not self._is_cache_expired():
            record_platform_metric(
                PlatformMetric.CREDENTIAL_CACHE_PROPAGATION_LAG_SECONDS, self.cache_age_seconds
            )
            return self._cached

        self._cached = self._fetch_from_secrets_manager()
        self._loaded_at = time.monotonic()

        if self._log_event is not None:
            _logger.info(self._log_event, **self._log_fields)

        return self._cached

    def invalidate_cache(self) -> None:
        """Force the next get_credentials() call to re-fetch from Secrets Manager."""
        self._cached = None
        self._loaded_at = 0.0

    def force_refresh(self) -> dict[str, str]:
        """Re-fetch immediately — the rotated-credential path must not wait for the TTL."""
        record_platform_metric(
            PlatformMetric.CREDENTIAL_CACHE_PROPAGATION_LAG_SECONDS, self.cache_age_seconds
        )
        self.invalidate_cache()
        return self.get_credentials()

    @property
    def cache_age_seconds(self) -> float:
        """Observed propagation lag for `CredentialCachePropagationLagSeconds` (DL-CFG-06)."""
        if self._cached is None:
            return 0.0
        return max(0.0, time.monotonic() - self._loaded_at)

    @property
    def cache_ttl_seconds(self) -> int:
        """The declared TTL bound; the invalidation basis is TTL-bounded (DL-CFG-04)."""
        return self._cache_ttl_seconds

    # ── Private ────────────────────────────────────────────────────────────────

    def _is_cache_expired(self) -> bool:
        return (time.monotonic() - self._loaded_at) >= self._cache_ttl_seconds

    def _fetch_from_secrets_manager(self) -> dict[str, str]:
        """
        Fetch, parse, and validate the secret from AWS Secrets Manager.

        Raises:
            self._error_cls: on any Secrets Manager error or malformed secret.
            Credential values never appear in the raised message.
        """
        try:
            response = self._secrets_client.get_secret_value(SecretId=self._secret_id)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "Unknown")
            record_platform_metric(PlatformMetric.SECRET_RETRIEVAL_FAILURES)
            raise self._error_cls(
                f"Failed to retrieve {self._source_label} credentials "
                f"from Secrets Manager: {error_code}"
            ) from None

        raw_secret = response.get("SecretString")
        if not raw_secret:
            raise self._error_cls(
                f"{self._source_label} secret is present but contains no SecretString value."
            )

        try:
            credentials: dict[str, str] = json.loads(raw_secret)
        except (json.JSONDecodeError, ValueError):
            raise self._error_cls(f"{self._source_label} secret value is not valid JSON.") from None

        missing = self._required_keys - credentials.keys()
        if missing:
            raise self._error_cls(
                f"{self._source_label} credentials secret is missing required keys: "
                f"{sorted(missing)}. "
                f"Expected keys: {sorted(self._required_keys)}."
            )

        return credentials
