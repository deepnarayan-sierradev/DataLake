"""
Shared Google OAuth client for Google Ads and Google Analytics 4 (DL-CONN-06).

Two registered sources, one credential client — the reuse clause in DL-01 names this pair
explicitly. Lives under `google_ads/` and is imported by `google_analytics/`, which is the
one deliberate exception to the no-cross-adapter-import rule: the alternative is two copies
of an OAuth refresh flow, and the rule exists to prevent duplication, not to require it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from connector_runtime.credential_client import (
    SecretsManagerCredentialClient,
    SecretsManagerCredentialError,
)

GOOGLE_OAUTH_TOKEN_HOST: Final[str] = "oauth2.googleapis.com"  # noqa: S105 — a hostname, and the name ends in "TOKEN_HOST"; the token itself comes from Secrets Manager

GOOGLE_REQUIRED_CREDENTIAL_KEYS: Final[frozenset[str]] = frozenset(
    {"client_id", "client_secret", "refresh_token", "access_token"}
)


class GoogleCredentialError(SecretsManagerCredentialError):
    """Raised when a Google credential secret is absent or incomplete."""


def google_credential_client(
    secret_id: str, region_name: str, source_label: str
) -> SecretsManagerCredentialClient:
    """One credential client shape for both Google sources."""
    return SecretsManagerCredentialClient(
        secret_id=secret_id,
        region_name=region_name,
        required_keys=GOOGLE_REQUIRED_CREDENTIAL_KEYS,
        source_label=source_label,
        error_cls=GoogleCredentialError,
        log_event="google_credentials_loaded",
        log_fields={"source_label": source_label},
    )


def refresh_request_payload(credentials: Mapping[str, str]) -> dict[str, str]:
    """
    The OAuth refresh body, built from the stored secret.

    Returned rather than posted so the caller owns the HTTP call and its rate-limit policy;
    this function never touches the network and never logs.
    """
    missing = GOOGLE_REQUIRED_CREDENTIAL_KEYS - credentials.keys()
    if missing:
        raise GoogleCredentialError(
            f"Google credential secret is missing required keys: {sorted(missing)}."
        )
    return {
        "client_id": credentials["client_id"],
        "client_secret": credentials["client_secret"],
        "refresh_token": credentials["refresh_token"],
        "grant_type": "refresh_token",
    }
