"""
Security-column gate (G7): a column a scope filter reads must be declared and written.

The defect this exists for, found on 2026-07-28: the twin routes filtered on
`twin.scope_unit_id` while `Twin`, `TwinEdge` and the DynamoDB item carried no such field.
`getattr(twin, "scope_unit_id", None)` turned the missing field into `None`, so the filter
always evaluated `matches(None)` — match-all for a `single` tenant, deny-all for a partitioned
one. Every existing gate stayed green:

- G1 saw a reachable module.
- G3 saw the literal `scope_predicate` in the handler source, which is a text assertion.
- The unit tests used `demo`, a `single`-partition tenant where `matches(None)` is `True`.

A filter is only as real as the column it reads. This gate closes the loop from the predicate
back to the writer: for every store a consumption surface filters, the record type must declare
the column and the writer that persists it must set it.

Stdlib only, like the other gates, so CI runs it with no dependency install.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class FilteredStore:
    """One store a consumption surface applies a row filter to."""

    name: str
    column: str
    writer_file: str
    writer_symbol: str
    record_file: str | None = None
    record_type: str | None = None
    column_aliases: tuple[str, ...] = ()


# Every store whose rows are filtered by a `ConsumptionSurface` predicate. Adding a surface that
# filters a new store means adding it here — the entry is what makes the filter checkable.
FILTERED_STORES: Final[tuple[FilteredStore, ...]] = (
    FilteredStore(
        name="twin node (TWIN_TRAVERSAL)",
        column="scope_unit_id",
        record_file="knowledge/twin.py",
        record_type="Twin",
        writer_file="knowledge/twin_repository.py",
        writer_symbol="upsert_twin",
    ),
    FilteredStore(
        name="twin edge (TWIN_TRAVERSAL, DL-SCOPE-13 fan-out)",
        column="scope_unit_id",
        record_file="knowledge/twin.py",
        record_type="TwinEdge",
        writer_file="knowledge/twin_repository.py",
        writer_symbol="upsert_twin",
    ),
    FilteredStore(
        name="quality exception",
        column="scope_unit_id",
        record_file="data_quality/exception_repository.py",
        record_type="QualityException",
        writer_file="data_quality/exception_repository.py",
        writer_symbol="_serialise",
    ),
    FilteredStore(
        name="curated row (attribution source of truth)",
        column="scope_unit_id",
        writer_file="tenancy/scope_attribution.py",
        writer_symbol="stamp",
        column_aliases=("SCOPE_UNIT_COLUMN",),
    ),
)


@dataclass
class Report:
    """What the sweep found, so callers can render or assert on it."""

    checked: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.violations)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _find_class(tree: ast.Module, name: str) -> ast.ClassDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _class_declares(node: ast.ClassDef, column: str) -> bool:
    """True when the class annotates a field named `column` (dataclass or pydantic model)."""
    for statement in node.body:
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            if statement.target.id == column:
                return True
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name) and target.id == column:
                    return True
    return False


def _function_writes(
    node: ast.FunctionDef | ast.AsyncFunctionDef, column: str, aliases: Sequence[str]
) -> bool:
    """True when the writer names the column, as a literal key, an attribute, or an alias."""
    wanted = {column, *aliases}
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and child.value in wanted:
            return True
        if isinstance(child, ast.Name) and child.id in wanted:
            return True
        if isinstance(child, ast.Attribute) and child.attr in wanted:
            return True
    return False


def analyse(stores: Sequence[FilteredStore] = FILTERED_STORES, root: Path = REPO_ROOT) -> Report:
    """Check every filtered store declares and writes its security column."""
    report = Report()
    for store in stores:
        report.checked.append(store.name)

        if store.record_type and store.record_file:
            record_path = root / store.record_file
            if not record_path.exists():
                report.violations.append(f"{store.name}: {store.record_file} does not exist")
                continue
            record_class = _find_class(_parse(record_path), store.record_type)
            if record_class is None:
                report.violations.append(
                    f"{store.name}: {store.record_file} defines no {store.record_type}"
                )
            elif not _class_declares(record_class, store.column):
                report.violations.append(
                    f"{store.name}: {store.record_type} in {store.record_file} does not declare "
                    f"`{store.column}`, so any filter reading it resolves to None"
                )

        writer_path = root / store.writer_file
        if not writer_path.exists():
            report.violations.append(f"{store.name}: {store.writer_file} does not exist")
            continue
        writer = _find_function(_parse(writer_path), store.writer_symbol)
        if writer is None:
            report.violations.append(
                f"{store.name}: {store.writer_file} defines no {store.writer_symbol}(...)"
            )
        elif not _function_writes(writer, store.column, store.column_aliases):
            report.violations.append(
                f"{store.name}: {store.writer_symbol}() in {store.writer_file} never sets "
                f"`{store.column}`, so the column is absent from persisted rows"
            )
    return report


def main() -> int:
    report = analyse()
    print(f"Scope-filtered stores checked: {len(report.checked)}")
    for name in report.checked:
        print(f"  - {name}")

    if report.failed:
        print("\nFAIL: a filtered column is not declared or not written")
        for violation in report.violations:
            print(f"  - {violation}")
        print(
            "\nA row filter on a column no writer emits is not a control. Add the field to the "
            "record type and set it in the writer, or remove the filter."
        )
        return 1

    print("\nOK — every filtered column is declared by its record and set by its writer.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
