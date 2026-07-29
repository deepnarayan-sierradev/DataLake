"""
Negative tests for the paging-primitive gate (G8).

The primitive was introduced with the reasoning that sixteen call sites had independently written
the same `while True: read → extend → LastEvaluatedKey` loop, and that the duplication was how a
truncation bug arose — one site omitted the loop and returned a partial list indistinguishable from
a complete one, with no single place for the omission to be visible.

A day later, two of the sixteen had adopted it. The rest still hand-rolled the loop, and
`index_available` had gone from two copies to three. A shared abstraction most callers ignore does
not remove a failure mode; it adds another way to reproduce it. So adoption is now mechanical rather
than aspirational, and this module proves the gate rejects the shape it exists to reject.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_paging_primitive import violations_in

_HAND_ROLLED_LOOP = """
def list_everything(table, tenant_code):
    items = []
    kwargs = {"KeyConditionExpression": "tenant_code = :tc"}
    while True:
        response = table.query(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return items
"""

_KWARG_FORM = """
def one_page(table, start_key):
    return table.query(KeyConditionExpression="tenant_code = :tc", ExclusiveStartKey=start_key)
"""

_SINGLE_PAGE_OMISSION = """
def list_for_run(table, run_id):
    # The original truncation bug: no loop at all, so this silently stops at 1 MB.
    return table.query(KeyConditionExpression="run_id = :r").get("Items", [])
"""

_USES_THE_PRIMITIVE = """
from persistence.dynamodb_paging import fetch_page, iter_items


def list_everything(table, tenant_code):
    return list(iter_items(table, KeyConditionExpression="tenant_code = :tc"))


def one_page(table, start_key):
    return fetch_page(table, start_key=start_key, KeyConditionExpression="tenant_code = :tc")
"""


def _markers(source: str, tmp_path: Path) -> list[str]:
    module = tmp_path / "candidate.py"
    module.write_text(source, encoding="utf-8")
    return [violation.marker for violation in violations_in(module)]


class TestTheGateRejectsHandRolledPaging:
    def test_a_full_hand_rolled_loop_is_rejected(self, tmp_path: Path) -> None:
        markers = _markers(_HAND_ROLLED_LOOP, tmp_path)
        assert "LastEvaluatedKey" in markers
        assert "ExclusiveStartKey" in markers

    def test_the_keyword_form_is_rejected(self, tmp_path: Path) -> None:
        # `ExclusiveStartKey=` as a kwarg is the same hand-rolled paging, written differently.
        assert "ExclusiveStartKey" in _markers(_KWARG_FORM, tmp_path)


class TestPositiveControl:
    def test_a_module_using_the_primitive_is_accepted(self, tmp_path: Path) -> None:
        # Without this, a gate that flagged every file would pass the tests above.
        assert _markers(_USES_THE_PRIMITIVE, tmp_path) == []

    def test_the_single_page_omission_is_not_what_this_gate_catches(self, tmp_path: Path) -> None:
        """
        Stated so the gate's limit is explicit rather than assumed.

        A read with no loop and no cursor carries neither marker, so G8 cannot see it — G8 enforces
        *where* paging lives, not that a caller paged at all. The defence against the omission is
        that `iter_items` is now the only way to drain, so there is no partial-read shape left to
        write by accident.
        """
        assert _markers(_SINGLE_PAGE_OMISSION, tmp_path) == []


class TestTheRepositoryItselfIsClean:
    def test_no_module_outside_the_primitive_pages_by_hand(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        excluded = {"tests", "__pycache__", ".venv", "dist", "pptx"}
        offenders: list[str] = []
        for path in sorted(repo_root.rglob("*.py")):
            relative = path.relative_to(repo_root)
            if set(relative.parts) & excluded or relative.parts[0] in {"persistence", "scripts"}:
                continue
            offenders.extend(v.render() for v in violations_in(path))
        assert offenders == [], "\n".join(offenders)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
