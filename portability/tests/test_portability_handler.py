"""
End-to-end tests for the portability Lambda (DL-PORT-01, DL-PORT-04).

There was no test for this handler at all, which is why two defects lived in it through two audit
passes while `portability/tests/test_portability.py` stayed green: that module tests the
*libraries*, and both defects were in how the handler *called* them.

  - `_scope_predicate_from_event` returned `None` on an absent grant, one line below a docstring
    saying it raised. `_apply_scope` treated `None` as "no filter", so the least recoverable form of
    the isolation defect — an artefact leaving the platform with every scope unit's rows — was the
    default behaviour for a caller who sent no grant.
  - `_run_export` never called `execute`, so no artefact was ever produced. DL-PORT-01 returned a
    job id and nothing else.

Both are asserted here by driving `lambda_handler` and then reading what actually landed in S3 —
not by inspecting the handler's source, which is how the previous call-site gate was satisfied.
"""

from __future__ import annotations

import csv
import io
from typing import Any

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from moto import mock_aws

from conftest import RESOURCE_NAME_ENVIRONMENT
from persistence.tenant_tables import tenant_scoped_tables
from portability.export_service import EXPORT_CAPABILITY, ExportFormat, ExportLayer
from portability.portability_handler import lambda_handler
from tenancy.scope_contract import PartitionKind, PartitionModel, ScopeUnit, TenantPartitionProfile
from tenancy.scope_unit_repository import ScopeUnitRepository

_REGION = "us-east-1"
_TENANT = "evive"
_UNIT_A = "franchisee-0001"
_UNIT_B = "franchisee-0002"

_BUCKETS = {
    "ANALYTICS_S3_BUCKET": "datalake-analytics-test",
    "EXPORT_ARTEFACT_BUCKET": "datalake-exports-test",
    "RAW_S3_BUCKET": "datalake-raw-test",
    "CURATED_S3_BUCKET": "datalake-curated-test",
    "GOVERNANCE_S3_BUCKET": "datalake-governance-test",
    "SCHEMA_SNAPSHOTS_S3_BUCKET": "datalake-schemas-test",
}


def _dynamo_table(name: str, pk: str, sk: str | None = None) -> None:
    key_schema = [{"AttributeName": pk, "KeyType": "HASH"}]
    attributes = [{"AttributeName": pk, "AttributeType": "S"}]
    if sk:
        key_schema.append({"AttributeName": sk, "KeyType": "RANGE"})
        attributes.append({"AttributeName": sk, "AttributeType": "S"})
    boto3.client("dynamodb", region_name=_REGION).create_table(
        TableName=name,
        KeySchema=key_schema,
        AttributeDefinitions=attributes,
        BillingMode="PAY_PER_REQUEST",
    )


