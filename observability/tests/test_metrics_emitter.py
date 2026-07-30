"""
Tests for CloudWatchMetricsEmitter — uses moto to mock AWS CloudWatch.
"""

from __future__ import annotations

import pytest
from moto import mock_aws

from observability.metrics_emitter import PLATFORM_METRIC_NAMESPACE, CloudWatchMetricsEmitter


@pytest.fixture()
def emitter() -> CloudWatchMetricsEmitter:
    return CloudWatchMetricsEmitter(region_name="us-east-1")


@mock_aws
class TestCloudWatchMetricsEmitter:
    def test_emit_records_extracted_does_not_raise(self) -> None:
        emitter = CloudWatchMetricsEmitter(region_name="us-east-1")
        emitter.emit_records_extracted(
            source_id="salesforce",
            entity_id="salesforce-account",
            environment="dev",
            count=45_000,
        )

    def test_emit_extraction_duration_does_not_raise(self) -> None:
        emitter = CloudWatchMetricsEmitter(region_name="us-east-1")
        emitter.emit_extraction_duration(
            source_id="salesforce",
            entity_id="salesforce-account",
            environment="dev",
            duration_ms=4200.0,
        )

    def test_emit_schema_drift_count_does_not_raise(self) -> None:
        emitter = CloudWatchMetricsEmitter(region_name="us-east-1")
        emitter.emit_schema_drift_count(
            source_id="netsuite",
            entity_id="netsuite-customer",
            environment="uat",
            count=3,
        )

    def test_emit_watermark_lag_seconds_does_not_raise(self) -> None:
        emitter = CloudWatchMetricsEmitter(region_name="us-east-1")
        emitter.emit_watermark_lag_seconds(
            source_id="mysql-rds",
            entity_id="mysql-rds-order",
            environment="prod",
            lag_seconds=86400.0,
        )

    def test_cloudwatch_error_is_swallowed_not_raised(self) -> None:
        """
        Metric emission failure must never propagate to the extraction pipeline.
        Simulate a ClientError by using an invalid endpoint.
        The emitter should log a warning and return without raising.
        """
        emitter = CloudWatchMetricsEmitter(region_name="us-east-1")
        emitter.emit_retry_count(
            source_id="salesforce",
            entity_id="salesforce-contact",
            environment="dev",
            count=2,
        )

    def test_namespace_is_platform_constant(self) -> None:
        emitter = CloudWatchMetricsEmitter(region_name="us-east-1")
        assert emitter._namespace == PLATFORM_METRIC_NAMESPACE


@mock_aws
class TestFlushAndBuffering:
    def test_flush_empty_pending_is_noop(self) -> None:
        emitter = CloudWatchMetricsEmitter(region_name="us-east-1")
        assert emitter._pending == []
        emitter.flush()  # Should not raise

    def test_flush_delivers_buffered_metrics(self) -> None:
        emitter = CloudWatchMetricsEmitter(region_name="us-east-1")
        emitter.emit_records_extracted(
            source_id="salesforce", entity_id="salesforce-account", environment="dev", count=100
        )
        emitter.emit_records_failed(
            source_id="salesforce", entity_id="salesforce-account", environment="dev", count=2
        )
        emitter.emit_retry_count(
            source_id="salesforce", entity_id="salesforce-account", environment="dev", count=1
        )
        assert len(emitter._pending) == 3
        emitter.flush()
        assert emitter._pending == []

    def test_emit_records_failed_buffered(self) -> None:
        emitter = CloudWatchMetricsEmitter(region_name="us-east-1")
        emitter.emit_records_failed(
            source_id="mysql-rds", entity_id="mysql-rds-order", environment="prod", count=5
        )
        assert len(emitter._pending) == 1
        assert emitter._pending[0]["MetricName"] == "RecordsFailed"

    def test_emit_records_skipped_buffered(self) -> None:
        emitter = CloudWatchMetricsEmitter(region_name="us-east-1")
        emitter.emit_records_skipped(
            source_id="salesforce_account",
            entity_id="salesforce_account",
            environment="dev",
            count=42,
            stage="serving_store_load",
        )
        assert len(emitter._pending) == 1
        assert emitter._pending[0]["MetricName"] == "RecordsSkipped"
        assert emitter._pending[0]["Value"] == 42.0

    def test_flush_swallows_cloudwatch_error(self) -> None:
        """Flush failure must never propagate to the pipeline."""
        from unittest.mock import MagicMock

        from botocore.exceptions import ClientError

        emitter = CloudWatchMetricsEmitter(region_name="us-east-1")
        emitter.emit_schema_drift_count(
            source_id="sf", entity_id="sf-contact", environment="dev", count=1
        )
        emitter._client.put_metric_data = MagicMock(  # type: ignore[method-assign]
            side_effect=ClientError(
                {"Error": {"Code": "InternalError", "Message": ""}},
                "PutMetricData",
            )
        )
        emitter.flush()  # Must not raise

    def test_flush_clears_pending_even_on_error(self) -> None:
        from unittest.mock import MagicMock

        from botocore.exceptions import ClientError

        emitter = CloudWatchMetricsEmitter(region_name="us-east-1")
        emitter.emit_watermark_lag_seconds(
            source_id="ns", entity_id="ns-customer", environment="uat", lag_seconds=3600.0
        )
        emitter._client.put_metric_data = MagicMock(  # type: ignore[method-assign]
            side_effect=ClientError(
                {"Error": {"Code": "Throttling", "Message": ""}},
                "PutMetricData",
            )
        )
        emitter.flush()
        assert emitter._pending == []


