"""
Tests for ExtractionScheduleClient.

Coverage:
  - create_or_update_schedule() creates a new schedule when none exists
  - create_or_update_schedule() updates an existing schedule
  - create_or_update_schedule() passes connector_params in Step Functions input
  - create_or_update_schedule() uses correct cron expression
  - create_or_update_schedule() returns schedule ARN
  - delete_schedule() deletes an existing schedule
  - delete_schedule() raises ScheduleNotFoundError when schedule does not exist
  - get_schedule() returns schedule data when schedule exists
  - get_schedule() returns None when schedule does not exist
  - build_schedule_name() produces deterministic names with double-hyphen separator
  - create_or_update_schedule() rejects invalid source_id / entity_id
  - Constructor rejects empty schedule_group_name, target_arn, execution_role_arn
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from orchestration.event_bridge.extraction_schedule_client import (
    ExtractionScheduleClient,
    ScheduleNotFoundError,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_GROUP = "dev-extraction-schedules"
_TARGET_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:extraction-pipeline"
_ROLE_ARN = "arn:aws:iam::123456789012:role/dev-eventbridge-scheduler-role"
_REGION = "us-east-1"
_ENV = "dev"
_SOURCE = "mysql-rds"
_ENTITY = "mysql-rds-orders"
_CRON = "cron(0 1 * * ? *)"
_CONNECTOR_PARAMS = {"table_name": "orders"}
_SCHEDULE_ARN = (
    "arn:aws:scheduler:us-east-1:123456789012:schedule"
    "/dev-extraction-schedules/mysql-rds--mysql-rds-orders"
)


def _resource_not_found_error() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "Not found"}},
        "GetSchedule",
    )


def _make_client() -> tuple[ExtractionScheduleClient, MagicMock]:
    """Return client + mock EventBridge Scheduler boto3 client."""
    mock_scheduler = MagicMock()

    with patch("boto3.client", return_value=mock_scheduler):
        client = ExtractionScheduleClient(
            schedule_group_name=_GROUP,
            target_arn=_TARGET_ARN,
            execution_role_arn=_ROLE_ARN,
            environment=_ENV,
            region_name=_REGION,
        )
    return client, mock_scheduler


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCreateOrUpdateSchedule:
    def test_creates_new_schedule_when_not_found(self) -> None:
        client, mock_scheduler = _make_client()
        # update raises ResourceNotFoundException (schedule doesn't exist yet)
        mock_scheduler.update_schedule.side_effect = _resource_not_found_error()
        mock_scheduler.create_schedule.return_value = {"ScheduleArn": _SCHEDULE_ARN}

        arn = client.create_or_update_schedule(_SOURCE, _ENTITY, _CRON, _CONNECTOR_PARAMS)

        mock_scheduler.create_schedule.assert_called_once()
        assert arn == _SCHEDULE_ARN

    def test_updates_existing_schedule(self) -> None:
        client, mock_scheduler = _make_client()
        mock_scheduler.update_schedule.return_value = {"ScheduleArn": _SCHEDULE_ARN}

        arn = client.create_or_update_schedule(_SOURCE, _ENTITY, _CRON, _CONNECTOR_PARAMS)

        mock_scheduler.update_schedule.assert_called_once()
        mock_scheduler.create_schedule.assert_not_called()
        assert arn == _SCHEDULE_ARN

    def test_connector_params_in_sfn_input(self) -> None:
        client, mock_scheduler = _make_client()
        mock_scheduler.update_schedule.side_effect = _resource_not_found_error()
        mock_scheduler.create_schedule.return_value = {"ScheduleArn": _SCHEDULE_ARN}

        client.create_or_update_schedule(_SOURCE, _ENTITY, _CRON, _CONNECTOR_PARAMS)

        call_kwargs = mock_scheduler.create_schedule.call_args[1]
        target_input = json.loads(call_kwargs["Target"]["Input"])
        assert target_input["connector_params"] == _CONNECTOR_PARAMS

    def test_source_and_entity_in_sfn_input(self) -> None:
        client, mock_scheduler = _make_client()
        mock_scheduler.update_schedule.side_effect = _resource_not_found_error()
        mock_scheduler.create_schedule.return_value = {"ScheduleArn": _SCHEDULE_ARN}

        client.create_or_update_schedule(_SOURCE, _ENTITY, _CRON, _CONNECTOR_PARAMS)

        call_kwargs = mock_scheduler.create_schedule.call_args[1]
        target_input = json.loads(call_kwargs["Target"]["Input"])
        assert target_input["source_id"] == _SOURCE
        assert target_input["entity_id"] == _ENTITY
        assert target_input["is_replay"] is False

    def test_environment_in_sfn_input(self) -> None:
        client, mock_scheduler = _make_client()
        mock_scheduler.update_schedule.side_effect = _resource_not_found_error()
        mock_scheduler.create_schedule.return_value = {"ScheduleArn": _SCHEDULE_ARN}

        client.create_or_update_schedule(_SOURCE, _ENTITY, _CRON, _CONNECTOR_PARAMS)

        call_kwargs = mock_scheduler.create_schedule.call_args[1]
        target_input = json.loads(call_kwargs["Target"]["Input"])
        assert target_input["environment"] == _ENV

    def test_cron_expression_passed_to_api(self) -> None:
        client, mock_scheduler = _make_client()
        mock_scheduler.update_schedule.side_effect = _resource_not_found_error()
        mock_scheduler.create_schedule.return_value = {"ScheduleArn": _SCHEDULE_ARN}

        client.create_or_update_schedule(_SOURCE, _ENTITY, _CRON, _CONNECTOR_PARAMS)

        call_kwargs = mock_scheduler.create_schedule.call_args[1]
        assert call_kwargs["ScheduleExpression"] == _CRON

    def test_schedule_enabled_by_default(self) -> None:
        client, mock_scheduler = _make_client()
        mock_scheduler.update_schedule.side_effect = _resource_not_found_error()
        mock_scheduler.create_schedule.return_value = {"ScheduleArn": _SCHEDULE_ARN}

        client.create_or_update_schedule(_SOURCE, _ENTITY, _CRON, _CONNECTOR_PARAMS)

        call_kwargs = mock_scheduler.create_schedule.call_args[1]
        assert call_kwargs["State"] == "ENABLED"

    def test_invalid_source_id_raises(self) -> None:
        client, _ = _make_client()
        with pytest.raises(ValueError, match="source_id"):
            client.create_or_update_schedule("Invalid_Source!", _ENTITY, _CRON, {})

    def test_invalid_entity_id_raises(self) -> None:
        client, _ = _make_client()
        with pytest.raises(ValueError, match="entity_id"):
            client.create_or_update_schedule(_SOURCE, "UPPER-CASE", _CRON, {})

    def test_non_resource_not_found_error_propagates(self) -> None:
        client, mock_scheduler = _make_client()
        access_denied = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "Denied"}},
            "UpdateSchedule",
        )
        mock_scheduler.update_schedule.side_effect = access_denied

        with pytest.raises(ClientError):
            client.create_or_update_schedule(_SOURCE, _ENTITY, _CRON, _CONNECTOR_PARAMS)


class TestDeleteSchedule:
    def test_deletes_existing_schedule(self) -> None:
        client, mock_scheduler = _make_client()
        mock_scheduler.delete_schedule.return_value = {}

        client.delete_schedule(_SOURCE, _ENTITY)

        mock_scheduler.delete_schedule.assert_called_once()
        call_kwargs = mock_scheduler.delete_schedule.call_args[1]
        assert call_kwargs["GroupName"] == _GROUP
        assert _SOURCE in call_kwargs["Name"]
        assert _ENTITY in call_kwargs["Name"]

    def test_not_found_raises_schedule_not_found_error(self) -> None:
        client, mock_scheduler = _make_client()
        mock_scheduler.delete_schedule.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "Not found"}},
            "DeleteSchedule",
        )

        with pytest.raises(ScheduleNotFoundError):
            client.delete_schedule(_SOURCE, _ENTITY)

    def test_other_client_error_propagates(self) -> None:
        client, mock_scheduler = _make_client()
        mock_scheduler.delete_schedule.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Too many requests"}},
            "DeleteSchedule",
        )

        with pytest.raises(ClientError):
            client.delete_schedule(_SOURCE, _ENTITY)


class TestGetSchedule:
    def test_returns_schedule_when_exists(self) -> None:
        client, mock_scheduler = _make_client()
        mock_scheduler.get_schedule.return_value = {
            "Name": f"{_SOURCE}--{_ENTITY}",
            "ScheduleExpression": _CRON,
            "State": "ENABLED",
        }

        result = client.get_schedule(_SOURCE, _ENTITY)

        assert result is not None
        assert result["State"] == "ENABLED"

    def test_returns_none_when_not_found(self) -> None:
        client, mock_scheduler = _make_client()
        mock_scheduler.get_schedule.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "Not found"}},
            "GetSchedule",
        )

        result = client.get_schedule(_SOURCE, _ENTITY)
        assert result is None

    def test_non_resource_error_propagates(self) -> None:
        client, mock_scheduler = _make_client()
        mock_scheduler.get_schedule.side_effect = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "Bad request"}},
            "GetSchedule",
        )

        with pytest.raises(ClientError):
            client.get_schedule(_SOURCE, _ENTITY)


class TestScheduleNameConstruction:
    def test_schedule_name_uses_double_hyphen_separator(self) -> None:
        name = ExtractionScheduleClient.build_schedule_name(
            "salesforce", "salesforce-account", tenant_code="demo"
        )
        assert name == "demo--salesforce--salesforce-account"

    def test_schedule_name_double_hyphen_separates_all_three_components(self) -> None:
        name = ExtractionScheduleClient.build_schedule_name(
            "mysql-rds", "mysql-rds-orders", tenant_code="demo"
        )
        # tenant / source (mysql-rds) / entity (mysql-rds-orders) must all be separable.
        parts = name.split("--")
        assert parts == ["demo", "mysql-rds", "mysql-rds-orders"]

    def test_schedule_name_prefixed_by_tenant_code(self) -> None:
        """ARCH-1: two tenants onboarding the same source/entity get distinct names."""
        name_a = ExtractionScheduleClient.build_schedule_name(
            "salesforce", "salesforce-account", tenant_code="acme-corp"
        )
        name_b = ExtractionScheduleClient.build_schedule_name(
            "salesforce", "salesforce-account", tenant_code="globex-eu"
        )
        assert name_a != name_b
        assert name_a == "acme-corp--salesforce--salesforce-account"

    def test_tenant_source_delimiter_collision_is_fixed(self) -> None:
        """
        ARCH-16 regression: a single hyphen between tenant_code and source_id
        let tenant="acme"/source="corp-salesforce" collide with
        tenant="acme-corp"/source="salesforce" on the identical literal name.
        Since create_or_update_schedule() is update-first, that collision was
        a silent cross-tenant schedule clobber. The double-hyphen separator
        between all three components must keep them distinct.
        """
        name_a = ExtractionScheduleClient.build_schedule_name(
            "corp-salesforce", "some-entity", tenant_code="acme"
        )
        name_b = ExtractionScheduleClient.build_schedule_name(
            "salesforce", "some-entity", tenant_code="acme-corp"
        )
        assert name_a != name_b
        assert name_a == "acme--corp-salesforce--some-entity"
        assert name_b == "acme-corp--salesforce--some-entity"

    def test_max_length_ids_produce_name_within_64_chars(self) -> None:
        """EventBridge Scheduler caps schedule names at 64 chars; max-length
        tenant_code (48) + source_id (64) + entity_id (64) joined would
        otherwise exceed that by a wide margin — the fallback must clamp to
        64 chars deterministically rather than let the boto3 call fail."""
        long_tenant = "a" + "b" * 47  # 48 chars
        long_source = "c" + "d" * 63  # 64 chars
        long_entity = "e" + "f" * 63  # 64 chars

        name = ExtractionScheduleClient.build_schedule_name(
            long_source, long_entity, tenant_code=long_tenant
        )
        assert len(name) <= 64

        # Deterministic: same inputs always produce the same clamped name.
        name_again = ExtractionScheduleClient.build_schedule_name(
            long_source, long_entity, tenant_code=long_tenant
        )
        assert name == name_again

    def test_max_length_ids_stay_distinct_across_tenants(self) -> None:
        """Two different max-length tenants with the same source/entity must
        not collapse onto the same 64-char clamped name."""
        long_source = "c" + "d" * 63
        long_entity = "e" + "f" * 63
        tenant_a = "a" + "a" * 47
        tenant_b = "a" + "b" * 47

        name_a = ExtractionScheduleClient.build_schedule_name(
            long_source, long_entity, tenant_code=tenant_a
        )
        name_b = ExtractionScheduleClient.build_schedule_name(
            long_source, long_entity, tenant_code=tenant_b
        )
        assert name_a != name_b
        assert len(name_a) <= 64
        assert len(name_b) <= 64


class TestScheduleOperationsTenantConsistency:
    """
    ARCH-1 coverage gap fix: TestScheduleNameConstruction only proved the
    static build_schedule_name() helper honors tenant_code. It never proved
    create_or_update_schedule() / get_schedule() / delete_schedule() — the
    actual operational methods callers use — build the SAME tenant-prefixed
    name, or that they stay distinguishable for a non-default tenant. A
    regression that desynced one of these methods from build_schedule_name()
    would have gone uncaught.
    """

    _TENANT = "acme-corp"

    def test_create_uses_tenant_prefixed_name(self) -> None:
        client, mock_scheduler = _make_client()
        mock_scheduler.update_schedule.side_effect = _resource_not_found_error()
        mock_scheduler.create_schedule.return_value = {"ScheduleArn": _SCHEDULE_ARN}

        client.create_or_update_schedule(
            _SOURCE, _ENTITY, _CRON, _CONNECTOR_PARAMS, tenant_code=self._TENANT
        )

        call_kwargs = mock_scheduler.create_schedule.call_args[1]
        assert call_kwargs["Name"] == ExtractionScheduleClient.build_schedule_name(
            _SOURCE, _ENTITY, tenant_code=self._TENANT
        )

    def test_get_uses_tenant_prefixed_name(self) -> None:
        client, mock_scheduler = _make_client()
        mock_scheduler.get_schedule.return_value = {"State": "ENABLED"}

        client.get_schedule(_SOURCE, _ENTITY, tenant_code=self._TENANT)

        call_kwargs = mock_scheduler.get_schedule.call_args[1]
        assert call_kwargs["Name"] == ExtractionScheduleClient.build_schedule_name(
            _SOURCE, _ENTITY, tenant_code=self._TENANT
        )

    def test_delete_uses_tenant_prefixed_name(self) -> None:
        client, mock_scheduler = _make_client()
        mock_scheduler.delete_schedule.return_value = {}

        client.delete_schedule(_SOURCE, _ENTITY, tenant_code=self._TENANT)

        call_kwargs = mock_scheduler.delete_schedule.call_args[1]
        assert call_kwargs["Name"] == ExtractionScheduleClient.build_schedule_name(
            _SOURCE, _ENTITY, tenant_code=self._TENANT
        )

    def test_two_tenants_get_distinct_schedule_names_via_delete(self) -> None:
        """End-to-end proof through the public API, not just the static helper."""
        client, mock_scheduler = _make_client()
        mock_scheduler.delete_schedule.return_value = {}

        client.delete_schedule(_SOURCE, _ENTITY, tenant_code="acme-corp")
        name_a = mock_scheduler.delete_schedule.call_args[1]["Name"]

        client.delete_schedule(_SOURCE, _ENTITY, tenant_code="globex-eu")
        name_b = mock_scheduler.delete_schedule.call_args[1]["Name"]

        assert name_a != name_b


class TestConstructorValidation:
    def test_empty_schedule_group_name_raises(self) -> None:
        with pytest.raises(ValueError, match="schedule_group_name"):
            ExtractionScheduleClient(
                schedule_group_name="",
                target_arn=_TARGET_ARN,
                execution_role_arn=_ROLE_ARN,
                environment=_ENV,
                region_name=_REGION,
            )

    def test_empty_target_arn_raises(self) -> None:
        with pytest.raises(ValueError, match="target_arn"):
            ExtractionScheduleClient(
                schedule_group_name=_GROUP,
                target_arn="",
                execution_role_arn=_ROLE_ARN,
                environment=_ENV,
                region_name=_REGION,
            )

    def test_empty_execution_role_arn_raises(self) -> None:
        with pytest.raises(ValueError, match="execution_role_arn"):
            ExtractionScheduleClient(
                schedule_group_name=_GROUP,
                target_arn=_TARGET_ARN,
                execution_role_arn="",
                environment=_ENV,
                region_name=_REGION,
            )

    def test_empty_environment_raises(self) -> None:
        with pytest.raises(ValueError, match="environment"):
            ExtractionScheduleClient(
                schedule_group_name=_GROUP,
                target_arn=_TARGET_ARN,
                execution_role_arn=_ROLE_ARN,
                environment="",
                region_name=_REGION,
            )
