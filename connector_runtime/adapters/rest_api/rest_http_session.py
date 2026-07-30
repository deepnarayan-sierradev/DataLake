"""
Rate-limited, allowlisted HTTP session shared by every REST-source adapter.

Security (OWASP A02, A09, A10): TLS only, outbound hosts allowlisted from the source's own
capability declaration, and no credential ever appears in a log line — the session logs
method, path, and status class, never headers or bodies.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urljoin, urlparse

import requests

from connector_runtime.adapters.rest_api.rest_source_spec import AuthKind, RestSourceSpec
from connector_runtime.adapters.rest_api.rest_token_exchange import RestTokenExchange
from connector_runtime.interfaces.connector_interface import (
    DeterministicConnectorError,
    ExtractionErrorClassification,
    TransientConnectorError,
)
from connector_runtime.rate_limiting import RateLimitPolicy
from connector_runtime.source_capabilities import enforce_allowed_host
from contracts.platform_metrics import PlatformMetric
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0


class RestSourceTransientError(TransientConnectorError):
    """5xx, 429, or a network failure — retry-eligible."""

    classification = ExtractionErrorClassification.TRANSIENT_NETWORK


class RestSourceThrottledError(TransientConnectorError):
    """429 specifically, so the retry policy can back off rather than retry immediately."""

    classification = ExtractionErrorClassification.TRANSIENT_THROTTLE


class RestSourceCredentialError(DeterministicConnectorError):
    """401/403 — retrying cannot fix a bad or revoked credential."""

    classification = ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS


class RestSourceObjectError(DeterministicConnectorError):
    """404 on a declared endpoint — the entity or endpoint does not exist."""

    classification = ExtractionErrorClassification.DETERMINISTIC_INVALID_OBJECT


class RestSourceRequestError(DeterministicConnectorError):
    """4xx other than auth/not-found — a malformed request the source rejected."""

    classification = ExtractionErrorClassification.DETERMINISTIC_INVALID_CONFIGURATION


@dataclass
class RestResponse:
    """Status, parsed body, and headers, with credentials already excluded."""

    status_code: int
    body: Any
    headers: dict[str, str]

    def records(
        self, json_path: tuple[str, ...], unwrap_field: str | None = None
    ) -> list[dict[str, Any]]:
        """Walk the declared JSON path to the record list; a scalar leaf becomes one record."""
        node: Any = self.body
        for segment in json_path:
            if isinstance(node, Mapping):
                node = node.get(segment)
            else:
                node = None
            if node is None:
                return []
        if isinstance(node, list):
            found = [item for item in node if isinstance(item, dict)]
        elif isinstance(node, dict):
            # An empty object is no record. It reaches here only for a source whose records
            # are the body itself (an empty `records_json_path`), where a blank response
            # parses to `{}` — yielding it would write a field-less row to the raw layer and
            # poison the schema fingerprint.
            found = [node] if node else []
        else:
            return []
        if unwrap_field is None:
            return found
        # A FHIR search bundle nests each row one level down under `resource`; an entry that
        # carries no wrapper is dropped rather than stored as an envelope masquerading as a
        # record, which would poison the raw layer's schema fingerprint.
        return [item[unwrap_field] for item in found if isinstance(item.get(unwrap_field), dict)]


class RestHttpSession:
    """One session per extraction run; acquires the rate-limit policy before every call."""

    def __init__(
        self,
        spec: RestSourceSpec,
        credentials: Mapping[str, str],
        rate_limit_policy: RateLimitPolicy,
        *,
        timeout_seconds: float | None = None,
        session: Any | None = None,
    ) -> None:
        self._spec = spec
        self._credentials = dict(credentials)
        self._rate_limit = rate_limit_policy
        # The source's declared timeout unless a caller overrides it; a slow report source
        # and a fast row collection should not share one number.
        self._timeout = (
            timeout_seconds if timeout_seconds is not None else spec.request_timeout_seconds
        )
        self._session = session or requests.Session()
        self._token_exchange = (
            RestTokenExchange(spec, credentials, session=self._session)
            if spec.token_endpoint_path
            else None
        )
        self.requests_issued = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def get(self, path: str, parameters: Mapping[str, Any] | None = None) -> RestResponse:
        return self._request("GET", path, parameters=parameters)

    def post(
        self,
        path: str,
        payload: Mapping[str, Any] | None = None,
        parameters: Mapping[str, Any] | None = None,
    ) -> RestResponse:
        return self._request("POST", path, parameters=parameters, payload=payload)

    def patch(self, path: str, payload: Mapping[str, Any] | None = None) -> RestResponse:
        return self._request("PATCH", path, payload=payload)

    # ── Private ───────────────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        parameters: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> RestResponse:
        response = self._issue(method, path, parameters, payload)
        if response.status_code == 401 and self._token_exchange is not None:
            # A token that expired mid-entity is not a bad credential: re-exchange once and
            # retry, so a long extraction is not failed by its own duration. Exactly one
            # retry — a genuinely revoked credential must still surface as deterministic.
            self._token_exchange.invalidate()
            response = self._issue(method, path, parameters, payload)
        self._raise_for_status(response.status_code, method, path)
        return response

    def _issue(
        self,
        method: str,
        path: str,
        parameters: Mapping[str, Any] | None,
        payload: Mapping[str, Any] | None,
    ) -> RestResponse:
        url = self._resolve_url(path)
        enforce_allowed_host(self._spec.source_id, urlparse(url).netloc)
        self._rate_limit.acquire()
        query = {**dict(parameters or {}), **self._auth_query_parameters()}
        try:
            raw = self._session.request(
                method,
                url,
                params=query,
                json=dict(payload) if payload is not None else None,
                headers=self._auth_headers(),
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise RestSourceTransientError(
                f"{self._spec.source_id}: network failure calling {method} {path}: "
                f"{type(exc).__name__}"
            ) from None
        self.requests_issued += 1

        headers = {str(k): str(v) for k, v in dict(raw.headers or {}).items()}
        # The policy needs the status to recognise a throttle even when the provider
        # sends no Retry-After header.
        self._rate_limit.observe({**headers, "x-edl-response-status": str(raw.status_code)})

        # `path` is the spec-declared template only. The query string is deliberately never
        # logged: a session-key source carries its credential there (OWASP A09).
        _logger.info(
            "rest_source_request_completed",
            source_id=self._spec.source_id,
            method=method,
            path=path,
            status_class=f"{raw.status_code // 100}xx",
        )
        if raw.status_code >= 400:
            record_platform_metric(
                PlatformMetric.SOURCE_API_ERRORS,
                1.0,
                SourceId=self._spec.source_id,
                StatusClass=f"{raw.status_code // 100}xx",
            )
        # An error body is never parsed: a 5xx HTML error page is a transient outage, and
        # parsing it would misclassify the failure as a deterministic contract break.
        body = {} if raw.status_code >= 400 else _parse_body(raw)
        return RestResponse(status_code=raw.status_code, body=body, headers=headers)

    def _resolve_url(self, path: str) -> str:
        if path.startswith("https://"):
            # Link-header pagination hands back an absolute URL; the allowlist still applies.
            return path
        base_url = self._spec.base_url
        base = base_url if base_url.endswith("/") else base_url + "/"
        return urljoin(base, path.lstrip("/"))

    def _auth_query_parameters(self) -> dict[str, str]:
        """A session-key source authenticates in the query string, not in a header."""
        if self._spec.auth_kind is not AuthKind.SESSION_KEY_QUERY:
            return {}
        return {self._spec.session_key_parameter: self._bearer_value()}

    def _bearer_value(self) -> str:
        """The live token: exchanged when the source declares an endpoint, else the stored one."""
        if self._token_exchange is not None:
            return self._token_exchange.token()
        return self._credentials.get("access_token", "")

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        kind = self._spec.auth_kind
        if kind in (AuthKind.BEARER_TOKEN, AuthKind.OAUTH2_REFRESH):
            headers["Authorization"] = f"Bearer {self._bearer_value()}"
        elif kind is AuthKind.API_KEY_HEADER:
            headers[self._spec.api_key_header_name] = (
                f"{self._spec.api_key_value_prefix}{self._credentials.get('api_key', '')}"
            )
        elif kind is AuthKind.BASIC:
            username = self._credentials.get("username", "")
            secret = self._credentials.get("password", "")
            pair = f"{username}:{secret}"
            encoded = base64.b64encode(pair.encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"
        return headers

    def _raise_for_status(self, status_code: int, method: str, path: str) -> None:
        if status_code < 400:
            return
        context = f"{self._spec.source_id}: {method} {path} returned {status_code}"
        if status_code == 429:
            raise RestSourceThrottledError(context)
        if status_code in (401, 403):
            raise RestSourceCredentialError(context)
        if status_code == 404:
            raise RestSourceObjectError(context)
        if status_code >= 500:
            raise RestSourceTransientError(context)
        raise RestSourceRequestError(context)


def _parse_body(raw: Any) -> Any:
    text = getattr(raw, "text", "") or ""
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # A non-JSON body is a provider contract break, not a credential problem.
        raise RestSourceRequestError("Source returned a non-JSON body.") from None
