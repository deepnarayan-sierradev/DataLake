"""
Per-request kernel for the control-plane API: identity, authorisation, scope, and serialisation.

Every route module depends on this and it depends on none of them, which is what keeps the split
acyclic. It exists because `control_plane_handler.py` had grown to 1,361 lines with 22 handlers and
imports from thirteen first-party packages — so every new capability widened one file, and that
file was the natural place for a route to be added without its isolation control. That is not
hypothetical: it is how the twin routes came to filter on a field the model never carried.

Security (OWASP A01): `authorize_path_tenant` is the single crossing point between a request and a
tenant. It reads the verified authorizer claim, never the path parameter, and binds `tenant_code`
into the log context only *after* the claim is confirmed — so a rejected request never stamps its
logs with a tenant it was not entitled to name. `scope_predicate_for` is the only place a scope
predicate is constructed for this surface.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

import structlog

from connector_runtime.api.errors import (
    ApiError,
    AuthenticationError,
    AuthorizationError,
    ValidationFailedError,
)
from contracts.identifier_policy import validate_tenant_code
from contracts.platform_metrics import PlatformMetric
from observability.lambda_runtime import require_env
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger
from tenancy.scope_predicate import (
    ConsumptionSurface,
    EmptyScopeDenialError,
    ScopePredicate,
    build_scope_claims,
    scope_predicate,
)
from tenancy.scope_unit_repository import ScopeUnitRepository

_logger = get_platform_logger(__name__)

PAGE_TOKEN_PREFIX: Final[str] = "edl-page:"  # noqa: S105 — a page marker, not a secret


def region() -> str:
    return os.environ.get("AWS_REGION", "us-east-1")


def environment() -> str:
    return require_env("PLATFORM_ENVIRONMENT")


def extract_claims(event: dict[str, Any]) -> dict[str, Any] | None:
    """
    Extract the authorizer claims dict from an API Gateway proxy event.

    Checks both plausible shapes so this works regardless of which API
    Gateway / authorizer combination fronts the Lambda:
      - REST API / HTTP API (payload format 1.0) + Cognito User Pools
        authorizer: requestContext.authorizer.claims
      - HTTP API (payload format 2.0) + JWT authorizer:
        requestContext.authorizer.jwt.claims
    """
    authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
    claims = authorizer.get("claims")
    if isinstance(claims, dict):
        return claims
    jwt_claims = (authorizer.get("jwt") or {}).get("claims")
    if isinstance(jwt_claims, dict):
        return jwt_claims
    return None


def authenticated_tenant_code(event: dict[str, Any]) -> str:
    """
    Extract and validate the authenticated tenant_code from the authorizer context.

    Fails closed: absence of authorizer claims (Cognito authorizer not wired
    up, or a local/manual invocation) is always rejected with 401 — the
    `{tenant_code}` path parameter is NEVER trusted as a fallback.
    """
    claims = extract_claims(event)
    if not claims:
        record_platform_metric(PlatformMetric.AUTHENTICATION_FAILURES)
        raise AuthenticationError(
            "Request is missing authenticated identity context. This API requires "
            "a valid authenticated request."
        )
    tenant_claim = claims.get("custom:tenant_code") or claims.get("tenant_code")
    if not tenant_claim:
        record_platform_metric(PlatformMetric.AUTHENTICATION_FAILURES)
        raise AuthenticationError("Authenticated identity does not carry a tenant_code claim.")
    return validate_tenant_code(str(tenant_claim))


def authorize_path_tenant(event: dict[str, Any], path_tenant_code: str) -> str:
    """Validate path_tenant_code's format and cross-check it against the authenticated tenant."""
    tenant_code = validate_tenant_code(path_tenant_code)
    authenticated = authenticated_tenant_code(event)
    if authenticated != tenant_code:
        # A caller reaching for another tenant's path is a cross-tenant attempt whether the
        # cause is a bug or an attack, so it pages either way.
        record_platform_metric(PlatformMetric.CROSS_TENANT_ACCESS_ATTEMPTS)
        record_platform_metric(PlatformMetric.AUTHORIZATION_DENIALS, 1.0, Capability="tenant_path")
        raise AuthorizationError(
            "Authenticated tenant is not permitted to access this tenant_code path."
        )
    # Bound only after the claim is verified, so a rejected request never stamps its logs with a
    # tenant it was not entitled to name.
    structlog.contextvars.bind_contextvars(tenant_code=tenant_code)
    return tenant_code


def json_default(value: Any) -> Any:
    """
    json.dumps default= hook: DynamoDB numeric attributes deserialize as
    decimal.Decimal via the boto3 resource API, which the stdlib json module
    cannot serialize natively.
    """
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def json_response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, separators=(",", ":"), default=json_default),
    }


def error_response(exc: ApiError) -> dict[str, Any]:
    return json_response(exc.status_code, {"error": exc.message})


