"""
Fail-open default gate (G4): security-critical parameters may not default to `None`.

The 2026-07-28 audit found that `scope_predicate: ScopePredicate | None = None` on the semantic
compiler, the query service, the view generator, and the export service meant every caller that
forgot to pass one silently got tenant-wide results. `_apply_scope_predicate` returned early on
`None`, so the omission produced no error, no log line, and no metric — the isolation control was
inert and looked healthy.

An optional security parameter is a fail-open default. This gate makes the omission a build
error rather than a silent authorization bypass: the parameter must be required, so a caller that
does not supply it cannot compile.

`idempotency_guard` is here for the same reason: optional at-most-once is no at-most-once.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

# Parameter name -> why a None default is a security defect. Every entry is a real incident
# class, not a hypothetical.
GUARDED_PARAMETERS: Final[dict[str, str]] = {
    "scope_predicate": (
        "an omitted predicate returns tenant-wide rows across every scope unit (DL-SCOPE-14)"
    ),
    "scope_claims": "an omitted claim set cannot be checked, so the request is unscoped",
    "idempotency_guard": "optional at-most-once means a retry can duplicate an external effect",
    "granted_access_tags": "an omitted tag set would widen column access rather than narrow it",
}

# Only production code is checked. Tests legitimately construct partial objects, and a test that
# passes no predicate is often the test asserting the refusal.
EXCLUDED_PARTS: Final[frozenset[str]] = frozenset({"tests", "__pycache__", ".venv", "dist"})


@dataclass(frozen=True)
class Violation:
    """One security parameter that carries an optional default."""

    path: Path
    line: int
    function: str
    parameter: str

    def render(self) -> str:
        relative = self.path.relative_to(REPO_ROOT)
        reason = GUARDED_PARAMETERS[self.parameter]
        return (
            f"{relative}:{self.line}: {self.function}(...) has `{self.parameter}=None` — {reason}"
        )


def _is_none_default(default: ast.expr | None) -> bool:
    return isinstance(default, ast.Constant) and default.value is None


def _violations_in(path: Path) -> list[Violation]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        args = node.args
        # Positional parameters pair with defaults from the right; keyword-only pair directly.
        positional = args.posonlyargs + args.args
        paired: list[tuple[ast.arg, ast.expr | None]] = [
            (arg, default)
            for arg, default in zip(
                positional[len(positional) - len(args.defaults) :], args.defaults, strict=False
            )
        ]
        paired += list(zip(args.kwonlyargs, args.kw_defaults, strict=False))
        for arg, default in paired:
            if arg.arg in GUARDED_PARAMETERS and _is_none_default(default):
                found.append(
                    Violation(path=path, line=node.lineno, function=node.name, parameter=arg.arg)
                )
    return found


def main() -> int:
    violations: list[Violation] = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        parts = set(path.relative_to(REPO_ROOT).parts)
        if parts & EXCLUDED_PARTS or path.parts[0] == "scripts":
            continue
        violations.extend(_violations_in(path))

    print(f"Checked guarded parameters: {', '.join(sorted(GUARDED_PARAMETERS))}")
    if not violations:
        print("\nOK — no security-critical parameter defaults to None.")
        return 0

    print(f"\nFAIL: {len(violations)} fail-open default(s):\n")
    for violation in violations:
        print(f"  {violation.render()}")
    print(
        "\nMake the parameter required. A caller that must pass a predicate cannot forget to, "
        "and the omission becomes a type error instead of an authorization bypass."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
