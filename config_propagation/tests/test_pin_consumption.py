"""
Stage-side pin consumption (DL-CFG-01, DL-CFG-08).

The audit found DL-11 complete and inert: nothing pinned, nothing recorded, so
`ConfigVersionMismatchWithinRun` could never fire and the exit gate "pinning proven under a
mid-run publish" could not be evaluated. These tests drive the consumption path directly.
"""

from __future__ import annotations

from datetime import UTC, datetime

import boto3
import pytest
from moto import mock_aws

from config_propagation.capability import ConfigCapability
from config_propagation.pin_consumption import consume_pinned_config
from config_propagation.pinned_versions import (
    ConfigVersionMismatchError,
    PinnedConfigVersions,
)
from conftest import RESOURCE_NAME_ENVIRONMENT
from observability.metric_recorder import platform_metric_recorder

_REGION = "us-east-1"


def _pin(version: str) -> PinnedConfigVersions:
    return PinnedConfigVersions(
        versions={ConfigCapability.ENTITY_RESOLUTION.value: version},
        pinned_at=datetime.now(UTC).isoformat(),
    )


def _create_effective_config_table() -> None:
    boto3.client("dynamodb", region_name=_REGION).create_table(
        TableName=RESOURCE_NAME_ENVIRONMENT["EFFECTIVE_CONFIG_TABLE"],
        KeySchema=[
            {"AttributeName": "tenant_code", "KeyType": "HASH"},
            {"AttributeName": "capability_key", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "tenant_code", "AttributeType": "S"},
            {"AttributeName": "capability_key", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _consume(**overrides: object) -> bool:
    payload: dict[str, object] = {
        "pinned": _pin("v3"),
        "capability": ConfigCapability.ENTITY_RESOLUTION,
        "observed_version": "v3",
        "tenant_code": "demo",
        "entity_key": "company",
        "run_id": "run-1",
        "environment": "dev",
        "region_name": _REGION,
    }
    payload.update(overrides)
    return consume_pinned_config(**payload)  # type: ignore[arg-type]


@mock_aws
class TestPinConsumption:
    def setup_method(self, method: object = None) -> None:
        platform_metric_recorder.clear()

    def test_a_matching_version_is_recorded_as_effective(self) -> None:
        _create_effective_config_table()
        assert _consume() is True
        recorded = {point.metric.value for point in platform_metric_recorder.snapshot()}
        assert "EffectiveVersionTransitions" in recorded

    def test_the_effective_record_names_the_first_consuming_run(self) -> None:
        _create_effective_config_table()
        _consume(run_id="run-1")
        _consume(run_id="run-2")
        table = boto3.resource("dynamodb", region_name=_REGION).Table(
            RESOURCE_NAME_ENVIRONMENT["EFFECTIVE_CONFIG_TABLE"]
        )
        items = table.query(
            KeyConditionExpression="tenant_code = :tc",
            ExpressionAttributeValues={":tc": "demo"},
        )["Items"]
        assert len(items) == 1
        assert items[0]["first_consuming_run_id"] == "run-1"

    def test_a_mid_run_publish_is_detected_as_a_mismatch(self) -> None:
        _create_effective_config_table()
        assert _consume(observed_version="v4") is False
        recorded = {point.metric.value for point in platform_metric_recorder.snapshot()}
        assert "ConfigVersionMismatchWithinRun" in recorded

    def test_a_mismatch_still_records_what_was_actually_consumed(self) -> None:
        _create_effective_config_table()
        _consume(observed_version="v4")
        table = boto3.resource("dynamodb", region_name=_REGION).Table(
            RESOURCE_NAME_ENVIRONMENT["EFFECTIVE_CONFIG_TABLE"]
        )
        items = table.query(
            KeyConditionExpression="tenant_code = :tc",
            ExpressionAttributeValues={":tc": "demo"},
        )["Items"]
        assert items[0]["effective_version"] == "v4"

    def test_fail_on_mismatch_raises_for_a_capability_that_cannot_tolerate_it(self) -> None:
        _create_effective_config_table()
        with pytest.raises(ConfigVersionMismatchError):
            _consume(observed_version="v4", fail_on_mismatch=True)

    def test_an_absent_pin_is_tolerated(self) -> None:
        _create_effective_config_table()
        assert _consume(pinned=None) is True

    def test_a_missing_audit_table_does_not_fail_the_stage(self) -> None:
        assert _consume() is True

    def test_a_capability_absent_from_the_pin_is_not_a_mismatch(self) -> None:
        _create_effective_config_table()
        assert _consume(capability=ConfigCapability.FIELD_MAPPING) is True


class TestPinPayloadRoundTrip:
    def test_the_pin_survives_the_step_functions_payload(self) -> None:
        original = _pin("v7")
        restored = PinnedConfigVersions.from_payload(original.to_payload())
        assert restored is not None
        assert restored.require(ConfigCapability.ENTITY_RESOLUTION) == "v7"

    def test_an_absent_payload_restores_to_none(self) -> None:
        assert PinnedConfigVersions.from_payload(None) is None

    def test_the_fingerprint_changes_when_a_version_changes(self) -> None:
        assert _pin("v1").audit_fingerprint() != _pin("v2").audit_fingerprint()
