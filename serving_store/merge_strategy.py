"""
Incremental serving-store loads and merge semantics (DL-SERV-05, DL-SERV-06).

Redshift's `COPY` path appends, which silently duplicates a row on every reload once volume
grows. This module makes the merge explicit: stage → merge → verify, per dialect, using a
staging table rather than delete-then-insert (which leaves a window in which a dashboard sees
neither the old nor the new row).

Also emits the sizing artefacts §11 requires: the SOW forbids throttling included capabilities,
so the serving layer is indexed and sized for concurrency rather than rate-limited.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from contracts.identifier_policy import SAFE_COLUMN_PATTERN
from serving_store.view_generator import ServingEngine


class LoadMode(StrEnum):
    """Whether a load replaces the entity or merges into it."""

    FULL = "full"
    INCREMENTAL = "incremental"


class MergeStrategyError(Exception):
    """Raised when a merge cannot be expressed for the given table and keys."""


BULK_LOAD_ROW_THRESHOLD: Final[int] = 5_000


def _validate_identifiers(*identifiers: str) -> None:
    for identifier in identifiers:
        if not SAFE_COLUMN_PATTERN.match(identifier):
            raise MergeStrategyError(
                f"identifier {identifier!r} is not an allowlisted SQL identifier (OWASP A03)."
            )


@dataclass(frozen=True)
class MergePlan:
    """The staged statements for one incremental load."""

    engine: ServingEngine
    target_table: str
    staging_table: str
    primary_keys: tuple[str, ...]
    statements: tuple[str, ...]
    verification_sql: str

    @property
    def statement_count(self) -> int:
        return len(self.statements)


def build_merge_plan(
    engine: ServingEngine,
    target_table: str,
    columns: tuple[str, ...],
    primary_keys: tuple[str, ...],
    *,
    soft_delete_column: str | None = None,
) -> MergePlan:
    """
    Build the stage → merge → verify statement sequence for one entity.

    A merge with no primary key is impossible to express safely, so it raises rather than
    degrading to an append that would duplicate every row.
    """
    if not primary_keys:
        raise MergeStrategyError(
            f"table {target_table!r}: an incremental merge requires at least one primary key. "
            "Without one, a reload appends duplicates instead of updating rows."
        )
    missing = set(primary_keys) - set(columns)
    if missing:
        raise MergeStrategyError(
            f"table {target_table!r}: primary key(s) {sorted(missing)} are not among the loaded "
            "columns."
        )
    _validate_identifiers(target_table, *columns, *primary_keys)
    if soft_delete_column:
        _validate_identifiers(soft_delete_column)

    staging_table = f"stg_{target_table}"
    join_condition = " AND ".join(f"t.{key} = s.{key}" for key in primary_keys)
    non_key_columns = tuple(c for c in columns if c not in primary_keys)

    if engine in (ServingEngine.POSTGRESQL, ServingEngine.REDSHIFT):
        statements = _postgres_family_merge(
            target_table, staging_table, columns, primary_keys, non_key_columns, join_condition
        )
    elif engine in (ServingEngine.SQLSERVER, ServingEngine.AZURE_SQL):
        statements = _sqlserver_merge(
            target_table, staging_table, columns, non_key_columns, join_condition
        )
    else:
        statements = _mysql_merge(target_table, staging_table, columns, non_key_columns)

    if soft_delete_column:
        statements = (
            *statements,
            f"DELETE FROM {target_table} WHERE {soft_delete_column} IN (1, TRUE);",  # nosec B608 — identifiers pass _validate_identifiers
        )

    return MergePlan(
        engine=engine,
        target_table=target_table,
        staging_table=staging_table,
        primary_keys=primary_keys,
        statements=statements,
        verification_sql=(
            f"SELECT COUNT(*) AS merged_rows, COUNT(DISTINCT {primary_keys[0]}) AS distinct_keys "  # nosec B608 — identifiers pass _validate_identifiers
            f"FROM {target_table};"
        ),
    )


def _postgres_family_merge(
    target_table: str,
    staging_table: str,
    columns: tuple[str, ...],
    primary_keys: tuple[str, ...],
    non_key_columns: tuple[str, ...],
    join_condition: str,
) -> tuple[str, ...]:
    """
    Redshift has no `ON CONFLICT`, so both engines use the delete-by-join-then-insert form
    inside one transaction — atomic, and the only shape Redshift supports.
    """
    column_list = ", ".join(columns)
    return (
        "BEGIN;",
        f"CREATE TEMP TABLE {staging_table} (LIKE {target_table});",
        f"-- bulk load staged rows into {staging_table} (COPY or batched INSERT)",
        f"DELETE FROM {target_table} USING {staging_table} s WHERE {join_condition.replace('t.', target_table + '.')};",  # noqa: E501  # nosec B608 — identifiers pass _validate_identifiers
        f"INSERT INTO {target_table} ({column_list}) SELECT {column_list} FROM {staging_table};",  # nosec B608 — identifiers pass _validate_identifiers
        f"DROP TABLE {staging_table};",
        "COMMIT;",
    )


def _sqlserver_merge(
    target_table: str,
    staging_table: str,
    columns: tuple[str, ...],
    non_key_columns: tuple[str, ...],
    join_condition: str,
) -> tuple[str, ...]:
    column_list = ", ".join(columns)
    set_clause = (
        ", ".join(f"t.{c} = s.{c}" for c in non_key_columns) or "t.updated_at = s.updated_at"
    )
    insert_values = ", ".join(f"s.{c}" for c in columns)
    return (
        "BEGIN TRANSACTION;",
        f"SELECT TOP 0 * INTO {staging_table} FROM {target_table};",  # nosec B608 — identifiers pass _validate_identifiers
        f"-- bulk load staged rows into {staging_table}",
        f"MERGE {target_table} AS t USING {staging_table} AS s ON {join_condition} "  # nosec B608 — identifiers pass _validate_identifiers
        f"WHEN MATCHED THEN UPDATE SET {set_clause} "
        f"WHEN NOT MATCHED THEN INSERT ({column_list}) VALUES ({insert_values});",
        f"DROP TABLE {staging_table};",
        "COMMIT TRANSACTION;",
    )


def _mysql_merge(
    target_table: str,
    staging_table: str,
    columns: tuple[str, ...],
    non_key_columns: tuple[str, ...],
) -> tuple[str, ...]:
    column_list = ", ".join(columns)
    fallback = f"{columns[0]} = VALUES({columns[0]})"
    update_clause = ", ".join(f"{c} = VALUES({c})" for c in non_key_columns) or fallback
    return (
        "START TRANSACTION;",
        f"CREATE TEMPORARY TABLE {staging_table} LIKE {target_table};",
        f"-- bulk load staged rows into {staging_table}",
        f"INSERT INTO {target_table} ({column_list}) SELECT {column_list} FROM {staging_table} "  # nosec B608 — identifiers pass _validate_identifiers
        f"ON DUPLICATE KEY UPDATE {update_clause};",
        f"DROP TEMPORARY TABLE {staging_table};",
        "COMMIT;",
    )


@dataclass(frozen=True)
class IndexRecommendation:
    """One index the serving layer needs for interactive concurrency."""

    table_name: str
    columns: tuple[str, ...]
    rationale: str

    def create_sql(self, engine: ServingEngine) -> str:
        name = f"ix_{self.table_name}_{'_'.join(self.columns)}"[:63]
        column_list = ", ".join(self.columns)
        if engine is ServingEngine.REDSHIFT:
            return f"ALTER TABLE {self.table_name} ALTER SORTKEY ({column_list});"
        return f"CREATE INDEX IF NOT EXISTS {name} ON {self.table_name} ({column_list});"


@dataclass(frozen=True)
class SizingProfile:
    """The documented concurrency target and the indexes that support it."""

    concurrent_connections_target: int
    p95_query_latency_ms_target: int
    indexes: tuple[IndexRecommendation, ...] = field(default_factory=tuple)
    materialised_aggregates: tuple[str, ...] = field(default_factory=tuple)

    def render_summary(self) -> str:
        lines = [
            "# Serving-layer sizing",
            "",
            f"- **Concurrency target:** {self.concurrent_connections_target} concurrent "
            "connections",
            f"- **Latency target:** p95 under {self.p95_query_latency_ms_target} ms",
            "",
            "§11 forbids throttling included capabilities, so the layer is sized and indexed "
            "for this concurrency rather than rate-limited.",
            "",
            "## Indexes",
            "",
        ]
        lines.extend(
            f"- `{index.table_name}` on ({', '.join(index.columns)}) — {index.rationale}"
            for index in self.indexes
        )
        if self.materialised_aggregates:
            lines.extend(["", "## Materialised aggregates", ""])
            lines.extend(f"- `{name}`" for name in self.materialised_aggregates)
        lines.append("")
        return "\n".join(lines)


def default_sizing_profile(table_names: tuple[str, ...]) -> SizingProfile:
    """
    The dominant filter columns are the security columns, which is the useful coincidence
    DL-SCOPE-16's performance note points at: the security predicate prunes partitions.
    """
    indexes = tuple(
        IndexRecommendation(
            table_name=table_name,
            columns=("scope_unit_id", "brand_code", "activity_date"),
            rationale=(
                "dominant filter columns: the row-security predicate filters on scope unit and "
                "brand, so indexing them makes security and performance align"
            ),
        )
        for table_name in table_names
    )
    return SizingProfile(
        concurrent_connections_target=50,
        p95_query_latency_ms_target=2_000,
        indexes=indexes,
        materialised_aggregates=tuple(f"mv_{name}_daily" for name in table_names),
    )