def parse_json_body(event: dict[str, Any]) -> dict[str, Any]:
    body_str = event.get("body") or "{}"
    try:
        parsed = json.loads(body_str)
    except json.JSONDecodeError as exc:
        raise ValidationFailedError("Request body is not valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise ValidationFailedError("Request body must be a JSON object.")
    return parsed


def authenticated_user(event: dict[str, Any]) -> str:
    claims = extract_claims(event) or {}
    return str(
        claims.get("sub") or claims.get("email") or claims.get("cognito:username") or "unknown"
    )


def granted_access_tags(event: dict[str, Any]) -> frozenset[str]:
    # OWASP A01: data-level access tags come from verified authorizer claims, never the body.
    claims = extract_claims(event) or {}
    raw = str(claims.get("custom:access_tags") or claims.get("access_tags") or "")
    return frozenset(tag.strip() for tag in raw.split(",") if tag.strip())


def granted_scope_units(event: dict[str, Any]) -> frozenset[str]:
    """
    Scope units this caller was granted, from the verified claim only (OWASP A01).

    Never read from the body or a query string: the whole point of DL-12 is that the caller
    cannot choose which franchisee's data it sees.
    """
    claims = extract_claims(event) or {}
    raw = str(claims.get("custom:scope_units") or claims.get("scope_units") or "")
    return frozenset(unit.strip().lower() for unit in raw.split(",") if unit.strip())


def claims_grant_tenant_wide(event: dict[str, Any]) -> bool:
    """A tenant-wide grant is explicit; absence of scope units is never read as "everything"."""
    claims = extract_claims(event) or {}
    raw = str(claims.get("custom:scope_tenant_wide") or claims.get("scope_tenant_wide") or "")
    return raw.strip().lower() in {"1", "true", "yes"}


def scope_predicate_for(
    event: dict[str, Any], tenant_code: str, surface: ConsumptionSurface
) -> ScopePredicate:
    """
    Build the row filter for this caller on this surface (DL-SCOPE-14).

    One builder, used by every read path in this handler. An empty grant raises
    `EmptyScopeDenialError`, which `error_response` renders as 403 — never as "no filter".
    """
    repository = ScopeUnitRepository(environment=environment(), region_name=region())
    profile = repository.get_partition_profile(tenant_code)
    claims = build_scope_claims(
        tenant_code,
        profile,
        granted_scope_unit_ids=granted_scope_units(event),
        tenant_wide=claims_grant_tenant_wide(event),
        units=repository.list_scope_units(tenant_code),
    )
    try:
        return scope_predicate(claims, surface=surface)
    except EmptyScopeDenialError as exc:
        # Empty grant means deny, and the caller is told so — a 500 here would read as an
        # outage and invite a retry loop against a decision that will not change.
        raise AuthorizationError(
            "Your access grant names no scope units, so no rows are visible."
        ) from exc


def decode_page_token(event: dict[str, Any], tenant_code: str) -> dict[str, Any] | None:
    """
    Decode the caller's continuation token into a DynamoDB exclusive-start key (F7).

    This carried a row *offset* until 2026-07-29, which made every page cost the same as the first:
    the handler re-read the whole result set and sliced it. It was also unstable — a row inserted
    or resolved between requests shifted the offset, silently skipping or duplicating rows.

    Security (OWASP A01): the token now carries a key, so a caller could craft one naming another
    tenant's partition. The `KeyConditionExpression` already pins `tenant_code`, but relying on
    that implicitly is how the twin filter came to read a field that did not exist — so the key's
    own `tenant_code` is checked here, and a mismatch is a 400 rather than a silent empty page.

    A malformed token is a 400, never a silent restart from zero: restarting silently would loop a
    paginating client forever.
    """
    raw = str((event.get("queryStringParameters") or {}).get("next_token") or "")
    if not raw:
        return None
    try:
        decoded = base64.urlsafe_b64decode(raw.encode("ascii")).decode("ascii")
        # The prefix must be present: `removeprefix` is a no-op when it is absent, which would
        # accept a hand-written payload and defeat the point of an opaque cursor.
        if not decoded.startswith(PAGE_TOKEN_PREFIX):
            raise ValueError("token is missing its marker")
        key = json.loads(decoded[len(PAGE_TOKEN_PREFIX) :])
    except (ValueError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as exc:
        raise ValidationFailedError("next_token is not a valid continuation token.") from exc

    if not isinstance(key, dict) or not key:
        raise ValidationFailedError("next_token is not a valid continuation token.")
    token_tenant = key.get("tenant_code")
    if token_tenant is not None and str(token_tenant) != tenant_code:
        record_platform_metric(PlatformMetric.CROSS_TENANT_ACCESS_ATTEMPTS)
        raise ValidationFailedError("next_token does not belong to this tenant.")
    return {str(name): value for name, value in key.items()}


def encode_page_token(next_key: dict[str, Any] | None) -> str | None:
    """Encode a DynamoDB exclusive-start key as an opaque continuation token."""
    if not next_key:
        return None
    payload = f"{PAGE_TOKEN_PREFIX}{json.dumps(next_key, sort_keys=True, default=str)}"
    return base64.urlsafe_b64encode(payload.encode("ascii")).decode("ascii")
