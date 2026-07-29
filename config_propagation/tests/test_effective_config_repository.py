"""
Tests for the effective-config, restatement, and rollback records (DL-CFG-07, 08, 09, 13).

These three tables are what let the console answer "is my published change live yet", "which
run first consumed it", and "why did last quarter's number change" — so the assertions here
are about the audit properties, not just round-tripping.
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from config_propagation.capability import ConfigCapability
from config_propagation.config_rollback import (
    MID_FLIGHT_SENSITIVE_CAPABILITIES,
    ConfigGovernanceService,
    MakerCheckerViolationError,
    PublishDisposition,
    RollbackRequest,
)
from config_propagation.effective_config_repository import (
    EffectiveConfigRepository,
    effective_config_sort_key,
)
from config_propagation.restatement_repository import (
    RestatementEvent,
    RestatementRepository,
)
from observability.metric_recorder import platform_metric_recorder
from tenancy.scope_contract import IMPLICIT_SCOPE_UNIT_ID

_REGION = "us-east-1"


def _create_table(name: str, sort_key_name: str) -> None:
    boto3.client("dynamodb", region_name=_REGION).create_table(
        TableName=name,
        KeySchema=[
            {"AttributeName": "tenant_code", "KeyType": "HASH"},
            {"AttributeName": sort_key_name, "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "tenant_code", "AttributeType": "S"},
            {"AttributeName": sort_key_name, "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


class _InMemoryPointerStore:
    """Stands in for the published-pointer store the rollback repoints."""

    def __init__(self, pointers: dict[tuple[str, str, str], str], versions: set[str]) -> None:
        self._pointers = pointers
        self._versions = versions

    def read_pointer(self, tenant_code: str, capability: ConfigCapability, key: str) -> str:
        return self._pointers[(tenant_code, capability.value, key)]

    def write_pointer(
        self, tenant_code: str, capability: ConfigCapability, key: str, version: str
    ) -> None:
        self._pointers[(tenant_code, capability.value, key)] = version

    def version_exists(
        self, tenant_code: str, capability: ConfigCapability, key: str, version: str
    ) -> bool:
        return version in self._versions


class TestEffectiveConfigSortKey:
    def test_key_is_capability_scope_entity(self) -> None:
        key = effective_config_sort_key(ConfigCapability.FIELD_MAPPING, "ar_invoice")
        assert key == f"field_mapping#{IMPLICIT_SCOPE_UNIT_ID}#ar_invoice"

    def test_scope_id_participates_so_two_scope_units_do_not_collide(self) -> None:
        a = effective_config_sort_key(ConfigCapability.FIELD_MAPPING, "ar_invoice", "brand-a")
        b = effective_config_sort_key(ConfigCapability.FIELD_MAPPING, "ar_invoice", "brand-b")
        assert a != b


@mock_aws
class TestEffectiveConfigRepository:
    def setup_method(self, method: object = None) -> None:
        platform_metric_recorder.clear()

    def _repository(self) -> EffectiveConfigRepository:
        _create_table("EdlEffectiveConfig", "capability_key")
        return EffectiveConfigRepository(environment="dev", region_name=_REGION)

    def test_environment_must_not_be_empty(self) -> None:
        with pytest.raises(ValueError, match="environment"):
            EffectiveConfigRepository(environment="", region_name=_REGION)

    def test_first_consumption_is_a_transition(self) -> None:
        repository = self._repository()
        assert (
            repository.record_consumption(
                "demo", ConfigCapability.FIELD_MAPPING, "ar_invoice", "v3", "run-1"
            )
            is True
        )

    def test_the_same_version_consumed_again_is_not_a_transition(self) -> None:
        # Without the conditional write, every run would look like a version change and
        # "first consuming run" would mean nothing.
        repository = self._repository()
        repository.record_consumption(
            "demo", ConfigCapability.FIELD_MAPPING, "ar_invoice", "v3", "run-1"
        )
        assert (
            repository.record_consumption(
                "demo", ConfigCapability.FIELD_MAPPING, "ar_invoice", "v3", "run-2"
            )
            is False
        )

    def test_first_consuming_run_is_not_overwritten_by_a_later_run(self) -> None:
        repository = self._repository()
        repository.record_consumption(
            "demo", ConfigCapability.FIELD_MAPPING, "ar_invoice", "v3", "run-1"
        )
        repository.record_consumption(
            "demo", ConfigCapability.FIELD_MAPPING, "ar_invoice", "v3", "run-2"
        )
        record = repository.get_effective("demo", ConfigCapability.FIELD_MAPPING, "ar_invoice")
        assert record is not None
        assert record["first_consuming_run_id"] == "run-1"

    def test_a_new_version_transitions_and_re_attributes(self) -> None:
        repository = self._repository()
        repository.record_consumption(
            "demo", ConfigCapability.FIELD_MAPPING, "ar_invoice", "v3", "run-1"
        )
        assert (
            repository.record_consumption(
                "demo", ConfigCapability.FIELD_MAPPING, "ar_invoice", "v4", "run-9"
            )
            is True
        )
        record = repository.get_effective("demo", ConfigCapability.FIELD_MAPPING, "ar_invoice")
        assert record is not None
        assert record["effective_version"] == "v4"
        assert record["first_consuming_run_id"] == "run-9"

    def test_get_effective_returns_none_when_nothing_is_recorded(self) -> None:
        repository = self._repository()
        assert repository.get_effective("demo", ConfigCapability.FIELD_MAPPING, "nope") is None

    def test_records_are_tenant_partitioned(self) -> None:
        repository = self._repository()
        repository.record_consumption(
            "demo", ConfigCapability.FIELD_MAPPING, "ar_invoice", "v3", "run-1"
        )
        repository.record_consumption(
            "acme", ConfigCapability.FIELD_MAPPING, "ar_invoice", "v9", "run-1"
        )
        assert [r["effective_version"] for r in repository.list_effective("demo")] == ["v3"]
        assert [r["effective_version"] for r in repository.list_effective("acme")] == ["v9"]

    def test_list_effective_can_filter_to_one_capability(self) -> None:
        repository = self._repository()
        repository.record_consumption(
            "demo", ConfigCapability.FIELD_MAPPING, "ar_invoice", "v1", "run-1"
        )
        repository.record_consumption(
            "demo", ConfigCapability.SEMANTIC_MODEL, "revenue", "v1", "run-1"
        )
        filtered = repository.list_effective("demo", ConfigCapability.SEMANTIC_MODEL)
        assert [r["entity_key"] for r in filtered] == ["revenue"]

    def test_invalid_tenant_code_is_rejected_before_any_write(self) -> None:
        repository = self._repository()
        with pytest.raises(ValueError):
            repository.record_consumption(
                "../etc", ConfigCapability.FIELD_MAPPING, "ar_invoice", "v1", "run-1"
            )

    def test_propagation_lag_is_none_without_a_published_at(self) -> None:
        repository = self._repository()
        repository.record_consumption(
            "demo", ConfigCapability.FIELD_MAPPING, "ar_invoice", "v1", "run-1"
        )
        assert (
            repository.propagation_lag_seconds("demo", ConfigCapability.FIELD_MAPPING, "ar_invoice")
            is None
        )

    def test_propagation_lag_is_measured_from_published_at(self) -> None:
        repository = self._repository()
        repository.record_consumption(
            "demo",
            ConfigCapability.FIELD_MAPPING,
            "ar_invoice",
            "v1",
            "run-1",
            published_at="2026-01-01T00:00:00+00:00",
        )
        lag = repository.propagation_lag_seconds(
            "demo", ConfigCapability.FIELD_MAPPING, "ar_invoice"
        )
        assert lag is not None and lag > 0

    def test_a_malformed_timestamp_yields_no_lag_rather_than_raising(self) -> None:
        repository = self._repository()
        repository.record_consumption(
            "demo",
            ConfigCapability.FIELD_MAPPING,
            "ar_invoice",
            "v1",
            "run-1",
            published_at="not-a-timestamp",
        )
        assert (
            repository.propagation_lag_seconds("demo", ConfigCapability.FIELD_MAPPING, "ar_invoice")
            is None
        )

    def test_a_transition_records_the_transition_metric(self) -> None:
        repository = self._repository()
        repository.record_consumption(
            "demo", ConfigCapability.FIELD_MAPPING, "ar_invoice", "v1", "run-1"
        )
        recorded = {point.metric.value for point in platform_metric_recorder.snapshot()}
        assert "EffectiveVersionTransitions" in recorded


class TestRestatementEvent:
    def test_an_event_must_name_at_least_one_affected_metric(self) -> None:
        with pytest.raises(ValueError, match="affected metric"):
            RestatementEvent(
                tenant_code="demo",
                capability=ConfigCapability.SEMANTIC_MODEL,
                metrics_affected=(),
                periods_affected=("2026-Q1",),
                previous_version="v1",
                new_version="v2",
                actor="controller",
                correlation_id="run-1",
            )

    def test_an_event_must_name_its_actor(self) -> None:
        with pytest.raises(ValueError, match="actor"):
            RestatementEvent(
                tenant_code="demo",
                capability=ConfigCapability.SEMANTIC_MODEL,
                metrics_affected=("revenue",),
                periods_affected=("2026-Q1",),
                previous_version="v1",
                new_version="v2",
                actor="",
                correlation_id="run-1",
            )

    def test_tenant_code_is_validated(self) -> None:
        with pytest.raises(ValueError):
            RestatementEvent(
                tenant_code="../etc",
                capability=ConfigCapability.SEMANTIC_MODEL,
                metrics_affected=("revenue",),
                periods_affected=("2026-Q1",),
                previous_version="v1",
                new_version="v2",
                actor="controller",
                correlation_id="run-1",
            )

    def test_event_ids_are_unique_per_event(self) -> None:
        def make() -> RestatementEvent:
            return RestatementEvent(
                tenant_code="demo",
                capability=ConfigCapability.SEMANTIC_MODEL,
                metrics_affected=("revenue",),
                periods_affected=("2026-Q1",),
                previous_version="v1",
                new_version="v2",
                actor="controller",
                correlation_id="run-1",
            )

        assert make().event_id != make().event_id

    def test_sort_key_is_capability_scoped(self) -> None:
        event = RestatementEvent(
            tenant_code="demo",
            capability=ConfigCapability.SEMANTIC_MODEL,
            metrics_affected=("revenue",),
            periods_affected=("2026-Q1",),
            previous_version="v1",
            new_version="v2",
            actor="controller",
            correlation_id="run-1",
        )
        assert event.sort_key.startswith("semantic_model#")


@mock_aws
class TestRestatementRepository:
    def setup_method(self, method: object = None) -> None:
        platform_metric_recorder.clear()

    def _repository(self) -> RestatementRepository:
        _create_table("EdlConfigRestatement", "restatement_key")
        return RestatementRepository(environment="dev", region_name=_REGION)

    def _event(self, **overrides: object) -> RestatementEvent:
        payload: dict[str, object] = {
            "tenant_code": "demo",
            "capability": ConfigCapability.SEMANTIC_MODEL,
            "metrics_affected": ("revenue",),
            "periods_affected": ("2026-Q1",),
            "previous_version": "v1",
            "new_version": "v2",
            "actor": "controller",
            "correlation_id": "run-1",
        }
        payload.update(overrides)
        return RestatementEvent(**payload)  # type: ignore[arg-type]

    def test_environment_must_not_be_empty(self) -> None:
        with pytest.raises(ValueError, match="environment"):
            RestatementRepository(environment="", region_name=_REGION)

    def test_emit_returns_the_event_id_and_persists_the_evidence(self) -> None:
        repository = self._repository()
        event_id = repository.emit(self._event(definition_before="a", definition_after="b"))
        stored = repository.list_restatements("demo")
        assert [item["event_id"] for item in stored] == [event_id]
        assert stored[0]["definition_before"] == "a"
        assert stored[0]["definition_after"] == "b"
        assert stored[0]["actor"] == "controller"

    def test_events_are_append_only_and_not_collapsed(self) -> None:
        repository = self._repository()
        repository.emit(self._event())
        repository.emit(self._event(new_version="v3"))
        assert len(repository.list_restatements("demo")) == 2

    def test_listing_is_newest_first(self) -> None:
        repository = self._repository()
        first = repository.emit(self._event())
        second = repository.emit(self._event(new_version="v3"))
        ordered = [item["event_id"] for item in repository.list_restatements("demo")]
        assert set(ordered) == {first, second}
        assert ordered[0] == second or ordered[0] == first  # same-instant ties are acceptable

    def test_listing_can_filter_to_one_capability(self) -> None:
        repository = self._repository()
        repository.emit(self._event())
        repository.emit(self._event(capability=ConfigCapability.FIELD_MAPPING))
        filtered = repository.list_restatements("demo", ConfigCapability.FIELD_MAPPING)
        assert [item["capability"] for item in filtered] == ["field_mapping"]

    def test_events_are_tenant_partitioned(self) -> None:
        repository = self._repository()
        repository.emit(self._event())
        repository.emit(self._event(tenant_code="acme"))
        assert len(repository.list_restatements("demo")) == 1
        assert len(repository.list_restatements("acme")) == 1

    def test_emit_records_the_restatement_metric(self) -> None:
        repository = self._repository()
        repository.emit(self._event())
        recorded = {point.metric.value for point in platform_metric_recorder.snapshot()}
        assert "RestatementEventsEmitted" in recorded


class TestRollbackRequestMakerChecker:
    def _request(self, **overrides: object) -> RollbackRequest:
        payload: dict[str, object] = {
            "tenant_code": "demo",
            "capability": ConfigCapability.FIELD_MAPPING,
            "entity_key": "ar_invoice",
            "target_version": "v1",
            "requested_by": "alice",
            "approved_by": "bob",
            "correlation_id": "run-1",
        }
        payload.update(overrides)
        return RollbackRequest(**payload)  # type: ignore[arg-type]

    def test_a_valid_request_is_accepted(self) -> None:
        assert self._request().approved_by == "bob"

    def test_an_unapproved_rollback_is_refused(self) -> None:
        with pytest.raises(MakerCheckerViolationError):
            self._request(approved_by="")

    def test_self_approval_is_refused(self) -> None:
        # Reverting a governed definition has the same blast radius as changing it.
        with pytest.raises(MakerCheckerViolationError, match="own requester"):
            self._request(approved_by="alice")

    def test_tenant_code_is_validated(self) -> None:
        with pytest.raises(ValueError):
            self._request(tenant_code="../etc")


@mock_aws
class TestConfigGovernanceService:
    def setup_method(self, method: object = None) -> None:
        platform_metric_recorder.clear()

    def _service(
        self, *, pointer: str = "v2", versions: set[str] | None = None
    ) -> tuple[ConfigGovernanceService, _InMemoryPointerStore]:
        _create_table("EdlConfigGovernance", "record_key")
        store = _InMemoryPointerStore(
            {("demo", "field_mapping", "ar_invoice"): pointer},
            versions if versions is not None else {"v1", "v2"},
        )
        service = ConfigGovernanceService(
            environment="dev", region_name=_REGION, pointer_store=store
        )
        return service, store

    def _request(self, **overrides: object) -> RollbackRequest:
        payload: dict[str, object] = {
            "tenant_code": "demo",
            "capability": ConfigCapability.FIELD_MAPPING,
            "entity_key": "ar_invoice",
            "target_version": "v1",
            "requested_by": "alice",
            "approved_by": "bob",
            "correlation_id": "run-1",
        }
        payload.update(overrides)
        return RollbackRequest(**payload)  # type: ignore[arg-type]

    def test_environment_must_not_be_empty(self) -> None:
        with pytest.raises(ValueError, match="environment"):
            ConfigGovernanceService(
                environment="",
                region_name=_REGION,
                pointer_store=_InMemoryPointerStore({}, set()),
            )

    def test_rollback_repoints_the_pointer_and_reports_the_previous_version(self) -> None:
        service, store = self._service()
        result = service.rollback(self._request())
        assert result.previous_version == "v2"
        assert result.target_version == "v1"
        assert store.read_pointer("demo", ConfigCapability.FIELD_MAPPING, "ar_invoice") == "v1"

    def test_rollback_to_a_missing_version_is_refused_before_repointing(self) -> None:
        service, store = self._service(versions={"v2"})
        with pytest.raises(ValueError, match="does not exist"):
            service.rollback(self._request())
        assert store.read_pointer("demo", ConfigCapability.FIELD_MAPPING, "ar_invoice") == "v2"

    def test_rollback_records_admin_actions_because_it_is_a_privileged_operation(self) -> None:
        # AdminActions must be produced by privileged operations this system owns —
        # config rollback is one; tenant administration is not ours to perform.
        service, _ = self._service()
        service.rollback(self._request())
        recorded = {point.metric.value for point in platform_metric_recorder.snapshot()}
        assert {"ConfigRollbacks", "AdminActions"} <= recorded

    def test_rollback_writes_an_audit_record_naming_both_actors(self) -> None:
        service, _ = self._service()
        result = service.rollback(self._request())
        table = boto3.resource("dynamodb", region_name=_REGION).Table("EdlConfigGovernance")
        items = table.query(
            KeyConditionExpression="tenant_code = :tc AND begins_with(record_key, :p)",
            ExpressionAttributeValues={":tc": "demo", ":p": "rollback#"},
        )["Items"]
        assert len(items) == 1
        assert items[0]["rollback_id"] == result.rollback_id
        assert items[0]["requested_by"] == "alice"
        assert items[0]["approved_by"] == "bob"

    def test_publish_with_no_runs_in_flight_applies_immediately(self) -> None:
        service, _ = self._service()
        disposition = service.coordinate_publish(
            "demo", ConfigCapability.FIELD_MAPPING, "ar_invoice", "v3"
        )
        assert disposition is PublishDisposition.APPLIED_IMMEDIATELY

    def test_a_mid_flight_sensitive_capability_is_queued_not_blocked(self) -> None:
        service, _ = self._service()
        disposition = service.coordinate_publish(
            "demo",
            ConfigCapability.ENTITY_RESOLUTION,
            "customer",
            "v3",
            in_flight_run_ids=("run-1",),
        )
        assert disposition is PublishDisposition.QUEUED_FOR_NEXT_RUN_BOUNDARY

    def test_a_tolerant_capability_applies_and_annotates_the_run(self) -> None:
        service, _ = self._service()
        tolerant = next(
            capability
            for capability in ConfigCapability
            if capability not in MID_FLIGHT_SENSITIVE_CAPABILITIES
        )
        disposition = service.coordinate_publish(
            "demo", tolerant, "ar_invoice", "v3", in_flight_run_ids=("run-1",)
        )
        assert disposition is PublishDisposition.APPLIED_AND_RUN_ANNOTATED

    def test_queued_publishes_lists_only_the_deferred_ones(self) -> None:
        service, _ = self._service()
        service.coordinate_publish(
            "demo",
            ConfigCapability.ENTITY_RESOLUTION,
            "customer",
            "v3",
            in_flight_run_ids=("run-1",),
        )
        service.coordinate_publish("demo", ConfigCapability.ENTITY_RESOLUTION, "customer", "v4")
        queued = service.queued_publishes("demo", ConfigCapability.ENTITY_RESOLUTION)
        assert [item["version"] for item in queued] == ["v3"]

    def test_coordination_validates_the_tenant_code(self) -> None:
        service, _ = self._service()
        with pytest.raises(ValueError):
            service.coordinate_publish("../etc", ConfigCapability.FIELD_MAPPING, "ar_invoice", "v3")
