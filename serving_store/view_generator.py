"""
Serving-layer view generation and row-level security (DL-SERV-04, DL-SEC-09/10/11, DL-SCOPE-15).

Views are generated from the semantic model so the physical serving layer and the governed
definitions cannot drift: a BI tool should not need to know the entity-resolution internals,
and it must not see a column the model does not expose.

Engine capability drives isolation for a `partitioned` tenant. MySQL has **no native row-level
security** — only views — and its current model is database-per-tenant, which provides nothing
*within* a tenant. So a partitioned tenant on MySQL gets schema-per-scope-unit or is declared
unsuitable; PostgreSQL, SQL Server, and Redshift use native RLS policies. This is an
engine-selection constraint, not an implementation detail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from contracts.identifier_policy import SAFE_COLUMN_PATTERN, validate_tenant_code
from contracts.platform_metrics import PlatformMetric
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger
from semantic.semantic_model import SemanticEntity, SemanticModel
from tenancy.scope_contract import (
    PartitionModel,
    TenantPartitionProfile,
    validate_scope_unit_id,
)
from tenancy.scope_predicate import SCOPE_UNIT_COLUMN

_logger = get_platform_logger(__name__)


class ServingEngine(StrEnum):
    """The engines in the loader registry."""

    MYSQL = "mysql"
    POSTGRESQL = "postgresql"
    SQLSERVER = "sqlserver"
    AZURE_SQL = "azure_sql"
    REDSHIFT = "redshift"


# Engines with native row-level security. MySQL is deliberately absent — it has none.
NATIVE_RLS_ENGINES: Final[frozenset[ServingEngine]] = frozenset(
    {
        ServingEngine.POSTGRESQL,
        ServingEngine.SQLSERVER,
        ServingEngine.AZURE_SQL,
        ServingEngine.REDSHIFT,
    }
)


class IsolationMechanism(StrEnum):
    """How within-tenant isolation is achieved on a given engine."""

    NATIVE_RLS = "native_rls"
    SCHEMA_PER_SCOPE_UNIT = "schema_per_scope_unit"
    NOT_REQUIRED = "not_required"
    UNSUITABLE = "unsuitable"


class EngineUnsuitableError(Exception):
    """Raised when an engine cannot provide the isolation a partitioned tenant requires."""


# Two engine enums exist: `ServingStoreEngine` (contracts, persisted in EdlServingStoreConfig)
# and `ServingEngine` (here, drives SQL dialect). Their member values diverge — `mysql_rds` vs
# `mysql` — because the config value names the *hosting* (RDS) and this one names the *dialect*.
# Renaming either would invalidate stored config records, so the divergence is mapped once here
# rather than coerced with `ServingEngine(value)` at each call site, which raises for MySQL.
_CONFIG_ENGINE_TO_DIALECT: Final[dict[str, str]] = {
    "mysql_rds": "mysql",
    "mysql": "mysql",
    "postgresql": "postgresql",
    "sqlserver": "sqlserver",
    "azure_sql": "azure_sql",
    "redshift": "redshift",
}


def serving_engine_from_config(config_engine_value: str) -> ServingEngine:
    """Resolve a persisted `ServingStoreEngine` value to its SQL dialect."""
    dialect = _CONFIG_ENGINE_TO_DIALECT.get(config_engine_value)
    if dialect is None:
        raise EngineUnsuitableError(
            f"Serving engine {config_engine_value!r} has no known SQL dialect mapping. "
            f"Known: {sorted(_CONFIG_ENGINE_TO_DIALECT)}."
        )
    return ServingEngine(dialect)


@dataclass(frozen=True)
class IsolationDecision:
    """The mechanism chosen for one tenant on one engine, and why."""

    engine: ServingEngine
    mechanism: IsolationMechanism
    rationale: str

    @property
    def is_permitted(self) -> bool:
        return self.mechanism is not IsolationMechanism.UNSUITABLE


def decide_isolation(
    engine: ServingEngine,
    profile: TenantPartitionProfile,
    *,
    allow_schema_per_scope_unit: bool = False,
) -> IsolationDecision:
    """
    Choose the within-tenant isolation mechanism (DL-SCOPE-15).

    A `single` tenant is unaffected and keeps the existing database-or-schema-per-tenant model;
    a `partitioned` tenant needs isolation *within* the tenant, which is where MySQL runs out.
    """
    if profile.partition_model is PartitionModel.SINGLE:
        return IsolationDecision(
            engine=engine,
            mechanism=IsolationMechanism.NOT_REQUIRED,
            rationale=(
                "single-partition tenant: the existing database-or-schema-per-tenant boundary "
                "already isolates it, and the scope predicate matches everything"
            ),
        )
    if engine in NATIVE_RLS_ENGINES:
        return IsolationDecision(
            engine=engine,
            mechanism=IsolationMechanism.NATIVE_RLS,
            rationale="engine supports native row-level security policies",
        )
    if allow_schema_per_scope_unit:
        return IsolationDecision(
            engine=engine,
            mechanism=IsolationMechanism.SCHEMA_PER_SCOPE_UNIT,
            rationale=(
                "MySQL has no native row-level security, so each scope unit gets its own schema "
                "with grants scoped to it"
            ),
        )
    return IsolationDecision(
        engine=engine,
        mechanism=IsolationMechanism.UNSUITABLE,
        rationale=(
            "MySQL has no native row-level security and only views, which provide nothing "
            "within a tenant. Choose PostgreSQL, SQL Server, or Redshift for a partitioned "
            "tenant, or opt in to schema-per-scope-unit explicitly"
        ),
    )


def require_suitable_engine(
    engine: ServingEngine,
    profile: TenantPartitionProfile,
    *,
    allow_schema_per_scope_unit: bool = False,
) -> IsolationDecision:
    """Raise rather than silently onboard a partitioned tenant onto an unsuitable engine."""
    decision = decide_isolation(
        engine, profile, allow_schema_per_scope_unit=allow_schema_per_scope_unit
    )
    if not decision.is_permitted:
        # A partitioned tenant on an engine that cannot isolate its units is a resolution-scope
        # violation waiting to happen, so it pages rather than merely failing the apply.
        record_platform_metric(PlatformMetric.RESOLUTION_SCOPE_VIOLATIONS, 1.0, Engine=engine.value)
        raise EngineUnsuitableError(
            f"Engine {engine.value!r} cannot isolate scope units for tenant "
            f"{profile.tenant_code!r}: {decision.rationale}."
        )
    return decision


# ---------------------------------------------------------------------------
# View generation (DL-SERV-04)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServingView:
    """One generated, dialect-specific view definition."""

    view_name: str
    entity_name: str
    engine: ServingEngine
    create_sql: str
    exposed_columns: tuple[str, ...]
    rls_policy_sql: str = ""
    materialised: bool = False


def _quote_identifier(engine: ServingEngine, identifier: str) -> str:
    """Dialect quoting; identifiers are model-derived and allowlisted before they get here."""
    if not SAFE_COLUMN_PATTERN.match(identifier):
        raise ValueError(f"identifier {identifier!r} is not an allowlisted SQL identifier.")
    if engine is ServingEngine.MYSQL:
        return f"`{identifier}`"
    if engine in (ServingEngine.SQLSERVER, ServingEngine.AZURE_SQL):
        return f"[{identifier}]"
    return f'"{identifier}"'


def generate_entity_view(
    entity: SemanticEntity,
    engine: ServingEngine,
    *,
    source_table: str,
    granted_access_tags: frozenset[str],
    include_scope_column: bool = True,
) -> ServingView:
    """
    Build a BI-friendly view over one entity's analytics table.

    A column whose access tag the target audience does not hold is **omitted**, not masked: an
    omitted column does not disclose that the metric exists (DL-SEM/OWASP A01).
    """
    columns: list[str] = []
    for dimension in entity.dimensions:
        if dimension.access_tag and dimension.access_tag not in granted_access_tags:
            continue
        columns.append(dimension.column)
    for time_dimension in entity.time_dimensions:
        if time_dimension.access_tag and time_dimension.access_tag not in granted_access_tags:
            continue
        columns.append(time_dimension.column)
    for metric in entity.metrics:
        if metric.is_derived or metric.column == "*":
            # A derived metric is computed at read time by the compiler; materialising it in a
            # view would create a second definition of the same number.
            continue
        if metric.access_tag and metric.access_tag not in granted_access_tags:
            continue
        columns.append(metric.column)
    if include_scope_column and SCOPE_UNIT_COLUMN not in columns:
        # The security column must be present or the RLS policy has nothing to filter on.
        columns.append(SCOPE_UNIT_COLUMN)

    ordered = tuple(dict.fromkeys(columns))
    if not ordered:
        raise ValueError(
            f"entity {entity.name!r}: no columns are visible to the granted access tags, so no "
            "view can be generated. Grant a tag or exclude the entity."
        )
    quoted = ", ".join(_quote_identifier(engine, column) for column in ordered)
    view_name = f"vw_{entity.name}"
    create_sql = (
        f"CREATE OR REPLACE VIEW {_quote_identifier(engine, view_name)} AS "  # nosec B608 — identifiers pass the allowlist patterns
        f"SELECT {quoted} FROM {_quote_identifier(engine, source_table)}"
    )
    return ServingView(
        view_name=view_name,
        entity_name=entity.name,
        engine=engine,
        create_sql=create_sql,
        exposed_columns=ordered,
    )


def generate_views(
    model: SemanticModel,
    engine: ServingEngine,
    *,
    granted_access_tags: frozenset[str],
    table_name_for: dict[str, str] | None = None,
) -> tuple[ServingView, ...]:
    """Generate a view per entity; a view must never span tenants (OWASP A01)."""
    tables = table_name_for or {}
    views: list[ServingView] = []
    for entity in model.entities:
        source_table = tables.get(entity.name, entity.entity_type)
        try:
            views.append(
                generate_entity_view(
                    entity,
                    engine,
                    source_table=source_table,
                    granted_access_tags=granted_access_tags,
                )
            )
        except ValueError as exc:
            _logger.info(
                "serving_view_skipped_no_visible_columns",
                entity=entity.name,
                reason=str(exc),
            )
    return tuple(views)


# ---------------------------------------------------------------------------
# Row-level security (DL-SEC-09, DL-SEC-10, DL-SEC-11)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RowSecurityPolicy:
    """A native RLS policy for one table on one engine."""

    table_name: str
    policy_name: str
    engine: ServingEngine
    sql_statements: tuple[str, ...]
    security_columns: tuple[str, ...] = field(default_factory=tuple)


DEFAULT_LOADER_ROLE: Final[str] = "edl_loader"


def generate_row_security_policy(
    table_name: str,
    engine: ServingEngine,
    *,
    session_scope_setting: str = "edl.scope_units",
    session_brand_setting: str = "edl.brand_codes",
    include_brand: bool = True,
    loader_role: str = DEFAULT_LOADER_ROLE,
) -> RowSecurityPolicy:
    """
    Emit native RLS statements filtering on the scope unit and, optionally, the brand.

    The predicate reads a **session setting** the connection pool sets from verified claims, not a
    value the client supplies in the query — the same server-side-injection discipline the semantic
    compiler uses.

    **The loader is exempted by role, and must be.** This previously emitted `ENABLE` + `FORCE ROW
    LEVEL SECURITY` with a `FOR SELECT` policy and nothing else. Under RLS, PostgreSQL denies any
    command that has no policy, and `FORCE` removes the table-owner exemption — so the *second*
    incremental load into a table would have been refused, and the hash-diff read that decides what
    changed would have returned zero rows first, making every row look new. A `FOR ALL` policy
    scoped `TO <loader_role>` is preferred over granting `BYPASSRLS`, which is a
    superuser-adjacent attribute the loader does not otherwise need (least privilege, OWASP A01).

    `FORCE` is deliberately kept: without it the table owner reads unfiltered, and the owner is
    reachable from any connection that happens to authenticate as it.

    NULL `scope_unit_id` is excluded, and that is a writer contract rather than a filter gap:
    attribution stamps `__tenant__` for a single-partition tenant, so a NULL is a data defect. The
    Python predicate's `IS NULL` branch exists for pre-attribution rows in the *lake*; rows reaching
    the serving store have been through attribution. Reconciled here on purpose so the two do not
    silently diverge.
    """
    if engine not in NATIVE_RLS_ENGINES:
        raise EngineUnsuitableError(
            f"Engine {engine.value!r} has no native row-level security; use "
            "schema-per-scope-unit instead (DL-SCOPE-15)."
        )
    if not SAFE_COLUMN_PATTERN.match(loader_role):
        raise ValueError(f"loader_role {loader_role!r} is not an allowlisted identifier.")

    quoted_table = _quote_identifier(engine, table_name)
    scope_column = _quote_identifier(engine, SCOPE_UNIT_COLUMN)
    brand_column = _quote_identifier(engine, "brand_code")
    policy_name = f"rls_{table_name}"
    loader_policy_name = f"rls_{table_name}_loader"

    statements: tuple[str, ...]
    if engine in (ServingEngine.POSTGRESQL, ServingEngine.REDSHIFT):
        predicate = (
            f"{scope_column} = ANY (string_to_array("
            f"current_setting('{session_scope_setting}', true), ','))"
        )
        if include_brand:
            predicate += (
                f" AND ({brand_column} IS NULL OR {brand_column} = ANY (string_to_array("
                f"current_setting('{session_brand_setting}', true), ',')))"
            )
        statements = (
            f"ALTER TABLE {quoted_table} ENABLE ROW LEVEL SECURITY;",
            f"ALTER TABLE {quoted_table} FORCE ROW LEVEL SECURITY;",
            f"DROP POLICY IF EXISTS {policy_name} ON {quoted_table};",
            f"CREATE POLICY {policy_name} ON {quoted_table} FOR SELECT USING ({predicate});",
            # Without this the loader's own next upsert is refused: RLS denies any command with no
            # policy, and FORCE removes the owner exemption.
            f"DROP POLICY IF EXISTS {loader_policy_name} ON {quoted_table};",
            f"CREATE POLICY {loader_policy_name} ON {quoted_table} FOR ALL "
            f"TO {loader_role} USING (true) WITH CHECK (true);",
        )
    else:
        predicate_function = f"fn_{policy_name}_predicate"
        statements = (
            f"CREATE OR ALTER FUNCTION {predicate_function}(@scope_unit_id NVARCHAR(64)) "  # nosec B608 — identifiers pass the allowlist patterns
            "RETURNS TABLE WITH SCHEMABINDING AS RETURN "
            "SELECT 1 AS is_visible WHERE @scope_unit_id IN "
            f"(SELECT value FROM STRING_SPLIT(SESSION_CONTEXT(N'{session_scope_setting}'), ',')) "
            # T-SQL has no per-command policy, so the loader exemption lives in the predicate. A
            # filter predicate also constrains the read side of MERGE, which is what the loader
            # uses to decide what changed.
            f"OR IS_ROLEMEMBER('{loader_role}') = 1;",
            f"DROP SECURITY POLICY IF EXISTS {policy_name};",
            f"CREATE SECURITY POLICY {policy_name} ADD FILTER PREDICATE "
            f"{predicate_function}({scope_column}) ON {quoted_table} WITH (STATE = ON);",
        )

    return RowSecurityPolicy(
        table_name=table_name,
        policy_name=policy_name,
        engine=engine,
        sql_statements=statements,
        security_columns=(
            (SCOPE_UNIT_COLUMN, "brand_code") if include_brand else (SCOPE_UNIT_COLUMN,)
        ),
    )


def schema_per_scope_unit_statements(
    tenant_code: str, scope_unit_ids: tuple[str, ...], table_names: tuple[str, ...]
) -> tuple[str, ...]:
    """
    MySQL fallback: one schema per scope unit with a filtered view and a scoped grant.

    Verbose by necessity — it is the price of an engine with no row-level security, and stating
    it explicitly is what makes the engine-selection trade-off visible rather than hidden.
    """
    validate_tenant_code(tenant_code)
    # Both reach generated DDL by interpolation, so both are allowlisted first (OWASP A03).
    for scope_unit_id in scope_unit_ids:
        validate_scope_unit_id(scope_unit_id)
    for table_name in table_names:
        if not SAFE_COLUMN_PATTERN.match(table_name):
            raise EngineUnsuitableError(
                f"table name {table_name!r} is not an allowlisted SQL identifier."
            )
    statements: list[str] = []
    for scope_unit_id in scope_unit_ids:
        schema = f"{tenant_code}_{scope_unit_id}".replace("-", "_")
        reader = f"{schema}_ro"
        statements.append(f"CREATE DATABASE IF NOT EXISTS `{schema}`;")
        for table_name in table_names:
            statements.append(
                f"CREATE OR REPLACE VIEW `{schema}`.`{table_name}` AS "  # nosec B608 — identifiers pass the allowlist patterns
                f"SELECT * FROM `{tenant_code.replace('-', '_')}`.`{table_name}` "
                f"WHERE `{SCOPE_UNIT_COLUMN}` = '{scope_unit_id}';"
            )
        statements.append(f"GRANT SELECT ON `{schema}`.* TO '{reader}'@'%';")
        # No grant on the tenant-wide database: the view reads it, the reader cannot.
        statements.append(f"REVOKE ALL ON `{tenant_code.replace('-', '_')}`.* FROM '{reader}'@'%';")
    return tuple(statements)


def drop_tenant_container_statements(tenant_code: str, engine: ServingEngine) -> tuple[str, str]:
    """
    The DROP that removes a tenant's whole serving-store container (DL-PORT-04).

    Returns `(container_name, statement)`. Generated here rather than in the deletion saga for the
    same reason every other statement is: the container name is derived from an allowlisted
    identifier once, and no caller composes SQL (OWASP A03).

    MySQL's container is a database; every other supported engine's is a schema, and `CASCADE` is
    required there because the schema still holds the tenant's tables.
    """
    validate_tenant_code(tenant_code)
    container = tenant_code.replace("-", "_")
    if not SAFE_COLUMN_PATTERN.match(container):
        raise EngineUnsuitableError(
            f"tenant container {container!r} is not an allowlisted SQL identifier."
        )
    quoted = _quote_identifier(engine, container)
    if engine is ServingEngine.MYSQL:
        return container, f"DROP DATABASE IF EXISTS {quoted};"
    return container, f"DROP SCHEMA IF EXISTS {quoted} CASCADE;"


def serving_view_s3_key(tenant_code: str, version: str, engine: ServingEngine) -> str:
    """Versioned per dialect: `{tenant_code}/serving-views/{version}.{engine}.sql`."""
    validate_tenant_code(tenant_code)
    return f"{tenant_code}/serving-views/{version}.{engine.value}.sql"


def render_view_script(
    views: tuple[ServingView, ...], policies: tuple[RowSecurityPolicy, ...]
) -> str:
    """One idempotent script per dialect, versioned in S3 alongside the model it came from."""
    lines = [
        "-- Generated from the published semantic model. Do not edit by hand:",
        "-- regenerate from the model so the physical layer and the definitions cannot drift.",
        "",
    ]
    for view in views:
        lines.extend([f"-- entity: {view.entity_name}", f"{view.create_sql};", ""])
    for policy in policies:
        lines.append(f"-- row-level security: {policy.table_name}")
        lines.extend(policy.sql_statements)
        lines.append("")
    return "\n".join(lines)
