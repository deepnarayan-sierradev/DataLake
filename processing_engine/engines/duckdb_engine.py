"""
DuckDB-backed set-based query engine (FR-F0.1).

Reads Parquet inputs and writes Parquet output directly in S3 via httpfs,
following the duckdb.connect(":memory:") / INSTALL httpfs / SET s3_region
pattern already established in transformation.curated_utils. Result rows are
streamed in Arrow record batches (stream) or written straight to S3 with
COPY (materialize) — the full result set never lands in Python memory.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from observability.structured_logger import get_platform_logger
from processing_engine.interfaces.set_based_engine_interface import (
    QueryOutput,
    SetBasedQueryEngine,
    SetBasedQueryError,
    validate_inputs,
    validate_output_target,
)
from processing_engine.registry import set_based_engine_registry

_logger = get_platform_logger(__name__)


@set_based_engine_registry.register("duckdb")
class DuckDbSetBasedEngine(SetBasedQueryEngine):
    def __init__(self, *, region_name: str) -> None:
        self._region_name = region_name

    def _connect(self) -> Any:
        try:
            import duckdb
        except ImportError as exc:
            raise SetBasedQueryError("duckdb is not available in this runtime.") from exc
        con = duckdb.connect(":memory:")
        # Lambda's HOME is read-only; point DuckDB's home/extension dir at writable /tmp
        # so httpfs/aws INSTALL+LOAD succeed on a cold container.
        con.execute("SET home_directory='/tmp';")
        con.execute("INSTALL httpfs; LOAD httpfs;")
        # OWASP A07: resolve S3 credentials from the execution role's default chain via
        # the aws extension — never static keys. Without this, httpfs S3 reads fail auth.
        con.execute("INSTALL aws; LOAD aws;")
        con.execute("CALL load_aws_credentials();")
        con.execute(f"SET s3_region='{self._region_name}';")
        return con

    def _register_views(self, con: Any, inputs: Mapping[str, str]) -> None:
        for view_name, uri in inputs.items():
            glob = f"{uri.rstrip('/')}/*.parquet"
            # Relation API avoids SQL-string interpolation; view_name allowlisted, glob validated.
            con.read_parquet(glob).create_view(view_name, replace=True)

    def stream(
        self,
        *,
        sql: str,
        inputs: Mapping[str, str],
        params: Sequence[Any] | None = None,
        batch_size: int = 50_000,
    ) -> Iterator[list[dict[str, Any]]]:
        validate_inputs(inputs)
        con = self._connect()
        try:
            self._register_views(con, inputs)
            cursor = con.execute(sql, list(params)) if params else con.execute(sql)
            reader = cursor.fetch_record_batch(batch_size)
            for batch in reader:
                yield batch.to_pylist()
        except SetBasedQueryError:
            raise
        except Exception as exc:
            raise SetBasedQueryError(f"DuckDB stream failed: {exc}") from exc
        finally:
            con.close()

    def materialize(
        self,
        *,
        sql: str,
        inputs: Mapping[str, str],
        output_bucket: str,
        output_prefix: str,
        params: Sequence[Any] | None = None,
    ) -> QueryOutput:
        validate_inputs(inputs)
        output_uri = validate_output_target(output_bucket, output_prefix)
        con = self._connect()
        try:
            self._register_views(con, inputs)
            create_sql = f"CREATE TEMP TABLE _out AS {sql}"
            if params:
                con.execute(create_sql, list(params))
            else:
                con.execute(create_sql)
            row_count = int(con.execute("SELECT count(*) FROM _out").fetchone()[0])
            con.execute(f"COPY _out TO '{output_uri}' (FORMAT PARQUET)")
        except SetBasedQueryError:
            raise
        except Exception as exc:
            raise SetBasedQueryError(f"DuckDB materialize failed: {exc}") from exc
        finally:
            con.close()
        _logger.info("set_based_materialize_complete", output_uri=output_uri, row_count=row_count)
        return QueryOutput(output_uri=output_uri, row_count=row_count)
