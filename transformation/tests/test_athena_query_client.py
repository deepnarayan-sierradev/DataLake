"""
Tests for transformation/athena_query_client.py.

Uses moto's Athena mock (limited — moto auto-succeeds queries).
"""

from __future__ import annotations

import pytest
from moto import mock_aws

from transformation.athena_query_client import (
    AthenaQueryClient,
    AthenaQueryError,
    AthenaQueryResult,
)

WORKGROUP = "dev-edl-analytics"
REGION = "us-east-1"
DATABASE = "dev_edl_analytics"


@pytest.fixture()
def athena_client():
    """Return an AthenaQueryClient against the moto Athena mock."""
    with mock_aws():
        import boto3

        # Create the workgroup so queries can reference it
        boto3.client("athena", region_name=REGION).create_work_group(
            Name=WORKGROUP,
            Configuration={
                "EnforceWorkGroupConfiguration": True,
                "ResultConfiguration": {
                    "OutputLocation": "s3://test-athena-results/",
                },
            },
        )
        yield AthenaQueryClient(
            workgroup=WORKGROUP,
            region_name=REGION,
            poll_interval_seconds=0.01,  # fast for tests
            timeout_seconds=10,
        )


class TestExecuteQuery:
    def test_execute_query_returns_result(self, athena_client: AthenaQueryClient) -> None:
        result = athena_client.execute_query(
            query="SELECT 1",
            database=DATABASE,
        )
        assert isinstance(result, AthenaQueryResult)
        assert result.workgroup == WORKGROUP
        assert result.database == DATABASE
        assert result.state == "SUCCEEDED"
        assert result.query_execution_id != ""

    def test_execute_query_invalid_database_raises_error(
        self, athena_client: AthenaQueryClient
    ) -> None:
        with pytest.raises(AthenaQueryError, match="Invalid database name"):
            athena_client.execute_query(query="SELECT 1", database="INVALID NAME!")

    def test_execute_query_timestamps_populated(self, athena_client: AthenaQueryClient) -> None:
        result = athena_client.execute_query(query="SELECT 1", database=DATABASE)
        assert "T" in result.submitted_at
        assert "T" in result.completed_at


class TestGetQueryResults:
    def test_get_query_results_succeeded_execution(self, athena_client: AthenaQueryClient) -> None:
        result = athena_client.execute_query(query="SELECT 1", database=DATABASE)
        rows = athena_client.get_query_results(result.query_execution_id)
        # moto returns empty result set for synthetic queries
        assert isinstance(rows, list)

    def test_get_query_results_unknown_execution_raises_error(
        self, athena_client: AthenaQueryClient
    ) -> None:
        # moto may raise ClientError for unknown execution IDs
        with pytest.raises(AthenaQueryError):
            athena_client.get_query_results("nonexistent-execution-id")


