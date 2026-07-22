"""Tests for RedshiftLoader (Amazon Redshift Serverless engine, S3 COPY path)."""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from moto import mock_aws

from serving_store.loaders.redshift_loader import (
    RedshiftLoader,
    ServingStoreError,
    _arrow_to_redshift_type,
    _infer_redshift_type,
)

_REGION = "us-east-1"
_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:redshift-conn"
_ANALYTICS_BUCKET = "edl-analytics-dev"
_ANALYTICS_PREFIX = "acme-corp/company/analytics_date=2026-07-22"
_TABLE_NAME = "salesforce_account"
_TENANT_CODE = "acme-corp"
_COPY_ROLE = "arn:aws:iam::123456789012:role/edl-serving-store-redshift-dev-copy-role"


def _connection_secret() -> str:
    return json.dumps(
        {
            "host": "redshift-dev.123.us-east-1.redshift-serverless.amazonaws.com",
            "port": "5439",
            "workgroup": "edl-serving-store-redshift-dev",
            "database": "edl_serving",
            "region": _REGION,
            "copy_iam_role": _COPY_ROLE,
        }
    )


def _put_parquet(bucket: str, key: str) -> None:
    table = pa.table(
        {
            "account_id": pa.array(["001", "002"], pa.string()),
            "name": pa.array(["Acme", "Beta"], pa.string()),
            "revenue": pa.array([1_000_000, 500_000], pa.int64()),
            "active": pa.array([True, False], pa.bool_()),
            "ratio": pa.array([1.5, 2.0], pa.float64()),
        }
    )
    buf = io.BytesIO()
    pq.write_table(table, buf)
    boto3.client("s3", region_name=_REGION).put_object(Bucket=bucket, Key=key, Body=buf.getvalue())


def _make_connection(
    total: int = 2, loaded: int = 2, user_exists: bool = False
) -> tuple[MagicMock, MagicMock]:
    """Cursor whose fetchone yields the pg_user check then the two COUNT results."""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.fetchone.side_effect = [
        (1,) if user_exists else None,  # pg_user existence check
        (total,),  # SELECT COUNT(*) FROM staging
        (loaded,),  # SELECT COUNT(*) new-or-changed
    ]
    conn.cursor.return_value = cursor
    return conn, cursor


