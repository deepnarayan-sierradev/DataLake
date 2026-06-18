"""
Platform-wide identifier validation policy — single source of truth.

All modules that validate source_id, entity_id, or run_id MUST import from
here.  Never duplicate these constants — update here and the change propagates
everywhere automatically.

Design:
  - STABLE_ID_PATTERN: 2-64 chars, lowercase letters/digits/hyphens,
    must start with a letter.  Used for source_id and entity_id.
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
