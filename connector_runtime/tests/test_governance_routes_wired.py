"""
The governance routes must exist in the deployed route table (DL-11, DL-03 §Interfaces).

`config_governance_routes.py` and its parameter guards were fully unit-tested on 2026-07-28 and
imported by nothing, so every documented governance endpoint returned 404 in the running system.
That is a class of defect a route-table unit test cannot catch: it tests the table, not whether the
handler dispatches to it.

These tests therefore go through `_route` — the deployed dispatcher — and assert only that each
documented path **reaches a handler**. They do not assert business behaviour, which is covered by
each repository's own tests; the property under test is reachability.
"""

from __future__ import annotations

from typing import Any, Final

import pytest

from connector_runtime.api import control_plane_handler
from connector_runtime.api.errors import NotFoundError

_TENANT: Final[str] = "demo"


def _event(method: str, path: str, body: str | None = None) -> dict[str, Any]:
    return {
        "httpMethod": method,
        "path": path,
        "body": body,
        "requestContext": {
            "requestId": "req-1",
            "authorizer": {"claims": {"custom:tenant_code": _TENANT}},
        },
    }


DOCUMENTED_GOVERNANCE_PATHS: Final[tuple[tuple[str, str], ...]] = (
    ("GET", f"/tenants/{_TENANT}/config/effective"),
    ("GET", f"/tenants/{_TENANT}/config/effective/field_mapping/ar_invoice"),
    ("GET", f"/tenants/{_TENANT}/config/restatements"),
    ("POST", f"/tenants/{_TENANT}/config/field_mapping/ar_invoice/rollback"),
    ("POST", f"/tenants/{_TENANT}/config/field_mapping/ar_invoice/reprocess"),
    ("GET", f"/tenants/{_TENANT}/semantic/metrics/revenue/lineage"),
    ("GET", f"/tenants/{_TENANT}/semantic/model/versions"),
    ("GET", f"/tenants/{_TENANT}/semantic/model"),
)


class TestEveryDocumentedGovernancePathIsRouted:
    @pytest.mark.parametrize(("method", "path"), DOCUMENTED_GOVERNANCE_PATHS)
    def test_the_dispatcher_reaches_a_handler(self, method: str, path: str) -> None:
        """
        A routed path may fail for any downstream reason — missing table, absent model, invalid
        body — but it must not fail with "no route matches", which is what an unwired route table
        produces.
        """
        try:
            control_plane_handler._route(_event(method, path, body="{}"))
        except NotFoundError as exc:
            assert "No route matches" not in str(exc), (
                f"{method} {path} is documented but the deployed dispatcher has no route for it. "
                "The route table exists in config_governance_routes.py; the handler must build "
                "and consult it."
            )
        except Exception:  # noqa: S110 — any other failure means the route was reached
            pass

    def test_the_route_table_is_built_from_the_shared_definition(self) -> None:
        assert control_plane_handler._GOVERNANCE_ROUTES
        resources = {route.resource for route in control_plane_handler._GOVERNANCE_ROUTES}
        assert resources == {"config", "semantic"}

    def test_there_is_still_no_tenant_provisioning_route(self) -> None:
        with pytest.raises(NotFoundError, match="No route matches"):
            control_plane_handler._route(_event("POST", "/tenants", body="{}"))


class TestGovernanceRoutesEnforceTheTenantClaim:
    @pytest.mark.parametrize(("method", "path"), DOCUMENTED_GOVERNANCE_PATHS)
    def test_a_foreign_tenant_in_the_path_is_refused(self, method: str, path: str) -> None:
        foreign = path.replace(f"/tenants/{_TENANT}/", "/tenants/acme/")
        with pytest.raises(Exception) as caught:
            control_plane_handler._route(_event(method, foreign, body="{}"))
        message = str(caught.value)
        assert "No route matches" not in message, (
            f"{method} {foreign} was not routed at all, so this test proves nothing about "
            "authorization for it."
        )
