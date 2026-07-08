"""
Tests for curated_utils shared utility functions.

Coverage:
  - find_latest_curated_prefix: returns None when no partitions exist
  - find_latest_curated_prefix: returns latest by ISO date then run_id
  - find_latest_curated_prefix: handles multiple date partitions (latest wins)
  - load_curated_records: path traversal rejected (OWASP A03)
  - load_curated_records: disallowed characters rejected
  - load_curated_records: loads all Parquet files under prefix
  - load_curated_records_duckdb: path traversal / disallowed characters rejected
  - load_curated_records_duckdb: reads via DuckDB read_parquet() when available
  - load_curated_records_duckdb: falls back to load_curated_records() when
    DuckDB is unavailable or the httpfs S3 read fails (PERF-3)
  - source_id_to_domain: hyphens converted to underscores
  - SAFE_S3_PREFIX_PATTERN: valid prefix matches; traversal patterns rejected
"""

from __future__ import annotations

import io
import sys
from typing import Any
from unittest.mock import MagicMock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from transformation.curated_utils import (
    SAFE_S3_PREFIX_PATTERN,
    find_latest_curated_prefix,
    load_curated_records,
    load_curated_records_duckdb,
    source_id_to_domain,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parquet_bytes(records: list[dict[str, Any]]) -> bytes:
    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(records), buf)
    return buf.getvalue()


def _mock_s3_empty() -> MagicMock:
    """S3 mock with no objects."""
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = iter(
        [{"CommonPrefixes": [], "Contents": []}]
    )
    return s3


def _mock_s3_with_prefix(
    domain: str,
    entity_id: str,
    date: str,
    run_id: str,
    records: list[dict[str, Any]],
) -> MagicMock:
    """S3 mock serving one date/run partition with given records."""
    s3 = MagicMock()
    parquet = _parquet_bytes(records)

    def _paginate(Bucket, Prefix, **kwargs):  # noqa: N803 -- kwarg names mirror the real boto3 S3 API
        delimiter = kwargs.get("Delimiter")
        if delimiter == "/" and "curated_date=" not in Prefix:
            date_prefix = f"curated/{domain}/{entity_id}/curated_date={date}/"
            return iter([{"CommonPrefixes": [{"Prefix": date_prefix}], "Contents": []}])
        elif delimiter == "/" and "curated_date=" in Prefix:
            run_prefix = f"curated/{domain}/{entity_id}/curated_date={date}/run_id={run_id}/"
            return iter([{"CommonPrefixes": [{"Prefix": run_prefix}], "Contents": []}])
        else:
            data_key = (
                f"curated/{domain}/{entity_id}/curated_date={date}/run_id={run_id}/data.parquet"
            )
            return iter([{"Contents": [{"Key": data_key}], "CommonPrefixes": []}])

    s3.get_paginator.return_value.paginate.side_effect = _paginate
    s3.get_object.return_value = {"Body": MagicMock(read=lambda: parquet)}
    return s3


# ---------------------------------------------------------------------------
# source_id_to_domain
# ---------------------------------------------------------------------------


class TestSourceIdToDomain:
    def test_hyphen_converted_to_underscore(self):
        assert source_id_to_domain("mysql-rds") == "mysql_rds"

    def test_no_hyphen_unchanged(self):
        assert source_id_to_domain("salesforce") == "salesforce"

    def test_multiple_hyphens(self):
        assert source_id_to_domain("some-multi-part") == "some_multi_part"


# ---------------------------------------------------------------------------
# SAFE_S3_PREFIX_PATTERN
# ---------------------------------------------------------------------------


class TestSafePrefixPattern:
    def test_valid_curated_prefix_matches(self):
        assert SAFE_S3_PREFIX_PATTERN.match(
            "curated/salesforce/salesforce-contact/curated_date=2026-07-02/run_id=run-001"
        )

    def test_hive_equals_sign_allowed(self):
        assert SAFE_S3_PREFIX_PATTERN.match("curated_date=2026-07-02")

    def test_path_traversal_dotdot_rejected(self):
        assert not SAFE_S3_PREFIX_PATTERN.match("../etc/passwd")

    def test_leading_slash_rejected(self):
        assert not SAFE_S3_PREFIX_PATTERN.match("/etc/passwd")