class TestAthenaErrorPaths:
    """Cover FAILED, CANCELLED, timeout, and get_results ClientError paths."""

    def _make_client(self, poll: float = 0.001, timeout: float = 5.0) -> AthenaQueryClient:
        return AthenaQueryClient(
            workgroup=WORKGROUP,
            region_name=REGION,
            poll_interval_seconds=poll,
            timeout_seconds=timeout,
        )

    @pytest.fixture(autouse=True)
    def _aws_mock(self):  # type: ignore[no-untyped-def]
        with mock_aws():
            import boto3
            boto3.client("athena", region_name=REGION).create_work_group(
                Name=WORKGROUP,
                Configuration={
                    "ResultConfiguration": {"OutputLocation": "s3://test-results/"},
                },
            )
            yield

    def test_failed_state_raises_athena_query_error(self) -> None:
        from unittest.mock import MagicMock, patch

        client = self._make_client()
        # Patch _get_execution_status to return FAILED after the first call to start
        mock_status = MagicMock()
        mock_status.return_value = {
            "QueryExecution": {
                "Status": {"State": "FAILED", "StateChangeReason": "syntax error"},
                "Statistics": {},
                "ResultConfiguration": {},
            }
        }
        with patch.object(client, "_get_execution_status", mock_status):
            with patch.object(
                client._client,  # type: ignore[attr-defined]
                "start_query_execution",
                return_value={"QueryExecutionId": "exec-failed-001"},
            ):
                with pytest.raises(AthenaQueryError, match="FAILED"):
                    client.execute_query(query="SELECT bad", database=DATABASE)

    def test_cancelled_state_raises_athena_query_error(self) -> None:
        from unittest.mock import MagicMock, patch

        client = self._make_client()
        mock_status = MagicMock()
        mock_status.return_value = {
            "QueryExecution": {
                "Status": {"State": "CANCELLED", "StateChangeReason": "user cancelled"},
                "Statistics": {},
                "ResultConfiguration": {},
            }
        }
        with patch.object(client, "_get_execution_status", mock_status):
            with patch.object(
                client._client,  # type: ignore[attr-defined]
                "start_query_execution",
                return_value={"QueryExecutionId": "exec-cancel-001"},
            ):
                with pytest.raises(AthenaQueryError, match="CANCELLED"):
                    client.execute_query(query="SELECT 1", database=DATABASE)

    def test_timeout_triggers_cancel_and_raises(self) -> None:
        from unittest.mock import MagicMock, patch

        client = self._make_client(timeout=0.0)  # Immediate timeout
        running_status = MagicMock()
        running_status.return_value = {
            "QueryExecution": {
                "Status": {"State": "RUNNING"},
                "Statistics": {},
                "ResultConfiguration": {},
            }
        }
        with patch.object(client, "_get_execution_status", running_status):
            with patch.object(
                client._client,  # type: ignore[attr-defined]
                "start_query_execution",
                return_value={"QueryExecutionId": "exec-timeout-001"},
            ):
                with patch.object(client, "_cancel_query") as mock_cancel:
                    with pytest.raises(AthenaQueryError, match="timed out"):
                        client.execute_query(query="SELECT 1", database=DATABASE)
                    mock_cancel.assert_called_once_with("exec-timeout-001")

    def test_get_query_results_non_succeeded_state_raises(self) -> None:
        from unittest.mock import MagicMock, patch

        client = self._make_client()
        mock_status = MagicMock()
        mock_status.return_value = {
            "QueryExecution": {
                "Status": {"State": "FAILED"},
                "Statistics": {},
                "ResultConfiguration": {},
            }
        }
        with patch.object(client, "_get_execution_status", mock_status):
            with pytest.raises(AthenaQueryError, match="Cannot fetch results"):
                client.get_query_results("exec-non-succeeded")

    def test_get_query_results_client_error_raises(self) -> None:
        from unittest.mock import MagicMock, patch

        from botocore.exceptions import ClientError

        client = self._make_client()
        mock_status = MagicMock()
        mock_status.return_value = {
            "QueryExecution": {
                "Status": {"State": "SUCCEEDED"},
                "Statistics": {},
                "ResultConfiguration": {},
            }
        }
        with patch.object(client, "_get_execution_status", mock_status):
            with patch.object(
                client._client,  # type: ignore[attr-defined]
                "get_query_results",
                side_effect=ClientError(
                    {"Error": {"Code": "InvalidRequestException", "Message": ""}},
                    "GetQueryResults",
                ),
            ):
                with pytest.raises(AthenaQueryError, match="Failed to fetch results"):
                    client.get_query_results("exec-clienterror-001")


class TestDatabaseValidation:
    @pytest.mark.parametrize(
        "database",
        [
            "dev_edl_analytics",
            "staging_edl_curated",
            "prod_edl_analytics",
            "a1b2c3",
        ],
    )
    def test_valid_database_names_accepted(
        self, athena_client: AthenaQueryClient, database: str
    ) -> None:
        # Should not raise; will proceed to Athena (or fail on workgroup for different workgroup)
        result = athena_client.execute_query(query="SELECT 1", database=database)
        assert result.state == "SUCCEEDED"

    @pytest.mark.parametrize(
        "database",
        [
            "UPPER_CASE",
            "has space",
            "",
            "123startsnumber",
            "a" * 130,  # too long
            "hyphen-name",
        ],
    )
    def test_invalid_database_names_rejected(
        self, athena_client: AthenaQueryClient, database: str
    ) -> None:
        with pytest.raises(AthenaQueryError, match="Invalid database name"):
            athena_client.execute_query(query="SELECT 1", database=database)
