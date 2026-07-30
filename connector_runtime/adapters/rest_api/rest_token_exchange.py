"""
Exchange of a long-lived stored secret for the short-lived token a source actually accepts.

Three of the documented sources hand out a token that expires inside a single extraction
window — MaidCentral (1 hour), WellSky Personal Care Connect (OAuth 2.0), and ServiceBridge
(a `sessionKey` with a 30-minute sliding expiry). Without this the connector authenticates
once at cold start and every subsequent page 401s halfway through a long entity, which the
error taxonomy correctly classifies as a *deterministic credential* failure — so the run
fails and no retry can fix it.

Security (OWASP A02, A07, A09): the exchange is HTTPS-only against the source's own
allowlisted host, the request body is never logged, and the token is held in memory for the
life of the invocation only — it is never written back to Secrets Manager, so a compromised
runtime cannot persist a credential it obtained.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any, Final
from urllib.parse import urljoin, urlparse

import requests

from connector_runtime.adapters.rest_api.rest_source_spec import (
    RestSourceSpec,
    TokenGrantKind,
)
from connector_runtime.interfaces.connector_interface import (
    DeterministicConnectorError,
    ExtractionErrorClassification,
    TransientConnectorError,
)
from connector_runtime.source_capabilities import enforce_allowed_host
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

TOKEN_RENEWAL_MARGIN_SECONDS: Final[float] = 120.0

DEFAULT_TOKEN_LIFETIME_SECONDS: Final[float] = 1_800.0

TOKEN_REQUEST_TIMEOUT_SECONDS: Final[float] = 15.0

_TOKEN_FIELDS: Final[tuple[str, ...]] = (
    "access_token",
    "accessToken",
    "sessionKey",
    "SessionKey",
    "token",
)


class TokenExchangeFailedError(DeterministicConnectorError):
    """The stored secret was rejected — retrying cannot fix a bad client id or password."""

    classification = ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS


class TokenEndpointUnavailableError(TransientConnectorError):
    """The token endpoint itself is down or throttled — the extraction may be retried."""

    classification = ExtractionErrorClassification.TRANSIENT_NETWORK


class RestTokenExchange:
    """Obtains and caches the short-lived token for one connection."""

    def __init__(
        self,
        spec: RestSourceSpec,
        credentials: Mapping[str, str],
        *,
        session: Any | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if not spec.token_endpoint_path or spec.token_grant_kind is None:
            raise ValueError(
                f"Source {spec.source_id!r} declares no token endpoint; construct a token "
                "exchange only for a source that needs one."
            )
        self._spec = spec
        self._credentials = dict(credentials)
        self._session = session or requests.Session()
        self._monotonic = monotonic or time.monotonic
        self._token: str | None = None
        self._expires_at: float = 0.0
        self.exchanges_performed = 0

    def token(self) -> str:
        """The current token, refreshed when it is within the renewal margin of expiry."""
        if self._token is not None and self._monotonic() < self._expires_at:
            return self._token
        return self._refresh()

    def invalidate(self) -> None:
        """Drop the cached token so the next call re-exchanges — used after a 401."""
        self._token = None
        self._expires_at = 0.0

    def _refresh(self) -> str:
        payload = self._grant_payload()
        url = self._token_url()
        enforce_allowed_host(self._spec.source_id, urlparse(url).netloc)
        try:
            response = self._session.request(
                "POST",
                url,
                data=payload if self._is_form_encoded() else None,
                json=None if self._is_form_encoded() else payload,
                headers=self._request_headers(),
                timeout=TOKEN_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise TokenEndpointUnavailableError(
                f"{self._spec.source_id}: token endpoint unreachable: {type(exc).__name__}"
            ) from None

        status = int(getattr(response, "status_code", 0))
        if status in (400, 401, 403):
            raise TokenExchangeFailedError(
                f"{self._spec.source_id}: the stored credential was rejected by the token "
                f"endpoint (HTTP {status}). Rotate the secret; retrying will not help."
            )
        if status >= 400:
            raise TokenEndpointUnavailableError(
                f"{self._spec.source_id}: token endpoint returned HTTP {status}."
            )

        body = _decode(response)
        token = _first_present(body, _TOKEN_FIELDS)
        if not token:
            raise TokenExchangeFailedError(
                f"{self._spec.source_id}: the token endpoint returned no recognised token "
                f"field. Expected one of {list(_TOKEN_FIELDS)}."
            )
        lifetime = _lifetime_seconds(body)
        self._token = token
        self._expires_at = self._monotonic() + max(0.0, lifetime - TOKEN_RENEWAL_MARGIN_SECONDS)
        self.exchanges_performed += 1
        _logger.info(
            "rest_source_token_exchanged",
            source_id=self._spec.source_id,
            grant_kind=str(self._spec.token_grant_kind),
            lifetime_seconds=lifetime,
        )
        return token

    def _token_url(self) -> str:
        base = self._spec.base_url
        base = base if base.endswith("/") else base + "/"
        return urljoin(base, str(self._spec.token_endpoint_path).lstrip("/"))

    def _is_form_encoded(self) -> bool:
        return self._spec.token_grant_kind is not TokenGrantKind.SESSION_LOGIN

    def _request_headers(self) -> dict[str, str]:
        if self._is_form_encoded():
            return {
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            }
        return {"Accept": "application/json", "Content-Type": "application/json"}

    def _grant_payload(self) -> dict[str, str]:
        kind = self._spec.token_grant_kind
        if kind is TokenGrantKind.CLIENT_CREDENTIALS:
            return {
                "grant_type": "client_credentials",
                "client_id": self._require("client_id"),
                "client_secret": self._require("client_secret"),
            }
        if kind is TokenGrantKind.SESSION_LOGIN:
            return {
                "userId": self._require("user_id"),
                "password": self._require("password"),
            }
        if kind is TokenGrantKind.REFRESH_TOKEN or self._credentials.get("refresh_token"):
            return {
                "grant_type": "refresh_token",
                "username": self._credentials.get("username", ""),
                "refresh_token": self._require("refresh_token"),
            }
        return {
            "grant_type": "password",
            "username": self._require("username"),
            "password": self._require("password"),
        }

    def _require(self, key: str) -> str:
        value = self._credentials.get(key, "")
        if not value:
            raise TokenExchangeFailedError(
                f"{self._spec.source_id}: the stored secret has no {key!r}, which its "
                f"{self._spec.token_grant_kind} grant requires."
            )
        return value


def _decode(response: Any) -> Mapping[str, Any]:
    text = getattr(response, "text", "") or ""
    if not text.strip():
        return {}
    import json

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        raise TokenExchangeFailedError("The token endpoint returned a non-JSON body.") from None
    return parsed if isinstance(parsed, Mapping) else {}


def _first_present(body: Mapping[str, Any], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = body.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _lifetime_seconds(body: Mapping[str, Any]) -> float:
    for name in ("expires_in", "expiresIn", "expires_in_seconds"):
        raw = body.get(name)
        try:
            if raw is not None:
                return float(raw)
        except (TypeError, ValueError):
            continue
    return DEFAULT_TOKEN_LIFETIME_SECONDS