# ---------------------------------------------------------------------------
# find_latest_curated_prefix
# ---------------------------------------------------------------------------


class TestFindLatestCuratedPrefix:
    def test_returns_none_when_no_partitions(self):
        s3 = _mock_s3_empty()
        result = find_latest_curated_prefix(
            s3, "bucket", "salesforce", "salesforce-contact", "demo"
        )
        assert result is None

    def test_returns_prefix_with_trailing_slash(self):
        s3 = _mock_s3_with_prefix("salesforce", "salesforce-contact", "2026-07-02", "run-001", [])
        result = find_latest_curated_prefix(
            s3, "bucket", "salesforce", "salesforce-contact", "demo"
        )
        assert result is not None
        assert result.endswith("/")

    def test_returns_latest_date_partition(self):
        """When multiple dates exist, the latest ISO date wins."""
        s3 = MagicMock()

        def _paginate(Bucket, Prefix, **kwargs):  # noqa: N803 -- kwarg names mirror the real boto3 S3 API
            delimiter = kwargs.get("Delimiter")
            if delimiter == "/" and "curated_date=" not in Prefix:
                return iter(
                    [
                        {
                            "CommonPrefixes": [
                                {"Prefix": "curated/sf/sf-contact/curated_date=2026-06-01/"},
                                {"Prefix": "curated/sf/sf-contact/curated_date=2026-07-02/"},
                                {"Prefix": "curated/sf/sf-contact/curated_date=2026-06-30/"},
                            ],
                            "Contents": [],
                        }
                    ]
                )
            elif delimiter == "/" and "curated_date=2026-07-02" in Prefix:
                run_prefix = "curated/sf/sf-contact/curated_date=2026-07-02/run_id=run-002/"
                return iter([{"CommonPrefixes": [{"Prefix": run_prefix}], "Contents": []}])
            return iter([{"CommonPrefixes": [], "Contents": []}])

        s3.get_paginator.return_value.paginate.side_effect = _paginate
        result = find_latest_curated_prefix(s3, "bucket", "sf", "sf-contact", "demo")
        assert result == "curated/sf/sf-contact/curated_date=2026-07-02/run_id=run-002/"

    def test_returns_none_when_no_run_id_subfolders(self):
        """Date partition exists but has no run_id sub-prefixes."""
        s3 = MagicMock()

        def _paginate(Bucket, Prefix, **kwargs):  # noqa: N803 -- kwarg names mirror the real boto3 S3 API
            delimiter = kwargs.get("Delimiter")
            if delimiter == "/" and "curated_date=" not in Prefix:
                return iter(
                    [
                        {
                            "CommonPrefixes": [
                                {"Prefix": "curated/sf/sf-c/curated_date=2026-07-02/"}
                            ],
                            "Contents": [],
                        }
                    ]
                )
            return iter([{"CommonPrefixes": [], "Contents": []}])

        s3.get_paginator.return_value.paginate.side_effect = _paginate
        result = find_latest_curated_prefix(s3, "bucket", "sf", "sf-c", "demo")
        assert result is None


# ---------------------------------------------------------------------------
# load_curated_records
# ---------------------------------------------------------------------------


class TestLoadCuratedRecords:
    def test_path_traversal_rejected(self):
        with pytest.raises(ValueError, match="Unsafe"):
            load_curated_records(MagicMock(), "bucket", "../etc/passwd")

    def test_leading_slash_rejected(self):
        with pytest.raises(ValueError, match="Unsafe"):
            load_curated_records(MagicMock(), "bucket", "/absolute/path")

    def test_disallowed_characters_rejected(self):
        with pytest.raises(ValueError, match="disallowed"):
            load_curated_records(MagicMock(), "bucket", "valid/path;rm -rf /")

    def test_loads_records_from_parquet(self):
        records = [{"Id": "1", "Name": "Alice"}, {"Id": "2", "Name": "Bob"}]
        s3 = _mock_s3_with_prefix("sf", "sf-c", "2026-07-02", "run-001", records)
        prefix = "curated/sf/sf-c/curated_date=2026-07-02/run_id=run-001/"

        result = load_curated_records(s3, "bucket", prefix)

        assert len(result) == 2
        names = {r["Name"] for r in result}
        assert names == {"Alice", "Bob"}

    def test_empty_partition_returns_empty_list(self):
        s3 = MagicMock()
        s3.get_paginator.return_value.paginate.return_value = iter(
            [{"Contents": [], "CommonPrefixes": []}]
        )
        result = load_curated_records(s3, "bucket", "curated/sf/sf-c/curated_date=2026-07-02/")
        assert result == []