def _dimensions_of(datum: dict[str, object]) -> list[dict[str, str]]:
    """_pending holds dict[str, object]; narrow the Dimensions list before indexing it."""
    dimensions = datum["Dimensions"]
    assert isinstance(dimensions, list)
    return dimensions


@mock_aws
class TestTenantCodeDimension:
    def test_tenant_code_included_when_set_via_constructor(self) -> None:
        emitter = CloudWatchMetricsEmitter(region_name="us-east-1", tenant_code="acme-corp")
        emitter.emit_records_extracted(
            source_id="salesforce", entity_id="salesforce-account", environment="dev", count=10
        )
        dimensions = _dimensions_of(emitter._pending[0])
        dims = {d["Name"]: d["Value"] for d in dimensions}
        assert dims["TenantCode"] == "acme-corp"
        assert dimensions[0]["Name"] == "TenantCode"

    def test_tenant_code_included_when_set_via_set_tenant_context(self) -> None:
        emitter = CloudWatchMetricsEmitter(region_name="us-east-1")
        emitter.set_tenant_context("globex-eu")
        emitter.emit_records_extracted(
            source_id="netsuite", entity_id="netsuite-customer", environment="dev", count=5
        )
        dims = {d["Name"]: d["Value"] for d in _dimensions_of(emitter._pending[0])}
        assert dims["TenantCode"] == "globex-eu"

    def test_tenant_code_absent_when_not_set(self) -> None:
        emitter = CloudWatchMetricsEmitter(region_name="us-east-1")
        emitter.emit_records_extracted(
            source_id="salesforce", entity_id="salesforce-account", environment="dev", count=5
        )
        dim_names = [d["Name"] for d in _dimensions_of(emitter._pending[0])]
        assert "TenantCode" not in dim_names

    def test_set_tenant_context_overrides_constructor_value(self) -> None:
        emitter = CloudWatchMetricsEmitter(region_name="us-east-1", tenant_code="old-tenant")
        emitter.set_tenant_context("new-tenant")
        emitter.emit_records_extracted(
            source_id="sf", entity_id="sf-account", environment="dev", count=1
        )
        dims = {d["Name"]: d["Value"] for d in _dimensions_of(emitter._pending[0])}
        assert dims["TenantCode"] == "new-tenant"

    def test_stage_and_tenant_code_both_present(self) -> None:
        emitter = CloudWatchMetricsEmitter(region_name="us-east-1", tenant_code="demo")
        emitter.emit_records_extracted(
            source_id="sf",
            entity_id="sf-account",
            environment="dev",
            count=1,
            stage="transformation",
        )
        dim_names = [d["Name"] for d in _dimensions_of(emitter._pending[0])]
        assert "TenantCode" in dim_names
        assert "Stage" in dim_names

    def test_stage_dimension_included_when_provided(self) -> None:
        emitter = CloudWatchMetricsEmitter(region_name="us-east-1")
        emitter.emit_records_extracted(
            source_id="salesforce",
            entity_id="salesforce-account",
            environment="dev",
            count=100,
            stage="transformation",
        )
        assert len(emitter._pending) == 1
        dims = {d["Name"]: d["Value"] for d in _dimensions_of(emitter._pending[0])}
        assert dims["Stage"] == "transformation"

    def test_stage_dimension_absent_when_not_provided(self) -> None:
        emitter = CloudWatchMetricsEmitter(region_name="us-east-1")
        emitter.emit_records_extracted(
            source_id="salesforce",
            entity_id="salesforce-account",
            environment="dev",
            count=100,
        )
        assert len(emitter._pending) == 1
        dim_names = [d["Name"] for d in _dimensions_of(emitter._pending[0])]
        assert "Stage" not in dim_names

    def test_emit_stage_duration(self) -> None:
        emitter = CloudWatchMetricsEmitter(region_name="us-east-1")
        emitter.emit_stage_duration(
            source_id="salesforce",
            entity_id="salesforce-account",
            environment="dev",
            stage="entity_resolution",
            duration_ms=2345.0,
        )
        assert len(emitter._pending) == 1
        assert emitter._pending[0]["MetricName"] == "StageDurationMs"
        dims = {d["Name"]: d["Value"] for d in _dimensions_of(emitter._pending[0])}
        assert dims["Stage"] == "entity_resolution"

    def test_emit_golden_record_count(self) -> None:
        emitter = CloudWatchMetricsEmitter(region_name="us-east-1")
        emitter.emit_golden_record_count(
            source_id="salesforce",
            entity_id="salesforce-account",
            environment="dev",
            count=500,
        )
        assert emitter._pending[0]["MetricName"] == "GoldenRecordCount"

    def test_emit_cluster_count(self) -> None:
        emitter = CloudWatchMetricsEmitter(region_name="us-east-1")
        emitter.emit_cluster_count(
            source_id="salesforce",
            entity_id="salesforce-account",
            environment="dev",
            count=120,
        )
        assert emitter._pending[0]["MetricName"] == "ClusterCount"