@mock_aws
class TestRedshiftLoaderS3Path:
    def setup_method(self, method=None):
        s3 = boto3.client("s3", region_name=_REGION)
        s3.create_bucket(Bucket=_ANALYTICS_BUCKET)
        _put_parquet(_ANALYTICS_BUCKET, f"{_ANALYTICS_PREFIX}/part-0.parquet")
        sm = boto3.client("secretsmanager", region_name=_REGION)
        sm.create_secret(Name=_SECRET_ARN, SecretString=_connection_secret())

    def _run(self, conn):
        loader = RedshiftLoader(_SECRET_ARN, _REGION)
        with patch(
            "serving_store.loaders.redshift_loader.redshift_connector.connect",
            return_value=conn,
        ):
            return loader.load_from_s3(
                _ANALYTICS_BUCKET,
                _ANALYTICS_PREFIX,
                _TABLE_NAME,
                ("account_id",),
                _TENANT_CODE,
                run_id="run-123",
            )

    def _executed(self, cursor: MagicMock) -> str:
        return "\n".join(str(call.args[0]) for call in cursor.execute.call_args_list)

    def test_load_counts_new_and_changed(self):
        conn, _ = _make_connection(total=5, loaded=3)
        result = self._run(conn)
        assert result.records_loaded == 3
        assert result.records_skipped == 2
        assert result.database_name == "acme_corp"
        assert result.table_name == _TABLE_NAME

    def test_schema_per_tenant_isolation(self):
        conn, cursor = _make_connection()
        self._run(conn)
        sql = self._executed(cursor)
        assert 'CREATE SCHEMA IF NOT EXISTS "acme_corp"' in sql
        assert 'SET search_path TO "acme_corp"' in sql
        # Reader is granted only on this tenant's schema, never a cluster-wide role.
        assert 'GRANT SELECT ON ALL TABLES IN SCHEMA "acme_corp"' in sql
        assert "db_datareader" not in sql

    def test_reader_user_created_with_md5_verifier(self):
        conn, cursor = _make_connection(user_exists=False)
        self._run(conn)
        sql = self._executed(cursor)
        assert "CREATE USER" in sql
        assert "PASSWORD 'md5" in sql  # md5 verifier, raw token never inlined

    def test_copy_uses_iam_role_and_parquet_format(self):
        conn, cursor = _make_connection()
        self._run(conn)
        copy_calls = [c for c in cursor.execute.call_args_list if "COPY" in str(c.args[0])]
        assert len(copy_calls) == 1
        sql, params = copy_calls[0].args[0], copy_calls[0].args[1]
        assert "FORMAT AS PARQUET" in sql
        assert params[1] == _COPY_ROLE  # IAM_ROLE bound as a parameter

    def test_merge_issued_when_rows_changed(self):
        conn, cursor = _make_connection(total=2, loaded=2)
        self._run(conn)
        sql = self._executed(cursor)
        assert "MERGE INTO" in sql
        assert "WHEN MATCHED THEN UPDATE SET" in sql
        assert "WHEN NOT MATCHED THEN INSERT" in sql

    def test_no_merge_when_nothing_changed(self):
        conn, cursor = _make_connection(total=4, loaded=0)
        result = self._run(conn)
        assert result.records_loaded == 0
        assert result.records_skipped == 4
        assert "MERGE INTO" not in self._executed(cursor)

    def test_missing_copy_role_raises(self):
        sm = boto3.client("secretsmanager", region_name=_REGION)
        sm.put_secret_value(
            SecretId=_SECRET_ARN, SecretString=json.dumps({"host": "h", "port": "5439"})
        )
        conn, _ = _make_connection()
        with pytest.raises(ServingStoreError, match="copy_iam_role"):
            self._run(conn)

    def test_no_parquet_objects_raises(self):
        loader = RedshiftLoader(_SECRET_ARN, _REGION)
        conn, _ = _make_connection()
        with (
            patch(
                "serving_store.loaders.redshift_loader.redshift_connector.connect",
                return_value=conn,
            ),
            pytest.raises(ServingStoreError, match="No Parquet objects"),
        ):
            loader.load_from_s3(
                _ANALYTICS_BUCKET, "acme-corp/empty", _TABLE_NAME, ("account_id",), _TENANT_CODE
            )

    def test_invalid_table_name_raises(self):
        loader = RedshiftLoader(_SECRET_ARN, _REGION)
        with pytest.raises(ValueError, match="Invalid table name"):
            loader.load_from_s3(
                _ANALYTICS_BUCKET, _ANALYTICS_PREFIX, "bad name", ("account_id",), _TENANT_CODE
            )


class TestRedshiftRowPathUnsupported:
    def test_read_existing_hashes_raises(self):
        loader = RedshiftLoader(_SECRET_ARN, _REGION)
        with pytest.raises(ServingStoreError, match="row-batch path is not supported"):
            loader._read_existing_hashes(MagicMock(), "acme_corp", _TABLE_NAME, ("id",), [("1",)])

    def test_bulk_upsert_raises(self):
        loader = RedshiftLoader(_SECRET_ARN, _REGION)
        with pytest.raises(ServingStoreError, match="row-batch path is not supported"):
            loader._bulk_upsert(MagicMock(), "acme_corp", _TABLE_NAME, ["id"], ("id",), [])


class TestRedshiftTypeInference:
    def test_arrow_types_never_use_text(self):
        assert _arrow_to_redshift_type(pa.string()) == "VARCHAR(65535)"
        assert _arrow_to_redshift_type(pa.large_string()) == "VARCHAR(65535)"
        assert _arrow_to_redshift_type(pa.int64()) == "BIGINT"
        assert _arrow_to_redshift_type(pa.float64()) == "DOUBLE PRECISION"
        assert _arrow_to_redshift_type(pa.bool_()) == "BOOLEAN"
        assert _arrow_to_redshift_type(pa.timestamp("us")) == "TIMESTAMP"
        assert _arrow_to_redshift_type(pa.date32()) == "DATE"
        assert _arrow_to_redshift_type(pa.list_(pa.int64())) == "SUPER"
        assert _arrow_to_redshift_type(pa.decimal128(38, 6)) == "DECIMAL(38,6)"

    def test_sample_value_inference_never_uses_text(self):
        assert _infer_redshift_type("x") == "VARCHAR(65535)"
        assert _infer_redshift_type(None) == "VARCHAR(65535)"
        assert _infer_redshift_type(True) == "BOOLEAN"
        assert _infer_redshift_type(5) == "BIGINT"
        assert _infer_redshift_type(1.2) == "DOUBLE PRECISION"
        assert _infer_redshift_type({"k": "v"}) == "SUPER"
