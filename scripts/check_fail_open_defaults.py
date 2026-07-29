"""
Fail-open gate (G4): a security-critical parameter may not be omitted, nullable, or skipped.

The 2026-07-28 audit found that `scope_predicate: ScopePredicate | None = None` on the semantic
compiler, the query service, the view generator, and the export service meant every caller that
forgot to pass one silently got tenant-wide results. `_apply_scope_predicate` returned early on
`None`, so the omission produced no error, no log line, and no metric — the isolation control was
inert and looked healthy.

**Three checks, because the first one alone was not enough.** The original gate checked only for a
`None` *default*, and the 2026-07-29 re-assessment found the fix had satisfied it without closing
the hole: every guarded parameter became positionally required but kept `| None` and its early
return, so a caller could still pass `None` and get unfiltered rows. Worse, writing `None`
explicitly reads as deliberate, so review waved it through. A gate that certifies the shape of a
control rather than its effect is how a fail-open survives being fixed.

  1. no `None` default            — the caller cannot omit it
  2. no `X | None` annotation     — the caller cannot pass nothing
  3. no `if <param> is None:` early return / bare `yield from` — there is no branch that skips it

Where a read legitimately has no end-user claim, the caller passes
`tenancy.scope_predicate.unrestricted_predicate(reason)`: an affirmative, named, metered object.
That keeps "unscoped" expressible, auditable, and countable, instead of indistinguishable from a
caller who forgot.

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
    """One way a security parameter can be omitted, nullified, or skipped."""

    path: Path
    line: int
    function: str
    parameter: str
    defect: str

    def render(self) -> str:
        relative = self.path.relative_to(REPO_ROOT)
        reason = GUARDED_PARAMETERS[self.parameter]
        return (
            f"{relative}:{self.line}: {self.function}(...) — {self.parameter}: "
            f"{self.defect}. {reason}"
        )


def _is_none_default(default: ast.expr | None) -> bool:
    return isinstance(default, ast.Constant) and default.value is None


def _is_optional_annotation(annotation: ast.expr | None) -> bool:
    """True for `X | None`, `None | X`, and `Optional[X]` — all of them admit no-value."""
    if annotation is None:
        return False
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return any(
            isinstance(side, ast.Constant) and side.value is None
            for side in (annotation.left, annotation.right)
        ) or any(_is_optional_annotation(side) for side in (annotation.left, annotation.right))
    if isinstance(annotation, ast.Subscript):
        base = annotation.value
        name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
        return name == "Optional"
    # A stringified annotation still spells the union out.
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        text = annotation.value.replace(" ", "")
        return "|None" in text or "None|" in text or text.startswith("Optional[")
    return False


def _guarded_parameters_of(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    args = node.args
    every = args.posonlyargs + args.args + args.kwonlyargs + [args.vararg, args.kwarg]
    return {arg.arg for arg in every if arg is not None and arg.arg in GUARDED_PARAMETERS}


def _skip_branch_violations(
    node: ast.FunctionDef | ast.AsyncFunctionDef, path: Path
) -> list[Violation]:
    """
    Find `if <guarded> is None: return` / `yield from ...` — the branch that silently skips.

    Also matches the attribute form (`self._scope_predicate is None`) and the `is not None`
    guard around the enforcement call, which is the same skip written the other way round.
    """
    found: list[Violation] = []

    def _named(expr: ast.expr) -> str | None:
        if isinstance(expr, ast.Name) and expr.id in GUARDED_PARAMETERS:
            return expr.id
        if isinstance(expr, ast.Attribute):
            bare = expr.attr.lstrip("_")
            if bare in GUARDED_PARAMETERS:
                return bare
        return None

    for inner in ast.walk(node):
        if not isinstance(inner, ast.Compare) or len(inner.ops) != 1:
            continue
        if not isinstance(inner.ops[0], ast.Is | ast.IsNot):
            continue
        comparator = inner.comparators[0]
        if not (isinstance(comparator, ast.Constant) and comparator.value is None):
            continue
        parameter = _named(inner.left)
        if parameter is None:
            continue
        found.append(
            Violation(
                path=path,
                line=inner.lineno,
                function=node.name,
                parameter=parameter,
                defect=(
                    "compared against None, so there is a code path in which the control does "
                    "not run. Make the parameter non-nullable and delete the branch"
                ),
            )
        )
    return found


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
                    Violation(
                        path=path,
                        line=node.lineno,
                        function=node.name,
                        parameter=arg.arg,
                        defect="defaults to None, so a caller can omit it entirely",
                    )
                )
        for arg in args.posonlyargs + args.args + args.kwonlyargs:
            if arg.arg in GUARDED_PARAMETERS and _is_optional_annotation(arg.annotation):
                found.append(
                    Violation(
                        path=path,
                        line=arg.lineno,
                        function=node.name,
                        parameter=arg.arg,
                        defect=(
                            "is annotated as optional, so a caller can pass None and get the "
                            "unenforced path while still satisfying the signature"
                        ),
                    )
                )
        if _guarded_parameters_of(node) or isinstance(node, ast.FunctionDef):
            found.extend(_skip_branch_violations(node, path))
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