def _audit_table_with_tenant_index() -> None:
    boto3.client("dynamodb", region_name=_REGION).create_table(
        TableName=RESOURCE_NAME_ENVIRONMENT["AUDIT_LOG_TABLE"],
        KeySchema=[
            {"AttributeName": "run_id", "KeyType": "HASH"},
            {"AttributeName": "stage", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "run_id", "AttributeType": "S"},
            {"AttributeName": "stage", "AttributeType": "S"},
            {"AttributeName": "tenant_code", "AttributeType": "S"},
            {"AttributeName": "started_at", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "tenant-started-index",
                "KeySchema": [
                    {"AttributeName": "tenant_code", "KeyType": "HASH"},
                    {"AttributeName": "started_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture(autouse=True)
def _environment(monkeypatch: Any) -> None:
    monkeypatch.setenv("AWS_REGION", _REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("PLATFORM_ENVIRONMENT", "dev")
    for variable, bucket in _BUCKETS.items():
        monkeypatch.setenv(variable, bucket)


def _provision(*, partitioned: bool) -> None:
    s3 = boto3.client("s3", region_name=_REGION)
    for bucket in _BUCKETS.values():
        s3.create_bucket(Bucket=bucket)

    _dynamo_table(RESOURCE_NAME_ENVIRONMENT["EXPORT_JOB_TABLE"], "tenant_code", "job_id")
    _dynamo_table(RESOURCE_NAME_ENVIRONMENT["SCOPE_UNIT_TABLE"], "tenant_code", "scope_unit_id")

    repository = ScopeUnitRepository(environment="dev", region_name=_REGION)
    if partitioned:
        repository.save_partition_profile(
            TenantPartitionProfile(
                tenant_code=_TENANT,
                partition_model=PartitionModel.PARTITIONED,
                partition_kind=PartitionKind.FRANCHISE,
            )
        )
        for unit_id in (_UNIT_A, _UNIT_B):
            repository.save_scope_unit(
                ScopeUnit(
                    tenant_code=_TENANT,
                    scope_unit_id=unit_id,
                    partition_kind=PartitionKind.FRANCHISE,
                    display_name=unit_id,
                )
            )

    table = pa.table(
        {
            "golden_id": ["c-1", "c-2"],
            "name": ["Acme", "Beta"],
            "scope_unit_id": [_UNIT_A, _UNIT_B],
        }
    )
    buffer = io.BytesIO()
    pq.write_table(table, buffer)
    s3.put_object(
        Bucket=_BUCKETS["ANALYTICS_S3_BUCKET"],
        Key=f"{_TENANT}/analytics/company/analytics_date=2026-07-29/data.parquet",
        Body=buffer.getvalue(),
    )


def _export_event(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "action": "export",
        "tenant_code": _TENANT,
        "entity_id": "company",
        "layer": ExportLayer.ANALYTICS.value,
        "export_format": ExportFormat.CSV.value,
        "requested_by": "ops@example.test",
        "granted_capabilities": [EXPORT_CAPABILITY],
    }
    return {**base, **overrides}


def _artefact_rows(job_id: str) -> list[dict[str, str]]:
    body = (
        boto3.client("s3", region_name=_REGION)
        .get_object(
            Bucket=_BUCKETS["EXPORT_ARTEFACT_BUCKET"], Key=f"{_TENANT}/exports/{job_id}/company.csv"
        )["Body"]
        .read()
        .decode()
    )
    return list(csv.DictReader(io.StringIO(body)))


@mock_aws
class TestExportProducesAnArtefact:
    def test_the_artefact_is_written_not_merely_requested(self) -> None:
        _provision(partitioned=False)
        result = lambda_handler(_export_event(), None)
        assert result["status"] == "completed"
        assert result["row_count"] == 2
        assert result["artefact_bytes"] > 0
        assert len(_artefact_rows(result["job_id"])) == 2

    def test_an_entity_with_no_published_partition_exports_zero_rows(self) -> None:
        _provision(partitioned=False)
        result = lambda_handler(_export_event(entity_id="never_published"), None)
        assert result["row_count"] == 0


@mock_aws
class TestExportScopeIsolation:
    def test_a_franchisee_export_contains_only_its_own_rows(self) -> None:
        _provision(partitioned=True)
        result = lambda_handler(_export_event(granted_scope_units=[_UNIT_A]), None)
        rows = _artefact_rows(result["job_id"])
        assert [row["scope_unit_id"] for row in rows] == [_UNIT_A]

    def test_the_sibling_franchisee_sees_the_other_row_and_only_that(self) -> None:
        _provision(partitioned=True)
        result = lambda_handler(_export_event(granted_scope_units=[_UNIT_B]), None)
        assert [row["scope_unit_id"] for row in _artefact_rows(result["job_id"])] == [_UNIT_B]

    def test_an_absent_grant_denies_rather_than_exporting_everything(self) -> None:
        """
        The F3 assertion. This returned `None`, which meant "no filter", so the caller who supplied
        no grant received both franchisees' rows in one downloadable file.
        """
        _provision(partitioned=True)
        with pytest.raises(PermissionError, match="denies all access"):
            lambda_handler(_export_event(), None)
        assert "Contents" not in boto3.client("s3", region_name=_REGION).list_objects_v2(
            Bucket=_BUCKETS["EXPORT_ARTEFACT_BUCKET"]
        )

    def test_a_grant_naming_a_unit_the_tenant_does_not_own_is_rejected(self) -> None:
        _provision(partitioned=True)
        with pytest.raises(PermissionError, match="do not exist"):
            lambda_handler(_export_event(granted_scope_units=["franchisee-9999"]), None)

    def test_an_affirmative_tenant_wide_grant_exports_both(self) -> None:
        _provision(partitioned=True)
        result = lambda_handler(_export_event(granted_scope_tenant_wide=True), None)
        assert len(_artefact_rows(result["job_id"])) == 2


@mock_aws
class TestExportCapability:
    def test_export_requires_its_own_capability(self) -> None:
        _provision(partitioned=False)
        with pytest.raises(Exception, match="distinct from"):
            lambda_handler(_export_event(granted_capabilities=["datalake:read"]), None)


@mock_aws
class TestDeletionCoversEveryStore:
    def test_a_certified_deletion_covers_all_eleven_stores(self) -> None:
        """
        Only four stores had deleters, and the saga refuses to certify a deletion it did not fully
        cover — so a correctly authorised deletion always raised. Failing loudly was right; the
        missing deleters were the defect.
        """
        _provision(partitioned=False)
        _dynamo_table(
            RESOURCE_NAME_ENVIRONMENT["DELETION_CERTIFICATE_TABLE"], "tenant_code", "certificate_id"
        )
        for table_name in tenant_scoped_tables():
            if table_name in {
                RESOURCE_NAME_ENVIRONMENT["EXPORT_JOB_TABLE"],
                RESOURCE_NAME_ENVIRONMENT["SCOPE_UNIT_TABLE"],
            }:
                continue
            if table_name == RESOURCE_NAME_ENVIRONMENT["AUDIT_LOG_TABLE"]:
                _audit_table_with_tenant_index()
            elif table_name in {
                RESOURCE_NAME_ENVIRONMENT["ENTITY_CONFIG_TABLE"],
                RESOURCE_NAME_ENVIRONMENT["WATERMARK_TABLE"],
            }:
                _dynamo_table(table_name, "source_id", "entity_id")
            else:
                _dynamo_table(table_name, "tenant_code", "sk")

        result = lambda_handler(
            {
                "action": "delete",
                "tenant_code": _TENANT,
                "requested_by": "ops@example.test",
                "approved_by": "ciso@example.test",
                "typed_confirmation": f"DELETE ALL DATA FOR {_TENANT}",
                "scope_description": "all customer data",
            },
            None,
        )
        assert result["certificate_id"].startswith("dcert-")
        assert result["stores_deleted"] == 11


class TestEventValidation:
    def test_an_unknown_action_is_refused_before_any_data_is_touched(self) -> None:
        with pytest.raises(ValueError, match="action must be one of"):
            lambda_handler({"action": "truncate", "tenant_code": _TENANT}, None)

    def test_a_request_without_a_tenant_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must name its tenant_code"):
            lambda_handler({"action": "export"}, None)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
