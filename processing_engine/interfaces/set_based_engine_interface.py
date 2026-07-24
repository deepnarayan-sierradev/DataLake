"""
Set-based query engine interface (FR-F0.1).

Runs SQL over Parquet-in-S3 inputs without materialising the full result set in
Python, so record-level work (matching, survivorship, masking, quality, SCD
merge, relationship joins) scales to millions of rows. Implementations register
under a name via ``processing_engine.registry`` — DuckDB for typical volumes,
Athena/Glue for the largest tenants — mirroring the connector/serving-store
adapter+registry pattern.

Security (OWASP A03): SQL is engine-internal (built by platform modules, never
caller input); view names are allowlisted and every S3 input/output prefix is
validated via ``contracts.identifier_policy.validate_s3_prefix``.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from contracts.identifier_policy import validate_s3_prefix

_SAFE_VIEW_NAME: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_S3_URI_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^s3://([a-z0-9][a-z0-9.\-]{1,61}[a-z0-9])/(.+)$"
)
_SAFE_BUCKET_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")


class SetBasedQueryError(Exception):
    """Raised when a set-based query cannot be built or executed."""


@dataclass(frozen=True)
class QueryOutput:
    """Result of a materialise() call."""

    output_uri: str
    row_count: int


def validate_inputs(inputs: Mapping[str, str]) -> None:
    """Validate input view names and their s3:// URIs (OWASP A03)."""
    if not inputs:
        raise SetBasedQueryError("At least one input relation is required.")
    for view_name, uri in inputs.items():
        if not _SAFE_VIEW_NAME.match(view_name):
            raise SetBasedQueryError(f"Unsafe input view name {view_name!r}.")
        match = _S3_URI_PATTERN.match(uri)
        if match is None:
            raise SetBasedQueryError(f"input[{view_name}]={uri!r} must be an s3:// URI.")
        validate_s3_prefix(match.group(2), field_name=f"input[{view_name}]")


def validate_output_target(bucket: str, prefix: str) -> str:
    """Validate an output bucket + prefix and return the full s3:// object URI (OWASP A03)."""
    if not _SAFE_BUCKET_PATTERN.match(bucket):
        raise SetBasedQueryError(f"Unsafe output bucket {bucket!r}.")
    clean_prefix = validate_s3_prefix(prefix, field_name="output_prefix")
    return f"s3://{bucket}/{clean_prefix}/data.parquet"


class SetBasedQueryEngine(ABC):
    """Executes SQL over Parquet-in-S3 inputs, streaming or materialising to S3."""

    @abstractmethod
    def stream(
        self,
        *,
        sql: str,
        inputs: Mapping[str, str],
        params: Sequence[Any] | None = None,
        batch_size: int = 50_000,
    ) -> Iterator[list[dict[str, Any]]]:
        """Yield result rows in batches; never holds the full result set in memory."""

    @abstractmethod
    def materialize(
        self,
        *,
        sql: str,
        inputs: Mapping[str, str],
        output_bucket: str,
        output_prefix: str,
        params: Sequence[Any] | None = None,
    ) -> QueryOutput:
        """Write the query result straight to Parquet in S3; return the output URI and row count."""
