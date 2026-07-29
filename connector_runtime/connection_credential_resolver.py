"""
Per-connection credential path resolution (DL-SEC-05, DL-SCOPE-06).

The correct grain is per connection, not per source: twelve franchisees on HubSpot need
twelve credential sets for one `source_id`. During the migration a connection whose
per-connection secret does not yet exist falls back to the legacy shared per-source path
once, with a warning, so onboarding is never blocked by migration ordering.

Security (OWASP A02, A07): the fallback is read-only and logged, never silent, and the
write-back secret is always a separate path so a read-only deployment cannot mutate a
source.
"""

from __future__ import annotations

from dataclasses import dataclass

from contracts.platform_metrics import PlatformMetric
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger
from tenancy.source_connection import (
    connection_credential_path,
    connection_writeback_credential_path,
)

_logger = get_platform_logger(__name__)


def legacy_source_credential_path(source_id: str) -> str:
    """The pre-DL-SEC-05 shared path, retained only as a migration fallback."""
    return f"edl/sources/{source_id}/credentials"


@dataclass(frozen=True)
class ResolvedCredentialPath:
    """The secret path chosen, and whether it was the legacy shared one."""

    secret_id: str
    is_legacy_shared: bool


class ConnectionCredentialPathResolver:
    """Chooses the per-connection secret path, falling back only when it is absent."""

    def __init__(self, secrets_client: object, allow_legacy_fallback: bool = True) -> None:
        self._secrets = secrets_client
        self._allow_legacy_fallback = allow_legacy_fallback

    def resolve(
        self, tenant_code: str, source_id: str, connection_id: str, *, write_back: bool = False
    ) -> ResolvedCredentialPath:
        preferred = (
            connection_writeback_credential_path(tenant_code, connection_id)
            if write_back
            else connection_credential_path(tenant_code, connection_id)
        )
        if self._secret_exists(preferred):
            return ResolvedCredentialPath(secret_id=preferred, is_legacy_shared=False)
        if write_back or not self._allow_legacy_fallback:
            # A write-back secret is never shared across tenants — fail closed instead.
            return ResolvedCredentialPath(secret_id=preferred, is_legacy_shared=False)
        record_platform_metric(
            PlatformMetric.CONNECTION_CREDENTIAL_FAILURES, 1.0, ConnectionId=connection_id
        )
        legacy = legacy_source_credential_path(source_id)
        _logger.warning(
            "connection_credential_legacy_path_used",
            tenant_code=tenant_code,
            connection_id=connection_id,
            source_id=source_id,
            message=(
                "Per-connection secret absent; using the shared per-source path. Run "
                "scripts/migrate_credentials_to_connection_paths.py --apply to close this."
            ),
        )
        return ResolvedCredentialPath(secret_id=legacy, is_legacy_shared=True)

    def _secret_exists(self, secret_id: str) -> bool:
        describe = getattr(self._secrets, "describe_secret", None)
        if describe is None:
            return False
        try:
            describe(SecretId=secret_id)
        except Exception:
            return False
        return True
