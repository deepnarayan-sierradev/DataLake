"""
Paging-primitive gate (G8): only `persistence/` may hand-roll a DynamoDB paging loop.

`persistence/dynamodb_paging.py` was introduced on 2026-07-29 with the reasoning that sixteen
call sites had independently written `while True: read → extend → LastEvaluatedKey`, and that the
duplication was how a truncation bug arose — one of the sixteen simply omitted the loop, returned
DynamoDB's first 1 MB page, and produced a partial list indistinguishable from a complete one.
Nothing could detect the omission, because there was no single place for it to be absent from.

The re-assessment a day later found the primitive adopted by **two** of the sixteen. The other
fourteen still hand-rolled the loop, and `index_available` had gone from two copies to three. A
shared abstraction that most callers ignore does not remove the failure mode; it just adds a fourth
way to do the same thing.

So the rule is mechanical: outside `persistence/`, reading `LastEvaluatedKey` or passing
`ExclusiveStartKey` is a build error. Use `iter_items` to drain or `fetch_page` to serve a request.
Scripts are exempt — a one-shot migration is allowed to be self-contained — and so are tests, which
legitimately construct paged responses to drive the primitive.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

# The one package allowed to implement paging. Everything else consumes it.
PRIMITIVE_PACKAGE: Final[str] = "persistence"

# Attribute and keyword names that only appear when a caller is driving paging by hand.
PAGING_MARKERS: Final[frozenset[str]] = frozenset({"LastEvaluatedKey", "ExclusiveStartKey"})

EXCLUDED_PARTS: Final[frozenset[str]] = frozenset({"tests", "__pycache__", ".venv", "dist", "pptx"})


@dataclass(frozen=True)
class Violation:
    """One hand-rolled paging site outside the primitive."""

    path: Path
    line: int
    marker: str

    def render(self) -> str:
        return f"{self.path.relative_to(REPO_ROOT)}:{self.line}: uses {self.marker} directly"


def violations_in(path: Path) -> list[Violation]:
    """Find literal paging markers, whether used as a dict key, a kwarg, or an attribute."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[Violation] = []
    for node in ast.walk(tree):
        marker: str | None = None
        if isinstance(node, ast.Constant) and node.value in PAGING_MARKERS:
            marker = str(node.value)
        elif isinstance(node, ast.keyword) and node.arg in PAGING_MARKERS:
            marker = str(node.arg)
        if marker is not None:
            found.append(Violation(path=path, line=node.lineno, marker=marker))
    return found


def main() -> int:
    violations: list[Violation] = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        relative = path.relative_to(REPO_ROOT)
        if set(relative.parts) & EXCLUDED_PARTS:
            continue
        # `scripts/` holds one-shot migrations that predate the primitive and run standalone.
        if relative.parts[0] in {PRIMITIVE_PACKAGE, "scripts"}:
            continue
        violations.extend(violations_in(path))

    print(f"Paging primitive: {PRIMITIVE_PACKAGE}/dynamodb_paging.py")
    if not violations:
        print("\nOK — no module outside the primitive hand-rolls a DynamoDB paging loop.")
        return 0

    print(f"\nFAIL: {len(violations)} hand-rolled paging site(s):\n")
    for violation in violations:
        print(f"  {violation.render()}")
    print(
        "\nUse persistence.dynamodb_paging: `iter_items(...)` to drain every page, or "
        "`fetch_page(...)` for one bounded page plus a cursor when serving a request."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
