"""
Negative test for the security-column gate (G7).

`make banned-names` sat in CI for months unable to fail, because its pattern was BRE alternation
run under `grep -E` and nobody ever fed it a positive case. A gate that has never rejected
anything is indistinguishable from a healthy one, so every gate here must prove it rejects a
known-bad input — and keep proving it, in CI, not once by hand at build time.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_security_column_writers import (
    FILTERED_STORES,
    FilteredStore,
    analyse,
)

_GOOD_RECORD = """
from dataclasses import dataclass


@dataclass(frozen=True)
class Widget:
    widget_id: str
    scope_unit_id: str | None
"""

_BAD_RECORD = """
from dataclasses import dataclass


@dataclass(frozen=True)
class Widget:
    widget_id: str
"""

_GOOD_WRITER = """
def save_widget(table, widget):
    table.put_item(Item={"widget_id": widget.widget_id, "scope_unit_id": widget.scope_unit_id})
"""

_BAD_WRITER = """
def save_widget(table, widget):
    table.put_item(Item={"widget_id": widget.widget_id})
"""


def _store() -> FilteredStore:
    return FilteredStore(
        name="widget",
        column="scope_unit_id",
        record_file="widget.py",
        record_type="Widget",
        writer_file="widget_repository.py",
        writer_symbol="save_widget",
    )


@pytest.fixture
def store_root(tmp_path: Path) -> Path:
    return tmp_path


def _write(root: Path, record: str, writer: str) -> None:
    (root / "widget.py").write_text(record, encoding="utf-8")
    (root / "widget_repository.py").write_text(writer, encoding="utf-8")


class TestGateRejectsKnownBadInput:
    def test_missing_field_on_the_record_is_a_violation(self, store_root: Path) -> None:
        _write(store_root, _BAD_RECORD, _GOOD_WRITER)
        report = analyse([_store()], root=store_root)
        assert report.failed
        assert any("does not declare" in v for v in report.violations)

    def test_writer_that_never_sets_the_column_is_a_violation(self, store_root: Path) -> None:
        _write(store_root, _GOOD_RECORD, _BAD_WRITER)
        report = analyse([_store()], root=store_root)
        assert report.failed
        assert any("never sets" in v for v in report.violations)

    def test_both_missing_reports_both(self, store_root: Path) -> None:
        _write(store_root, _BAD_RECORD, _BAD_WRITER)
        report = analyse([_store()], root=store_root)
        assert len(report.violations) == 2

    def test_absent_record_file_is_a_violation(self, store_root: Path) -> None:
        report = analyse([_store()], root=store_root)
        assert report.failed


class TestGateAcceptsCorrectInput:
    def test_declared_and_written_passes(self, store_root: Path) -> None:
        # The positive control: without this, a gate that always fails would also pass the
        # negative tests above and would be just as useless.
        _write(store_root, _GOOD_RECORD, _GOOD_WRITER)
        report = analyse([_store()], root=store_root)
        assert not report.failed, report.violations


class TestRegistryCannotBeQuietlyEmptied:
    def test_registry_covers_the_scope_filtered_stores(self) -> None:
        # Deleting entries is the cheapest way to make this gate green; the count is the guard.
        assert len(FILTERED_STORES) >= 4
        names = " ".join(store.name for store in FILTERED_STORES)
        assert "twin node" in names
        assert "twin edge" in names

    def test_every_entry_names_a_column(self) -> None:
        assert all(store.column for store in FILTERED_STORES)
