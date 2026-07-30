"""
The system boundary, enforced at the layer that actually publishes routes.

`connector_runtime/tests/test_control_plane_handler.py` asserts `POST /tenants` returns 404. It
passed for months while `infrastructure/modules/control_plane/main.tf` provisioned that exact
route, because a test that calls `lambda_handler` cannot see API Gateway. The handler refusing a
route and the infrastructure not publishing it are two different claims, and only one was checked.

Tenants, users, roles and permissions belong to the Identity API. This system consumes a verified
claim and never authors identity, so no route here may create or administer one — see CLAUDE.md,
which the repo owner has restated across multiple sessions as a boundary rather than a preference.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

CONTROL_PLANE_TF: Final[Path] = (
    Path(__file__).resolve().parent.parent
    / "infrastructure"
    / "modules"
    / "control_plane"
    / "main.tf"
)

IDENTITY_COLLECTIONS: Final[frozenset[str]] = frozenset(
    {"tenants", "users", "roles", "permissions", "groups"}
)

MUTATING_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_ROUTE_LINE: Final[re.Pattern[str]] = re.compile(
    r'^\s*(?P<key>[a-z0-9_]+)\s*=\s*"(?P<method>[A-Z]+)\s+(?P<path>/\S*)"\s*$', re.MULTILINE
)


def declared_routes() -> dict[str, tuple[str, str]]:
    """Every `key = "METHOD /path"` entry in the control-plane route map."""
    lines = CONTROL_PLANE_TF.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith("routes = {"))
    block: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() == "}":
            break
        block.append(line)
    return {
        match.group("key"): (match.group("method"), match.group("path"))
        for match in _ROUTE_LINE.finditer("\n".join(block))
    }


def test_the_route_map_is_actually_parsed() -> None:
    """Guard the parser: a regex that matches nothing would make every assertion below vacuous."""
    routes = declared_routes()
    assert len(routes) >= 10, (
        f"parsed only {len(routes)} routes — the parser is broken, not the map"
    )
    assert ("GET", "/tenants/{tenant_code}/entities") in routes.values()


def test_no_route_creates_or_administers_an_identity_resource() -> None:
    """A mutating route whose collection is the identity resource itself is a boundary breach."""
    offenders: list[str] = []
    for key, (method, path) in sorted(declared_routes().items()):
        segments = [segment for segment in path.split("/") if segment]
        if not segments:
            continue
        collection_is_the_resource = len(segments) == 1 and segments[0] in IDENTITY_COLLECTIONS
        if method in MUTATING_METHODS and collection_is_the_resource:
            offenders.append(f"{key} = {method} {path}")

    assert not offenders, (
        "control-plane Terraform publishes identity-management route(s) this system must not own: "
        f"{offenders}. Tenants, users, roles and permissions belong to the Identity API."
    )


def test_post_tenants_specifically_is_absent() -> None:
    """The named case from CLAUDE.md, asserted at the infrastructure layer."""
    assert ("POST", "/tenants") not in declared_routes().values()
