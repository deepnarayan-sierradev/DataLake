"""
Tests for curated_utils shared utility functions.

Coverage:
  - find_latest_curated_prefix: returns None when no partitions exist
  - find_latest_curated_prefix: returns latest by ISO date then run_id
  - find_latest_curated_prefix: handles multiple date partitions (latest wins)
  - load_curated_records: path traversal rejected (OWASP A03)
  - load_curated_records: disallowed characters rejected
  - load_curated_records: loads all Parquet files under prefix
  - source_id_to_domain: hyphens converted to underscores
  - SAFE_S3_PREFIX_PATTERN: valid prefix matches; traversal patterns rejected
"""

from __future__ import annotations

import io
from typing import Any
from unittest.mock import MagicMock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from transformation.curated_utils import (
    SAFE_S3_PREFIX_PATTERN,
    find_latest_curated_prefix,
    load_curated_records,
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
    s3.get_paginator.return_value.paginate.return_value = iter([
        {"CommonPrefixes": [], "Contents": []}
    ])
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

    def _paginate(Bucket, Prefix, **kwargs):  # noqa: N803
        delimiter = kwargs.get("Delimiter")
        if delimiter == "/" and "curated_date=" not in Prefix:
            return iter([{"CommonPrefixes": [
                {"Prefix": f"curated/{domain}/{entity_id}/curated_date={date}/"}
            ], "Contents": []}])
        elif delimiter == "/" and "curated_date=" in Prefix:
            return iter([{"CommonPrefixes": [
                {"Prefix": f"curated/{domain}/{entity_id}/curated_date={date}/run_id={run_id}/"}
            ], "Contents": []}])
        else:
            return iter([{"Contents": [
                {"Key": f"curated/{domain}/{entity_id}/curated_date={date}/run_id={run_id}/data.parquet"}
            ], "CommonPrefixes": []}])

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
        result = find_latest_curated_prefix(s3, "bucket", "salesforce", "salesforce-contact")
        assert result is None

    def test_returns_prefix_with_trailing_slash(self):
        s3 = _mock_s3_with_prefix("salesforce", "salesforce-contact", "2026-07-02", "run-001", [])
        result = find_latest_curated_prefix(s3, "bucket", "salesforce", "salesforce-contact")
        assert result is not None
        assert result.endswith("/")

    def test_returns_latest_date_partition(self):
        """When multiple dates exist, the latest ISO date wins."""
        s3 = MagicMock()

        def _paginate(Bucket, Prefix, **kwargs):  # noqa: N803
            delimiter = kwargs.get("Delimiter")
            if delimiter == "/" and "curated_date=" not in Prefix:
                return iter([{"CommonPrefixes": [
                    {"Prefix": "curated/sf/sf-contact/curated_date=2026-06-01/"},
                    {"Prefix": "curated/sf/sf-contact/curated_date=2026-07-02/"},
                    {"Prefix": "curated/sf/sf-contact/curated_date=2026-06-30/"},
                ], "Contents": []}])
            elif delimiter == "/" and "curated_date=2026-07-02" in Prefix:
                return iter([{"CommonPrefixes": [
                    {"Prefix": "curated/sf/sf-contact/curated_date=2026-07-02/run_id=run-002/"}
                ], "Contents": []}])
            return iter([{"CommonPrefixes": [], "Contents": []}])

        s3.get_paginator.return_value.paginate.side_effect = _paginate
        result = find_latest_curated_prefix(s3, "bucket", "sf", "sf-contact")
        assert result == "curated/sf/sf-contact/curated_date=2026-07-02/run_id=run-002/"

    def test_returns_none_when_no_run_id_subfolders(self):
        """Date partition exists but has no run_id sub-prefixes."""
        s3 = MagicMock()

        def _paginate(Bucket, Prefix, **kwargs):  # noqa: N803
            delimiter = kwargs.get("Delimiter")
            if delimiter == "/" and "curated_date=" not in Prefix:
                return iter([{"CommonPrefixes": [
                    {"Prefix": "curated/sf/sf-c/curated_date=2026-07-02/"}
                ], "Contents": []}])
            return iter([{"CommonPrefixes": [], "Contents": []}])

        s3.get_paginator.return_value.paginate.side_effect = _paginate
        result = find_latest_curated_prefix(s3, "bucket", "sf", "sf-c")
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
        s3.get_paginator.return_value.paginate.return_value = iter([
            {"Contents": [], "CommonPrefixes": []}
        ])
        result = load_curated_records(s3, "bucket", "curated/sf/sf-c/curated_date=2026-07-02/")
        assert result == []
