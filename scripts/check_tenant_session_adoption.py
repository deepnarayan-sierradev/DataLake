"""
Tenant-session adoption gate (G9): report which data-plane modules still build untagged clients.

The IAM tenant boundary conditions on `aws:PrincipalTag/tenant_code`. That tag can only exist on a
session assumed per tenant (`tenancy/tenant_session.py`), so a client built from the Lambda's
ambient
credentials is **outside the boundary entirely** — no matter what the policy says.

This gate does not fail the build. It prints the adoption count, and
`tests/test_tenant_session_gate.py`
asserts the one property that must hold: the Terraform interlock
(`tenant_session_tagging_adopted`) is not set to `true` while unadopted sites remain. That is the
inversion that matters — the previous failure was a security control whose *stated* status and
actual
status were independent, so the status is now derived from the code rather than declared beside it.

Run `make tenant-session-adoption` to see the remaining work. It is real work: 100+ call sites
construct clients inside repository constructors, which is why this is tracked rather than claimed
finished.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

# Packages whose runtime touches tenant data under one of the four boundary roles. The control plane
# is deliberately absent: it serves every tenant per request and derives scope from a verified
# claim,
# so a principal-tag condition would break it rather than protect anything (see tenant_boundary.tf).
DATA_PLANE_PACKAGES: Final[frozenset[str]] = frozenset(
    {
        "connector_runtime",
        "transformation",
        "entity_resolution",
        "analytics_publisher",
        "knowledge",
        "serving_store",
        "tenancy",
        "watermark_management",
        "schema_management",
        "governance",
    }
)

EXCLUDED_PARTS: Final[frozenset[str]] = frozenset({"tests", "__pycache__", ".venv", "dist"})

# The module that implements the mechanism necessarily builds an untagged STS client.
EXEMPT_MODULES: Final[frozenset[str]] = frozenset({"tenancy/tenant_session.py"})

ADOPTION_FLAG_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"tenant_session_tagging_adopted\s*=\s*(true|false)"
)


@dataclass(frozen=True)
class UntaggedClient:
    """One boto3 client built from ambient credentials in a data-plane module."""

    path: Path
    line: int
    service: str

    def render(self) -> str:
        return f"{self.path.relative_to(REPO_ROOT)}:{self.line}: boto3 {self.service} client"


def untagged_clients_in(path: Path) -> list[UntaggedClient]:
    """Find `boto3.client(...)` / `boto3.resource(...)` calls not built from a tenant session."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[UntaggedClient] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"client", "resource"}:
            continue
        base = node.func.value
        # `boto3.client(...)` is ambient; `session.client(...)` is whatever the session is, and a
        # tenant session is the only way to get a tagged one.
        if isinstance(base, ast.Name) and base.id == "boto3":
            service = ""
            if node.args and isinstance(node.args[0], ast.Constant):
                service = str(node.args[0].value)
            found.append(UntaggedClient(path=path, line=node.lineno, service=service or "unknown"))
    return found


def adoption_flag_states() -> dict[str, str]:
    """What each environment declares for `tenant_session_tagging_adopted`."""
    states: dict[str, str] = {}
    for environment in ("dev", "staging", "prod"):
        main = REPO_ROOT / "infrastructure" / "environments" / environment / "main.tf"
        if not main.exists():
            continue
        match = ADOPTION_FLAG_PATTERN.search(main.read_text(encoding="utf-8"))
        states[environment] = match.group(1) if match else "unset"
    return states


def scan() -> list[UntaggedClient]:
    findings: list[UntaggedClient] = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        relative = path.relative_to(REPO_ROOT)
        if set(relative.parts) & EXCLUDED_PARTS:
            continue
        if relative.parts[0] not in DATA_PLANE_PACKAGES:
            continue
        if str(relative) in EXEMPT_MODULES:
            continue
        findings.extend(untagged_clients_in(path))
    return findings


def main() -> int:
    findings = scan()
    states = adoption_flag_states()

    print("Tenant-session adoption (G9) — clients outside the IAM tenant boundary\n")
    by_module: dict[str, int] = {}
    for finding in findings:
        by_module[str(finding.path.relative_to(REPO_ROOT))] = (
            by_module.get(str(finding.path.relative_to(REPO_ROOT)), 0) + 1
        )
    for module, count in sorted(by_module.items(), key=lambda item: (-item[1], item[0])):
        print(f"  {count:>3}  {module}")

    print(f"\n  {len(findings)} untagged client construction(s) across {len(by_module)} module(s).")
    print(f"  tenant_session_tagging_adopted: {states}")

    if findings and any(state == "true" for state in states.values()):
        print(
            "\nFAIL: an environment declares the tagged-session path adopted while untagged "
            "clients remain. Enforcing the boundary in that state leaves S3 open and denies "
            "DynamoDB outright — see modules/iam/tenant_boundary.tf."
        )
        return 1
    if not findings:
        print("\nOK — every data-plane client is built from a tenant-tagged session.")
        return 0
    print(
        "\nOK (tracked) — adoption is incomplete and no environment claims otherwise. "
        "The Terraform interlock refuses `enforce` while the flag is false."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