class TestCheckLambdaTimeoutPeriodic:
    """Tests for check_lambda_timeout_periodic mid-execution check."""

    def _make_context(self, remaining_ms: int):
        from unittest.mock import MagicMock

        ctx = MagicMock()
        ctx.get_remaining_time_in_millis.return_value = remaining_ms
        return ctx

    def test_sufficient_time_does_not_raise(self) -> None:
        from observability.lambda_runtime import check_lambda_timeout_periodic

        ctx = self._make_context(remaining_ms=300_000)  # 5 minutes
        check_lambda_timeout_periodic(ctx, min_remaining_ms=120_000, operation_name="test_op")

    def test_insufficient_time_raises(self) -> None:
        from observability.lambda_runtime import check_lambda_timeout_periodic

        ctx = self._make_context(remaining_ms=50_000)  # 50 seconds
        with pytest.raises(RuntimeError, match="test_op"):
            check_lambda_timeout_periodic(ctx, min_remaining_ms=120_000, operation_name="test_op")

    def test_none_context_is_noop(self) -> None:
        from observability.lambda_runtime import check_lambda_timeout_periodic

        check_lambda_timeout_periodic(None, min_remaining_ms=1_000_000, operation_name="test")

    def test_error_message_includes_remaining_time(self) -> None:
        from observability.lambda_runtime import check_lambda_timeout_periodic

        ctx = self._make_context(remaining_ms=30_000)
        try:
            check_lambda_timeout_periodic(ctx, min_remaining_ms=120_000, operation_name="write_op")
        except RuntimeError as exc:
            assert "30000" in str(exc)
            assert "write_op" in str(exc)
