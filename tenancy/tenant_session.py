"""
Tenant-tagged AWS sessions — the missing half of the IAM tenant boundary (DL-SEC-01, DL-SEC-02).

`infrastructure/modules/iam/tenant_boundary.tf` conditions every statement on
`${aws:PrincipalTag/tenant_code}`. That tag was never set, and **could not be**: the boundary
attaches to four Lambda execution roles that each serve every tenant, and a role tag holds one
value. So the policy as written was not merely unapplied, it was unsatisfiable:

- the S3 statements are guarded by `Null aws:PrincipalTag/tenant_code = false`, which is never true
  for an untagged principal, so the Deny never applies — **S3 would stay unprotected under
  `enforce`**;
- the DynamoDB statement compares `dynamodb:LeadingKeys` against an unresolvable policy variable,
  which no key matches, so the Deny applies to everything — **every item operation would fail**;
- `secretsmanager:ResourceTag/tenant_code` is absent on every secret, and a negated condition on a
  missing key is true, so **every credential read would fail**.

The fix is a *session* tag rather than a role tag: the stage role assumes a per-stage data role with
`Tags=[{tenant_code}]` for the tenant it is currently processing, and the boundary attaches to that
data role. `sts:TagSession` in the trust policy is what makes the tag authoritative — a caller
cannot
choose a tag the trust policy does not permit.

Credentials are cached per (role, tenant) with a safety margin, because a warm container processing
one tenant's entity repeatedly should not call STS per S3 read. The cache is keyed by tenant, so a
container that later serves a different tenant gets different credentials rather than reusing the
first tenant's — which would reintroduce exactly the boundary being built.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import boto3
from botocore.exceptions import ClientError

from contracts.identifier_policy import validate_tenant_code
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# Refresh this far before expiry so a long stage never runs past its credentials mid-operation.
REFRESH_MARGIN: Final[timedelta] = timedelta(minutes=5)

# One hour is the default maximum for a role chained from another role; shorter than the Lambda's
# own 15-minute ceiling matters less than not having to refresh inside a single invocation.
SESSION_DURATION_SECONDS: Final[int] = 3600

# Set by Terraform on each stage Lambda. Absent means the tenant-tagged path is not deployed for
# this
# function, which is a deployment state the caller must handle explicitly rather than silently skip.
TENANT_DATA_ROLE_ARN_ENV: Final[str] = "TENANT_DATA_ROLE_ARN"


class TenantSessionUnavailableError(Exception):
    """Raised when a tenant-tagged session cannot be obtained, so no tenant data may be touched."""


@dataclass(frozen=True)
class _CachedCredentials:
    """One tenant's assumed-role credentials and their expiry."""

    access_key_id: str
    secret_access_key: str
    session_token: str
    expires_at: datetime

    def is_fresh(self, now: datetime) -> bool:
        return now + REFRESH_MARGIN < self.expires_at


# Keyed by (role_arn, tenant_code) — never by role alone, or a warm container would hand one
# tenant's credentials to the next tenant it happens to serve.
_CACHE: dict[tuple[str, str], _CachedCredentials] = {}


def clear_cached_sessions() -> None:
    """Drop every cached credential; used by tests and by a container recycling deliberately."""
    _CACHE.clear()


def tenant_data_role_arn() -> str | None:
    """The data role this function assumes per tenant, or None when the path is not deployed."""
    return os.environ.get(TENANT_DATA_ROLE_ARN_ENV) or None


def tenant_scoped_session(
    tenant_code: str,
    *,
    region_name: str,
    role_arn: str | None = None,
    sts_client: Any = None,
) -> boto3.Session:
    """
    A boto3 session whose credentials carry `tenant_code` as a session tag.

    Every client built from this session is subject to the tenant boundary's conditions, because the
    conditions resolve against the session's tag. A client built from the ambient Lambda credentials
    is not — which is the state the whole platform is in until each call site adopts this.

    Raises `TenantSessionUnavailableError` rather than falling back to ambient credentials. A silent
    fallback would make the boundary's coverage depend on which code path ran, which is
    indistinguishable from no boundary at all.
    """
    tenant_code = validate_tenant_code(tenant_code)
    resolved_role = role_arn or tenant_data_role_arn()
    if not resolved_role:
        raise TenantSessionUnavailableError(
            f"{TENANT_DATA_ROLE_ARN_ENV} is not set, so no tenant-tagged session can be built. "
            "Deploy the tenant data role for this function, or call the untagged path explicitly."
        )

    now = datetime.now(UTC)
    key = (resolved_role, tenant_code)
    cached = _CACHE.get(key)
    if cached is not None and cached.is_fresh(now):
        return _session_from(cached, region_name)

    client = sts_client or boto3.client("sts", region_name=region_name)
    try:
        response = client.assume_role(
            RoleArn=resolved_role,
            # The session name is an audit label; CloudTrail shows which tenant a call acted for.
            RoleSessionName=f"edl-{tenant_code}"[:64],
            DurationSeconds=SESSION_DURATION_SECONDS,
            # The tag the boundary conditions read. `sts:TagSession` must be granted by the data
            # role's trust policy, which also constrains which tag values are acceptable.
            Tags=[{"Key": "tenant_code", "Value": tenant_code}],
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        _logger.error(
            "tenant_session_assume_role_failed",
            tenant_code=tenant_code,
            role_arn=resolved_role,
            error_code=code,
        )
        raise TenantSessionUnavailableError(
            f"Could not assume {resolved_role} for tenant {tenant_code!r} ({code}). Refusing to "
            "fall back to untagged credentials: that would silently leave the boundary unenforced."
        ) from exc

    credentials = response["Credentials"]
    cached = _CachedCredentials(
        access_key_id=str(credentials["AccessKeyId"]),
        secret_access_key=str(credentials["SecretAccessKey"]),
        session_token=str(credentials["SessionToken"]),
        expires_at=_as_utc(credentials["Expiration"]),
    )
    _CACHE[key] = cached
    return _session_from(cached, region_name)


def _session_from(credentials: _CachedCredentials, region_name: str) -> boto3.Session:
    return boto3.Session(
        aws_access_key_id=credentials.access_key_id,
        aws_secret_access_key=credentials.secret_access_key,
        aws_session_token=credentials.session_token,
        region_name=region_name,
    )


def _as_utc(value: Any) -> datetime:
    """STS returns an aware datetime; a stub may return a naive one."""
    moment = value if isinstance(value, datetime) else datetime.now(UTC)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
