"""
Resource names come from the environment, never from this repository.

Every physical AWS name — table, queue, secret path — is created by Terraform and delivered to the
running code as an environment variable. Nothing here holds a literal name, because a literal is
what silently addresses the wrong environment: dev and uat share one AWS account, so a name without
the environment token in it resolves to the *other* environment's data rather than failing.

The two composed values below exist because some names are built rather than passed whole:

- `RESOURCE_NAME_PREFIX` (e.g. `datalake`) — for the per-stage DLQ names, which are derived from a
  stage key that only the code knows.
- `SECRET_PATH_PREFIX` (e.g. `datalake/dev`) — for credential paths, whose tenant and connection
  segments are only known at request time.

Both are `require_env`, so an unset variable fails closed at first use instead of composing a name
that points nowhere.
"""

from __future__ import annotations

from typing import Final

from observability.lambda_runtime import require_env

RESOURCE_NAME_PREFIX_VAR: Final[str] = "RESOURCE_NAME_PREFIX"
SECRET_PATH_PREFIX_VAR: Final[str] = "SECRET_PATH_PREFIX"  # noqa: S105 — a variable name, not a secret


KNOWN_ENVIRONMENTS: Final[frozenset[str]] = frozenset({"dev", "uat", "prod"})


def validate_environment(environment: str) -> str:
    """Reject an unknown environment rather than composing a name nothing answers to."""
    if environment not in KNOWN_ENVIRONMENTS:
        raise ValueError(
            f"environment must be one of {sorted(KNOWN_ENVIRONMENTS)}, got {environment!r}."
        )
    return environment


def resource_name_prefix() -> str:
    """The platform's resource name prefix, as Terraform set it."""
    return require_env(RESOURCE_NAME_PREFIX_VAR)


def secret_path_prefix() -> str:
    """The Secrets Manager path prefix, already environment-qualified by Terraform."""
    return require_env(SECRET_PATH_PREFIX_VAR)


def secret_path(*segments: str) -> str:
    """A Secrets Manager path under the deployment's prefix."""
    if not segments or any(not segment for segment in segments):
        raise ValueError("every secret path segment must be non-empty.")
    return "/".join((secret_path_prefix(), *segments))


def name_list_from_env(variable: str) -> tuple[str, ...]:
    """A comma-separated list of resource names that Terraform built from the real resources."""
    raw = require_env(variable)
    names = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not names:
        raise RuntimeError(f"{variable} is set but contains no resource names.")
    return names
