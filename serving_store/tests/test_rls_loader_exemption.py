"""
The RLS policy must not lock out the writer that applies it (F4).

The defect: `generate_row_security_policy` emitted `ENABLE` + `FORCE ROW LEVEL SECURITY` and a
single `FOR SELECT` policy. Under RLS, PostgreSQL denies any command that has no policy, and
`FORCE` removes the table-owner exemption — so the *second* incremental load into a table would
have been refused, and the hash-diff read that decides what changed would have returned zero rows
first, making every row look new. Applied after every load, to a `CREATE TABLE IF NOT EXISTS`
table the next run upserts into.

These are SQL-shape assertions, not behavioural ones. **A real PostgreSQL is still needed to prove
the fix**: the outstanding integration test is load → apply RLS → load again → reader sees only
its own unit. That gap is recorded in docs/SCALE_AND_DLQ_THRESHOLDS.md rather than implied away by
a green unit test.
"""

from __future__ import annotations

import pytest

from serving_store.view_generator import (
    DEFAULT_LOADER_ROLE,
    EngineUnsuitableError,
    ServingEngine,
    generate_row_security_policy,
)

_POSTGRES_LIKE = (ServingEngine.POSTGRESQL, ServingEngine.REDSHIFT)


def _sql(engine: ServingEngine, **kwargs: object) -> str:
    policy = generate_row_security_policy(table_name="dim_customer", engine=engine, **kwargs)  # type: ignore[arg-type]
    return "\n".join(policy.sql_statements)


class TestLoaderCanStillWrite:
    @pytest.mark.parametrize("engine", _POSTGRES_LIKE)
    def test_a_for_all_policy_is_emitted_for_the_loader_role(
        self, engine: ServingEngine
    ) -> None:
        # The assertion the previous implementation failed.
        sql = _sql(engine)
        assert "FOR ALL" in sql
        assert f"TO {DEFAULT_LOADER_ROLE}" in sql

    @pytest.mark.parametrize("engine", _POSTGRES_LIKE)
    def test_the_loader_policy_permits_both_read_and_write(
        self, engine: ServingEngine
    ) -> None:
        # USING governs rows read, WITH CHECK governs rows written. An upsert needs both.
        sql = _sql(engine)
        assert "USING (true) WITH CHECK (true)" in sql

    @pytest.mark.parametrize("engine", _POSTGRES_LIKE)
    def test_the_loader_policy_is_idempotent(self, engine: ServingEngine) -> None:
        # Applied after every load, so a second application must not error.
        assert _sql(engine).count("DROP POLICY IF EXISTS") == 2

    def test_sqlserver_exempts_the_loader_inside_the_predicate(self) -> None:
        # T-SQL has no per-command policy, and a filter predicate also constrains the read side of
        # MERGE — which is exactly what the loader uses to decide what changed.
        sql = _sql(ServingEngine.SQLSERVER)
        assert f"IS_ROLEMEMBER('{DEFAULT_LOADER_ROLE}') = 1" in sql

    def test_the_loader_role_is_configurable(self) -> None:
        assert "TO edl_bulk_writer" in _sql(ServingEngine.POSTGRESQL, loader_role="edl_bulk_writer")


class TestReaderIsolationIsUnchanged:
    @pytest.mark.parametrize("engine", _POSTGRES_LIKE)
    def test_the_reader_policy_is_still_select_only(self, engine: ServingEngine) -> None:
        assert "FOR SELECT USING" in _sql(engine)

    @pytest.mark.parametrize("engine", _POSTGRES_LIKE)
    def test_force_is_retained(self, engine: ServingEngine) -> None:
        # Without FORCE the table owner reads unfiltered, and the owner is reachable from any
        # connection that authenticates as it.
        assert "FORCE ROW LEVEL SECURITY" in _sql(engine)

    @pytest.mark.parametrize("engine", _POSTGRES_LIKE)
    def test_the_predicate_reads_a_session_setting_not_a_client_value(
        self, engine: ServingEngine
    ) -> None:
        # OWASP A03: the caller must not be able to choose which scope units it sees.
        assert "current_setting('edl.scope_units', true)" in _sql(engine)

    @pytest.mark.parametrize("engine", _POSTGRES_LIKE)
    def test_a_null_scope_unit_is_excluded(self, engine: ServingEngine) -> None:
        # Writer contract, not a filter gap: attribution stamps `__tenant__` for a single-partition
        # tenant, so a NULL reaching the serving store is a data defect.
        assert "scope_unit_id IS NULL" not in _sql(engine)


class TestGuardrails:
    def test_an_engine_without_native_rls_is_refused(self) -> None:
        with pytest.raises(EngineUnsuitableError, match="schema-per-scope-unit"):
            generate_row_security_policy(table_name="dim_customer", engine=ServingEngine.MYSQL)

    def test_an_unsafe_loader_role_is_rejected(self) -> None:
        # The role name is interpolated into DDL, so it must pass the allowlist (OWASP A03).
        with pytest.raises(ValueError, match="allowlisted identifier"):
            generate_row_security_policy(
                table_name="dim_customer",
                engine=ServingEngine.POSTGRESQL,
                loader_role='public; DROP TABLE "x"; --',
            )
