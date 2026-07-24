"""
Platform-wide identifier validation policy — single source of truth.

All modules that validate source_id, entity_id, entity_type, or run_id MUST import
from here.  Never duplicate these constants — update here and the change propagates
everywhere automatically.

Design:
  - STABLE_ID_PATTERN: 2-64 chars, lowercase letters/digits/hyphens,
    must start with a letter.  Used for source_id and entity_id.
  - ENTITY_TYPE_PATTERN: like STABLE_ID_PATTERN but also permits underscores
    (real entity types like "ar_invoice"/"ap_bill" need them). Used for entity_type.
  - RUN_ID_PATTERN: same character set but up to 100 chars to accommodate
    the timestamp + UUID format (e.g. run-20260611-143022123456-a3f9c1d2).
  - SEQUENTIAL_INTEGER_PATTERN: detects bare integer run_ids, which are
    rejected to prevent enumeration attacks on audit logs.
  - PROHIBITED_IDENTIFIERS: generic names that must never be used as
    source or entity identifiers.

Security (OWASP A03):
  - Centralised validation prevents identifier pattern drift between modules,
    which could allow path traversal or DynamoDB key injection via one entry
    point that has a looser check than another.
"""

from __future__ import annotations

import re
from typing import Final

# ---------------------------------------------------------------------------
# Compiled patterns (compiled once at module load — never inside functions)
# ---------------------------------------------------------------------------

STABLE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9\-]{1,63}$")

# Like STABLE_ID_PATTERN but also permits underscores (e.g. "ar_invoice", "ap_bill").
ENTITY_TYPE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_\-]{1,63}$")

# Tenant code format: lowercase letters, digits, hyphens; 2-48 characters; starts with a letter.
# Examples: "acme-corp", "globex-eu", "demo".
TENANT_CODE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9\-]{1,47}$")

# Sentinel tenant code for the pre-multi-tenancy single-tenant deployment.
# `tenant_scoped_key()` special-cases this value so existing DynamoDB items
# and S3 objects written before tenant scoping existed continue to resolve
# without a data migration (§1.1 backward-compatibility guarantee).
DEFAULT_TENANT_CODE: Final[str] = "demo"

# Run-ids include a timestamp+UUID component and are up to 100 chars.
# The generated format "run-YYYYMMDD-HHMMSSffffff-xxxxxxxx" is ~37 chars,
# but 100 chars is allowed to accommodate future extensions.
RUN_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9\-]{1,99}$")

# Rejects bare sequential integers as run_ids (enumeration attack prevention).
SEQUENTIAL_INTEGER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d+$")

# Generic names that must never be used as source or entity identifiers.
PROHIBITED_IDENTIFIERS: Final[frozenset[str]] = frozenset(
    {
        "helper",
        "util",
        "common",
        "manager",
        "phase1",
        "phase2",
    }
)


# ---------------------------------------------------------------------------
# Reusable validation helpers
# ---------------------------------------------------------------------------


def validate_stable_id(value: str, field_name: str = "identifier") -> str:
    """
    Validate a stable identifier.

    Raises ValueError with a precise message on failure so callers can
    surface it directly to operators without leaking internals.
    Returns the original value on success so it can be used inline inside
    Pydantic field_validators.

    Args:
        value:      The identifier string to validate.
        field_name: Display name used in the error message (e.g. 'source_id').

    Raises:
        ValueError: When the value fails the stable-id format or is prohibited.
    """
    if not STABLE_ID_PATTERN.match(value):
        raise ValueError(
            f"{field_name} {value!r} does not conform to the stable identifier format. "
            "Use lowercase letters, digits, and hyphens only (2-64 chars; must start "
            "with a letter). Examples: 'salesforce', 'salesforce-account', 'mysql-rds'."
        )
    if value in PROHIBITED_IDENTIFIERS:
        raise ValueError(
            f"{field_name} {value!r} is a prohibited generic name. "
            "Use a specific, domain-meaningful identifier instead. "
            f"Prohibited names: {sorted(PROHIBITED_IDENTIFIERS)}."
        )
    return value


def validate_tenant_code(value: str, field_name: str = "tenant_code") -> str:
    """
    Validate a tenant code slug.

    Raises ValueError with a precise message on failure so callers can
    surface it directly to operators without leaking internals.
    Returns the original value on success so it can be used inline inside
    Pydantic field_validators.

    Args:
        value:      The tenant code string to validate.
        field_name: Display name used in the error message.

    Raises:
        ValueError: When the value fails the tenant code format.
    """
    if not TENANT_CODE_PATTERN.match(value):
        raise ValueError(
            f"{field_name} {value!r} does not conform to the tenant code format. "
            "Use lowercase letters, digits, and hyphens only (2-48 chars; must start "
            "with a letter). Examples: 'acme-corp', 'globex-eu', 'demo'."
        )
    return value


def tenant_scoped_key(tenant_code: str, key: str) -> str:
    """
    Build a tenant-scoped composite value for a DynamoDB key attribute.

    Every repository that stores tenant-owned records by source_id/entity_id
    (ConfigurationRepositoryClient, WatermarkRepository, SchemaSnapshotRepository)
    must scope its key through this function so two different tenants can
    never collide on the same source_id/entity_id value (§1.1 — SEC-2 / ARCH-1).

    Matches the convention already established in `curated_layer_writer.py`'s
    S3 path scheme (`{tenant_code}/curated/...`): `tenant_code` is always
    prefixed, including for `DEFAULT_TENANT_CODE` ("demo") — there is no
    special-cased "no prefix" behaviour, so every tenant (including the
    pre-multi-tenancy default) is scoped identically and consistently.

    Args:
        tenant_code: Validated tenant code slug.
        key:         The unscoped key value (e.g. entity_id).

    Returns:
        `f"{tenant_code}#{key}"`.
    """
    return f"{tenant_code}#{key}"


def strip_tenant_prefix(tenant_code: str, scoped_key: str) -> str:
    """Inverse of `tenant_scoped_key`: plain key, or `scoped_key` unchanged if not prefixed."""
    prefix = f"{tenant_code}#"
    return scoped_key[len(prefix) :] if scoped_key.startswith(prefix) else scoped_key


def validate_run_id(value: str) -> str:
    """
    Validate a run_id.

    Rejects bare sequential integers to prevent enumeration attacks on
    run audit logs and to enforce idempotency guarantees.

    Args:
        value: The run_id string to validate.

    Raises:
        ValueError: When the value is a bare sequential integer.
    """
    if SEQUENTIAL_INTEGER_PATTERN.match(value):
        raise ValueError(
            f"run_id {value!r} is a bare sequential integer, which is not permitted. "
            "Use a run_id that includes a timestamp or UUID component to prevent "
            "enumeration. Example: 'run-20260611-143022123456-a3f9c1d2'."
        )
    return value


SAFE_S3_PREFIX_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-_/=]{0,511}$")

# Physical column / SQL identifier — allowlisted before any query build (OWASP A03).
SAFE_COLUMN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def validate_s3_prefix(value: str, field_name: str = "s3_prefix") -> str:
    # OWASP A03: reject path traversal / injection in S3 key prefixes.
    clean = value.rstrip("/")
    if not SAFE_S3_PREFIX_PATTERN.match(clean):
        raise ValueError(f"{field_name} {value!r} is not a safe S3 prefix.")
    return clean