# ---------------------------------------------------------------------------
# load_curated_records_duckdb (PERF-3)
# ---------------------------------------------------------------------------


class TestLoadCuratedRecordsDuckdb:
    """
    load_curated_records_duckdb() reads curated Parquet directly from S3 via
    DuckDB's read_parquet(), instead of load_curated_records()'s hand-rolled
    list -> download -> BytesIO -> pq.read_table() loop. It falls back to
    load_curated_records() when DuckDB is unavailable or the read fails —
    exercised here by swapping sys.modules['duckdb'] rather than depending on
    real network/S3 access, so these tests are deterministic and offline.
    """

    def test_path_traversal_rejected(self):
        with pytest.raises(ValueError, match="Unsafe"):
            load_curated_records_duckdb(MagicMock(), "bucket", "../etc/passwd", "us-east-1")

    def test_leading_slash_rejected(self):
        with pytest.raises(ValueError, match="Unsafe"):
            load_curated_records_duckdb(MagicMock(), "bucket", "/absolute/path", "us-east-1")

    def test_disallowed_characters_rejected(self):
        with pytest.raises(ValueError, match="disallowed"):
            load_curated_records_duckdb(MagicMock(), "bucket", "valid/path;rm -rf /", "us-east-1")

    def test_duckdb_unavailable_falls_back_to_python_load(self, monkeypatch):
        """A None entry in sys.modules makes `import duckdb` raise ImportError."""
        records = [{"Id": "1", "Name": "Alice"}]
        s3 = _mock_s3_with_prefix("sf", "sf-c", "2026-07-02", "run-1", records)
        monkeypatch.setitem(sys.modules, "duckdb", None)

        prefix = "curated/sf/sf-c/curated_date=2026-07-02/run_id=run-1/"
        result = load_curated_records_duckdb(s3, "bucket", prefix, "us-east-1")

        assert len(result) == 1
        assert result[0]["Name"] == "Alice"

    def test_duckdb_execute_failure_falls_back_to_python_load(self, monkeypatch):
        """A DuckDB/httpfs execution failure degrades to the Python loader."""
        records = [{"Id": "1", "Name": "Alice"}]
        s3 = _mock_s3_with_prefix("sf", "sf-c", "2026-07-02", "run-1", records)

        mock_con = MagicMock()
        mock_con.execute.side_effect = Exception("httpfs unreachable in test environment")
        mock_duckdb = MagicMock()
        mock_duckdb.connect.return_value = mock_con
        monkeypatch.setitem(sys.modules, "duckdb", mock_duckdb)

        prefix = "curated/sf/sf-c/curated_date=2026-07-02/run_id=run-1/"
        result = load_curated_records_duckdb(s3, "bucket", prefix, "us-east-1")

        assert len(result) == 1
        assert result[0]["Name"] == "Alice"
        mock_con.close.assert_called_once()

    def test_duckdb_success_path_reads_via_read_parquet(self, monkeypatch):
        """When DuckDB read_parquet succeeds, its Arrow result is used directly
        — load_curated_records() (the Python S3 list+download loop) must NOT
        be invoked at all."""
        expected = [{"Id": "1", "Name": "Alice"}, {"Id": "2", "Name": "Bob"}]
        table = pa.Table.from_pylist(expected)

        mock_con = MagicMock()
        mock_con.execute.return_value.arrow.return_value = table
        mock_duckdb = MagicMock()
        mock_duckdb.connect.return_value = mock_con
        monkeypatch.setitem(sys.modules, "duckdb", mock_duckdb)

        # s3 client would raise if load_curated_records() were reached.
        s3 = MagicMock()
        s3.get_paginator.side_effect = AssertionError(
            "Python S3 fallback must not run on the DuckDB success path"
        )

        prefix = "curated/sf/sf-c/curated_date=2026-07-02/run_id=run-1/"
        result = load_curated_records_duckdb(s3, "bucket", prefix, "us-east-1")

        assert len(result) == 2
        assert {r["Name"] for r in result} == {"Alice", "Bob"}
        mock_con.execute.assert_any_call("INSTALL httpfs; LOAD httpfs;")
        mock_con.close.assert_called_once()

    def test_duckdb_connection_closed_on_success(self, monkeypatch):
        table = pa.Table.from_pylist([{"Id": "1"}])
        mock_con = MagicMock()
        mock_con.execute.return_value.arrow.return_value = table
        mock_duckdb = MagicMock()
        mock_duckdb.connect.return_value = mock_con
        monkeypatch.setitem(sys.modules, "duckdb", mock_duckdb)

        prefix = "curated/sf/sf-c/curated_date=2026-07-02/run_id=run-1/"
        load_curated_records_duckdb(MagicMock(), "bucket", prefix, "us-east-1")

        mock_con.close.assert_called_once()


