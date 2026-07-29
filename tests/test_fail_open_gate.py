"""
Negative tests for the fail-open gate (G4).

G4 existed before the 2026-07-29 re-assessment and passed throughout it — while the defect it was
written for was intact in three modules. It checked for a `None` *default*, so the fix that made
every guarded parameter positionally required satisfied it, even though each kept `| None` and its
early return. A caller could still pass `None` and get the unenforced path.

So this module drives the gate at each of the three shapes it must now reject, plus a positive
control. Without the control, a gate that rejected everything would pass every negative test and
be exactly as useless as one that rejected nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_fail_open_defaults import _violations_in

_CLEAN = """
def compile_query(request: str, *, scope_predicate: ScopePredicate) -> str:
    return apply(request, scope_predicate)
"""

_NONE_DEFAULT = """
def compile_query(request: str, *, scope_predicate: ScopePredicate | None = None) -> str:
    return apply(request, scope_predicate)
"""

_OPTIONAL_ANNOTATION = """
def compile_query(request: str, *, scope_predicate: ScopePredicate | None) -> str:
    return apply(request, scope_predicate)
"""

_OPTIONAL_VIA_TYPING = """
def compile_query(request: str, *, scope_predicate: Optional[ScopePredicate]) -> str:
    return apply(request, scope_predicate)
"""

_SKIP_BRANCH = """
def apply_scope(scope_predicate: ScopePredicate, plan: Plan) -> None:
    if scope_predicate is None:
        return
    plan.prepend_where(scope_predicate.sql)
"""

_SKIP_BRANCH_ON_ATTRIBUTE = """
class Engine:
    def act(self, context: Context) -> None:
        if self._idempotency_guard is not None and not self._idempotency_guard.claim(context.key):
            return
        self._fire(context)
"""

_YIELD_FROM_SKIP = """
def apply_scope(rows: Iterable[dict], scope_predicate: ScopePredicate) -> Iterator[dict]:
    if scope_predicate is None:
        yield from rows
        return
    for row in rows:
        if scope_predicate.matches(row.get("scope_unit_id")):
            yield row
"""


def _violations(source: str, tmp_path: Path) -> list[str]:
    module = tmp_path / "candidate.py"
    module.write_text(source, encoding="utf-8")
    return [v.defect for v in _violations_in(module)]


class TestTheGateRejectsEveryShapeOfOmission:
    def test_a_none_default_is_rejected(self, tmp_path: Path) -> None:
        assert any("defaults to None" in d for d in _violations(_NONE_DEFAULT, tmp_path))

    def test_an_optional_annotation_is_rejected(self, tmp_path: Path) -> None:
        # The exact shape that survived the previous fix: required in position, nullable in type.
        assert any(
            "annotated as optional" in d for d in _violations(_OPTIONAL_ANNOTATION, tmp_path)
        )

    def test_optional_from_typing_is_rejected(self, tmp_path: Path) -> None:
        assert any(
            "annotated as optional" in d for d in _violations(_OPTIONAL_VIA_TYPING, tmp_path)
        )

    def test_an_early_return_skip_branch_is_rejected(self, tmp_path: Path) -> None:
        assert any("compared against None" in d for d in _violations(_SKIP_BRANCH, tmp_path))

    def test_a_yield_from_skip_branch_is_rejected(self, tmp_path: Path) -> None:
        # The export defect: `yield from rows` returns every scope unit's rows unfiltered.
        assert any("compared against None" in d for d in _violations(_YIELD_FROM_SKIP, tmp_path))

    def test_the_attribute_form_of_the_skip_is_rejected(self, tmp_path: Path) -> None:
        # `self._idempotency is not None and ...` is the same skip written the other way round.
        assert any(
            "compared against None" in d for d in _violations(_SKIP_BRANCH_ON_ATTRIBUTE, tmp_path)
        )


class TestPositiveControl:
    def test_a_correctly_written_function_is_accepted(self, tmp_path: Path) -> None:
        # Without this, a gate that flagged every file would pass every test above.
        assert _violations(_CLEAN, tmp_path) == []

    def test_an_unguarded_parameter_name_is_ignored(self, tmp_path: Path) -> None:
        # The gate is scoped to the named security parameters, not to `| None` in general.
        source = "def f(retry_limit: int | None = None) -> None:\n    return None\n"
        assert _violations(source, tmp_path) == []


class TestTheRepositoryItselfIsClean:
    def test_no_production_module_carries_a_fail_open_shape(self) -> None:
        """The gate is only meaningful if it is green on HEAD for the right reason."""
        repo_root = Path(__file__).resolve().parent.parent
        excluded = {"tests", "__pycache__", ".venv", "dist"}
        offenders: list[str] = []
        for path in sorted(repo_root.rglob("*.py")):
            parts = set(path.relative_to(repo_root).parts)
            if parts & excluded or path.parts[0] == "scripts":
                continue
            offenders.extend(v.render() for v in _violations_in(path))
        assert offenders == [], "\n".join(offenders)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
