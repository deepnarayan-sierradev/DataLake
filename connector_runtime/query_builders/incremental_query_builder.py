"""
Shared incremental-query text assembly (DUP-4).

Salesforce SOQL (salesforce_soql_query_builder.py), NetSuite SuiteQL
(netsuite_incremental_query_planner.py), and MySQL SQL
(mysql_incremental_extractor.py) each independently assembled:

    SELECT {fields} FROM {table}
    [WHERE {watermark_field} >= {lower_placeholder} AND {watermark_field} < {upper_placeholder}]
    [ORDER BY {watermark_field} ASC]

— differing only in placeholder syntax (":lower_bound" for SOQL/SuiteQL vs
"%(lower_bound)s" for MySQL's pymysql pyformat), identifier quoting (none for
SOQL/SuiteQL, backticks for MySQL), and whether an ORDER BY clause is
appended (MySQL only). build_incremental_select() below is that shared
assembly, used by all three.

Scope note: Sage's Intacct and X3 query engines
(adapters/sage/products/{intacct,x3}/*_query_engine.py) are deliberately
NOT built on this module. They produce structured JSON REST request bodies
(an Intacct JSON-DSL filter list / an OData v4 $filter string embedded in a
JSON dict), not a "SELECT ... FROM ... WHERE ..." text string — forcing them
through this same template would require leaking SQL-shaped concepts (a
FROM clause, a flat WHERE string) into two query languages that don't have
them, trading a real reduction in duplication for a leaky, confusing
abstraction. Their own field/watermark validation and placeholder
(__SAGE_*__ / __X3_*__) substitution logic is close in spirit to this
module but is intentionally left standalone (DUP-4).

Field-name/object-name validation and the exact error-message wording for
each are also intentionally left to each connector's own query builder —
each already validates against a strict identifier allowlist pattern before
calling build_incremental_select(), and the messages are test-asserted
verbatim per adapter. This module assumes its inputs are already validated
and safe to interpolate; it does no validation of its own.
"""

from __future__ import annotations

from collections.abc import Callable

from contracts.entity_configuration_contract import LoadType


def NO_QUOTE(identifier: str) -> str:  # noqa: N802 -- public constant-style API, not a method
    """Identity quoting — used by SOQL and SuiteQL, which never quote identifiers."""
    return identifier


def BACKTICK_QUOTE(identifier: str) -> str:  # noqa: N802 -- public constant-style API
    """Backtick quoting — used by MySQL, whose identifiers must be backtick-wrapped."""
    return f"`{identifier}`"


def build_incremental_select(
    *,
    field_names: list[str],
    table: str,
    load_type: LoadType,
    watermark_field: str | None,
    watermark_lower: str | None,
    watermark_upper: str | None,
    lower_bound_placeholder: str,
    upper_bound_placeholder: str,
    quote: Callable[[str], str] = NO_QUOTE,
    include_order_by: bool = False,
) -> tuple[str, dict[str, str | None], str | None]:
    """
    Assemble a `SELECT {fields} FROM {table}` query with an optional
    watermark-bounded WHERE clause, independent of the target query language.

    Callers own all identifier/watermark_field validation *before* calling
    this — it assumes every value it's given is already safe to interpolate
    (Salesforce, NetSuite, and MySQL each validate field/table/watermark_field
    names against a strict identifier allowlist pattern first).

    Args:
        field_names: Validated, ordered column/field names for the SELECT list.
        table: Validated table/object/record-type name for the FROM clause.
        load_type: FULL (no filter) or INCREMENTAL (watermark filter applied).
        watermark_field: Column used for the watermark filter (INCREMENTAL only).
        watermark_lower: Raw lower-bound value. Stored in query_parameters —
            NEVER interpolated into the returned query text.
        watermark_upper: Raw upper-bound value. Stored in query_parameters —
            NEVER interpolated into the returned query text.
        lower_bound_placeholder: Literal placeholder token embedded in the
            WHERE clause for the lower bound (e.g. ":lower_bound",
            "%(lower_bound)s"). The caller's execution layer is responsible
            for binding (or, for SuiteQL, textually substituting after
            ISO-8601 validation) the real value at execution time.
        upper_bound_placeholder: Same, for the upper bound.
        quote: Per-identifier quoting function. Defaults to NO_QUOTE
            (SOQL/SuiteQL); pass BACKTICK_QUOTE for MySQL.
        include_order_by: When True, appends "ORDER BY {quoted watermark_field}
            ASC" for INCREMENTAL queries (MySQL only — SOQL/SuiteQL rely on
            source-side ordering guarantees and don't add this clause).

    Returns:
        (query_text, query_parameters, effective_watermark_field) — the
        triple every call site assembles into its own QueryContract.
    """
    select_clause = ", ".join(quote(name) for name in field_names)
    # OWASP A03: identifiers are pre-validated by the caller against a strict
    # allowlist pattern before reaching this function — no user-controlled
    # input can reach this f-string unvalidated.
    query_text = f"SELECT {select_clause} FROM {quote(table)}"  # noqa: S608
    query_parameters: dict[str, str | None] = {}
    effective_watermark_field: str | None = None

    if load_type == LoadType.INCREMENTAL and watermark_field:
        quoted_watermark = quote(watermark_field)
        query_text = (
            f"{query_text}"
            f" WHERE {quoted_watermark} >= {lower_bound_placeholder}"
            f" AND {quoted_watermark} < {upper_bound_placeholder}"
        )
        if include_order_by:
            query_text = f"{query_text} ORDER BY {quoted_watermark} ASC"
        query_parameters["lower_bound"] = watermark_lower
        query_parameters["upper_bound"] = watermark_upper
        effective_watermark_field = watermark_field

    return query_text, query_parameters, effective_watermark_field