class TestFindLatestCuratedPrefixSecurity:
    """Security tests for S3 prefix validation in find_latest_curated_prefix."""

    def _make_s3_mock_with_prefix(self, date_prefix, run_prefix):
        """Mock S3 paginator returning custom prefixes."""
        from unittest.mock import MagicMock

        mock_s3 = MagicMock()
        pages_date = [{"CommonPrefixes": [{"Prefix": date_prefix}]}]
        pages_run = [{"CommonPrefixes": [{"Prefix": run_prefix}]}]
        paginator = MagicMock()
        call_count = [0]

        def paginate(**kwargs):
            call_count[0] += 1
            return iter(pages_date if call_count[0] == 1 else pages_run)

        paginator.paginate.side_effect = paginate
        mock_s3.get_paginator.return_value = paginator
        return mock_s3

    def test_path_traversal_in_run_prefix_returns_none(self) -> None:
        """A run prefix containing '..' must be rejected (CWE-22)."""
        from transformation.curated_utils import find_latest_curated_prefix

        mock_s3 = self._make_s3_mock_with_prefix(
            date_prefix="curated/sf/sf-account/curated_date=2026-07-07/",
            run_prefix="curated/sf/sf-account/curated_date=2026-07-07/run_id=../../evil/",
        )
        result = find_latest_curated_prefix(mock_s3, "bucket", "sf", "sf-account", "demo")
        # Must return None — the unsafe prefix is rejected
        assert result is None

    def test_leading_slash_in_run_prefix_returns_none(self) -> None:
        """A run prefix starting with '/' must be rejected."""
        from transformation.curated_utils import find_latest_curated_prefix

        mock_s3 = self._make_s3_mock_with_prefix(
            date_prefix="curated/sf/sf-account/curated_date=2026-07-07/",
            run_prefix="/curated/sf/sf-account/curated_date=2026-07-07/run_id=run-001/",
        )
        result = find_latest_curated_prefix(mock_s3, "bucket", "sf", "sf-account", "demo")
        assert result is None

    def test_safe_prefix_returned_normally(self) -> None:
        """A normal safe run prefix must be returned unchanged."""
        from transformation.curated_utils import find_latest_curated_prefix

        safe_run_prefix = "curated/sf/sf-account/curated_date=2026-07-07/run_id=run-20260707-001/"
        mock_s3 = self._make_s3_mock_with_prefix(
            date_prefix="curated/sf/sf-account/curated_date=2026-07-07/",
            run_prefix=safe_run_prefix,
        )
        result = find_latest_curated_prefix(mock_s3, "bucket", "sf", "sf-account", "demo")
        assert result == safe_run_prefix
