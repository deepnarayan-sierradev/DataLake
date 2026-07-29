"""
Tests for the config- and semantic-governance route table (DL-11, DL-03).

These routes are the console's read/act surface, so the assertions are about the guards: a
capability path segment must resolve to a declared capability, a reprocess must be eligible and
bounded, and a rollback needs a second actor.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from config_propagation.capability import (
    CAPABILITY_POLICIES,
    ConfigCapability,
    RetentionReprocessingConflictError,
)
from connector_runtime.api.config_governance_routes import (
    ConfigRoute,
    ConfigRouteError,
    ReprocessRequestParams,
    RollbackRequestParams,
    build_config_routes,
    match_config_route,
    parse_capability,
    parse_entity_key,
)


def _eligible_capability() -> ConfigCapability:
    return next(
        capability
        for capability, policy in CAPABILITY_POLICIES.items()
        if policy.is_reprocess_eligible
    )


def _apply_forward_capability() -> ConfigCapability:
    return next(
        capability
        for capability, policy in CAPABILITY_POLICIES.items()
        if not policy.is_reprocess_eligible
    )


class TestCapabilityParsing:
    def test_a_declared_capability_resolves(self) -> None:
        assert parse_capability("field_mapping") is ConfigCapability.FIELD_MAPPING

    def test_an_undeclared_capability_is_refused(self) -> None:
        with pytest.raises(ConfigRouteError, match="not a declared"):
            parse_capability("everything")

    def test_a_path_traversal_attempt_is_refused(self) -> None:
        with pytest.raises(ConfigRouteError):
            parse_capability("../../secrets")

    def test_every_declared_capability_is_parseable(self) -> None:
        # A capability the API cannot name is a capability the console cannot govern.
        for capability in ConfigCapability:
            assert parse_capability(capability.value) is capability


class TestEntityKeyParsing:
    def test_an_underscored_key_is_accepted(self) -> None:
        assert parse_entity_key("ar_invoice") == "ar_invoice"

    @pytest.mark.parametrize("bad", ["../etc", "ar invoice", "ar;drop", "", "AR_INVOICE!"])
    def test_a_malformed_key_is_refused(self, bad: str) -> None:
        with pytest.raises(ConfigRouteError):
            parse_entity_key(bad)


class TestReprocessRequestParams:
    def _params(self, **overrides: Any) -> ReprocessRequestParams:
        payload: dict[str, Any] = {
            "capability": _eligible_capability(),
            "entity_key": "ar_invoice",
            "window_start": date(2026, 1, 1),
            "window_end": date(2026, 1, 31),
            "reason": "restating a mapping fix",
            "pinned_config_version": "v7",
        }
        payload.update(overrides)
        return ReprocessRequestParams(**payload)

    def test_a_valid_request_reports_its_inclusive_window(self) -> None:
        assert self._params().window_days == 31

    def test_a_single_day_window_is_one_day(self) -> None:
        params = self._params(window_start=date(2026, 1, 5), window_end=date(2026, 1, 5))
        assert params.window_days == 1

    def test_an_inverted_window_is_refused(self) -> None:
        with pytest.raises(ConfigRouteError, match="window_end"):
            self._params(window_start=date(2026, 2, 1), window_end=date(2026, 1, 1))

    def test_a_reprocess_must_state_a_reason(self) -> None:
        # The reason is what makes the recomputed output explainable afterwards.
        with pytest.raises(ConfigRouteError, match="reason"):
            self._params(reason="   ")

    def test_a_reprocess_must_pin_a_configuration_version(self) -> None:
        with pytest.raises(ConfigRouteError, match="pin"):
            self._params(pinned_config_version="")

    def test_an_apply_forward_capability_cannot_be_reprocessed(self) -> None:
        with pytest.raises(ConfigRouteError, match="apply-forward"):
            self._params(capability=_apply_forward_capability())

    def test_retention_shorter_than_the_window_is_refused(self) -> None:
        capability = _eligible_capability()
        minimum = CAPABILITY_POLICIES[capability].minimum_reprocessing_window_days
        assert minimum is not None, "a reprocess-eligible capability declares a minimum window"
        with pytest.raises(RetentionReprocessingConflictError):
            self._params(capability=capability).guard_retention(max(1, minimum - 1))

    def test_retention_at_or_above_the_declared_minimum_is_accepted(self) -> None:
        capability = _eligible_capability()
        minimum = CAPABILITY_POLICIES[capability].minimum_reprocessing_window_days
        assert minimum is not None
        self._params(capability=capability).guard_retention(minimum)

    def test_unset_retention_is_not_treated_as_zero(self) -> None:
        self._params().guard_retention(None)


class TestRollbackRequestParams:
    def _params(self, **overrides: Any) -> RollbackRequestParams:
        payload: dict[str, Any] = {
            "capability": ConfigCapability.FIELD_MAPPING,
            "entity_key": "ar_invoice",
            "target_version": "v3",
            "requested_by": "alice",
            "approved_by": "bob",
        }
        payload.update(overrides)
        return RollbackRequestParams(**payload)

    def test_a_valid_request_is_accepted(self) -> None:
        assert self._params().target_version == "v3"

    def test_a_rollback_must_name_its_target_version(self) -> None:
        with pytest.raises(ConfigRouteError, match="target version"):
            self._params(target_version="  ")

    def test_a_rollback_must_name_its_requester(self) -> None:
        with pytest.raises(ConfigRouteError, match="requester"):
            self._params(requested_by="")

    def test_an_unapproved_rollback_is_refused(self) -> None:
        with pytest.raises(ConfigRouteError, match="approver"):
            self._params(approved_by="")

    def test_self_approval_is_refused(self) -> None:
        with pytest.raises(ConfigRouteError, match="approver"):
            self._params(approved_by="alice")


def _routes(calls: list[str]) -> tuple[ConfigRoute, ...]:
    def handler(name: str) -> Any:
        def call(event: dict[str, Any], *args: Any) -> dict[str, Any]:
            calls.append(f"{name}:{','.join(str(a) for a in args)}")
            return {"handler": name, "args": list(args)}

        return call

    return build_config_routes(
        effective_config=handler("effective_config"),
        effective_config_one=handler("effective_config_one"),
        rollback=handler("rollback"),
        reprocess=handler("reprocess"),
        restatements=handler("restatements"),
        metric_lineage=handler("metric_lineage"),
        model_versions=handler("model_versions"),
        active_model=handler("active_model"),
    )


class TestRouteMatching:
    def setup_method(self, method: object = None) -> None:
        self.calls: list[str] = []
        self.routes = _routes(self.calls)

    def _match(self, method: str, path: str) -> dict[str, Any] | None:
        return match_config_route(self.routes, {}, method, path.strip("/").split("/"))

    def test_effective_config_list_route(self) -> None:
        response = self._match("GET", "/tenants/demo/config/effective")
        assert response is not None and response["handler"] == "effective_config"
        assert response["args"] == ["demo"]

    def test_effective_config_single_route_passes_capability_and_entity(self) -> None:
        response = self._match("GET", "/tenants/demo/config/effective/field_mapping/ar_invoice")
        assert response is not None
        assert response["args"] == ["demo", "field_mapping", "ar_invoice"]

    def test_rollback_route_is_a_post(self) -> None:
        # A GET must never reach the rollback handler — reads and state changes are
        # separated by method, not by path alone.
        read = self._match("GET", "/tenants/demo/config/field_mapping/ar_invoice/rollback")
        assert read is None or read["handler"] != "rollback"
        response = self._match("POST", "/tenants/demo/config/field_mapping/ar_invoice/rollback")
        assert response is not None and response["handler"] == "rollback"

    def test_reprocess_route_is_distinct_from_rollback(self) -> None:
        response = self._match("POST", "/tenants/demo/config/field_mapping/ar_invoice/reprocess")
        assert response is not None and response["handler"] == "reprocess"

    def test_restatements_route(self) -> None:
        response = self._match("GET", "/tenants/demo/config/restatements")
        assert response is not None and response["handler"] == "restatements"

    def test_metric_lineage_route(self) -> None:
        response = self._match("GET", "/tenants/demo/semantic/metrics/revenue/lineage")
        assert response is not None
        assert response["args"] == ["demo", "revenue"]

    def test_model_versions_route(self) -> None:
        response = self._match("GET", "/tenants/demo/semantic/model/versions")
        assert response is not None and response["handler"] == "model_versions"

    def test_active_model_route(self) -> None:
        response = self._match("GET", "/tenants/demo/semantic/model")
        assert response is not None and response["handler"] == "active_model"

    def test_an_unknown_path_matches_nothing(self) -> None:
        assert self._match("GET", "/tenants/demo/unknown/thing") is None

    def test_a_path_not_rooted_at_tenants_matches_nothing(self) -> None:
        # Every governed read is tenant-scoped; a non-tenant path must not fall through.
        assert self._match("GET", "/admin/demo/config/effective") is None

    def test_the_wrong_segment_count_matches_nothing(self) -> None:
        assert self._match("GET", "/tenants/demo/config") is None

    def test_no_handler_is_invoked_when_nothing_matches(self) -> None:
        self._match("GET", "/tenants/demo/unknown/thing")
        assert self.calls == []

    def test_there_is_no_route_that_creates_a_tenant_user_or_role(self) -> None:
        # Identity is owned by the Identity API; this surface only consumes a verified claim.
        resources = {route.resource for route in self.routes}
        assert resources == {"config", "semantic"}
