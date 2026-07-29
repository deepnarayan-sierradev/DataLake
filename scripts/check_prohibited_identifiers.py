"""
Naming gate: prohibited generic identifiers (spec §10.4).

Replaces the `grep` in the Makefile, which **could not fail**. Its pattern used BRE alternation
(`def helper\\b\\|def util\\b\\|...`) but ran under `grep -E`, where `\\|` is a literal pipe — so
the expression only matched the literal text `def helper|def util|...`. A file containing
`def helper():` and `class Manager:` passed it. It had been a required CI job for months and had
never rejected anything.

Three things the old check could not do, which is why this is a script and not a longer pattern:

- match a *suffix* (`SageCredentialManager`, not just `class Manager`);
- match module filenames and package directories (`curated_utils.py`, `sage/common/`);
- be tested. `tests/test_prohibited_identifiers_gate.py` feeds it known-bad input and asserts it
  fails, so it cannot silently rot into a no-op a second time.

Stdlib only, like the other gates.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

# Singular and plural, because `utils` is the form that actually appears in filenames.
PROHIBITED_WORDS: Final[frozenset[str]] = frozenset(
    {
        "helper",
        "helpers",
        "util",
        "utils",
        "utility",
        "utilities",
        "common",
        "manager",
        "managers",
        "misc",
    }
)

EXCLUDED_DIRECTORIES: Final[frozenset[str]] = frozenset(
    {
        ".venv",
        ".git",
        "__pycache__",
        "dist",
        "pptx",
        ".terraform",
        "node_modules",
        ".ruff_cache",
        # Test code is out of scope, as it was under the original rule: a fixture called
        # `_patch_common` names a test concern, not a domain concept. Production code and
        # operator scripts are both in scope — `scripts/` was wrongly excluded before.
        "tests",
    }
)

# Proper nouns whose spelling is not ours to choose. Narrow by construction: the word is excused
# only inside this exact compound, so `SecretsManagerCredentialClient` passes while a bare
# `CredentialManager` still fails. This is not a general escape hatch, and there is no
# file-based allowlist on purpose — a waiver list is how a naming rule dies quietly.
PROPER_NOUN_COMPOUNDS: Final[dict[str, str]] = {"secretsmanager": "manager"}

_CAMEL_BOUNDARY: Final[re.Pattern[str]] = re.compile(r"(?<!^)(?=[A-Z])")


@dataclass
class Report:
    """Where prohibited identifiers were found, so callers can render or assert on it."""

    files_scanned: int = 0
    violations: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.violations)


def _words_in(identifier: str) -> set[str]:
    """Split snake_case and CamelCase into lowercase words."""
    parts: list[str] = []
    for chunk in identifier.split("_"):
        if chunk:
            parts.extend(_CAMEL_BOUNDARY.split(chunk))
    return {part.lower() for part in parts if part}


def _offending_word(identifier: str) -> str | None:
    hit = _words_in(identifier) & PROHIBITED_WORDS
    normalised = identifier.replace("_", "").lower()
    for compound, excused in PROPER_NOUN_COMPOUNDS.items():
        if compound in normalised:
            hit.discard(excused)
    return sorted(hit)[0] if hit else None


def _python_files(root: Path) -> list[Path]:
    return [
        path
        for path in sorted(root.rglob("*.py"))
        if not EXCLUDED_DIRECTORIES & set(path.relative_to(root).parts)
        and not path.name.startswith("test_")
    ]


def analyse(root: Path = REPO_ROOT) -> Report:
    """Find prohibited generic words in module names, package names, classes, and functions."""
    report = Report()
    flagged_directories: set[Path] = set()

    for path in _python_files(root):
        report.files_scanned += 1
        relative = path.relative_to(root)

        for index, part in enumerate(relative.parts[:-1]):
            offender = _offending_word(part)
            directory = Path(*relative.parts[: index + 1])
            if offender and directory not in flagged_directories:
                flagged_directories.add(directory)
                report.violations.append(
                    f"{directory}/ — package name contains prohibited word {offender!r}"
                )

        module_offender = _offending_word(relative.stem)
        if module_offender:
            report.violations.append(
                f"{relative} — module name contains prohibited word {module_offender!r}"
            )

        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            report.violations.append(f"{relative} — could not be parsed: {exc.msg}")
            continue

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                offender = _offending_word(node.name)
                if offender:
                    kind = "class" if isinstance(node, ast.ClassDef) else "function"
                    report.violations.append(
                        f"{relative}:{node.lineno} — {kind} {node.name!r} contains "
                        f"prohibited word {offender!r}"
                    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail on prohibited generic identifiers.")
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    args = parser.parse_args()

    report = analyse(args.root)
    print(f"Checked {report.files_scanned} Python files for prohibited generic identifiers.")

    if report.failed:
        print(f"\nFAIL: {len(report.violations)} prohibited identifier(s) found")
        for violation in report.violations:
            print(f"  - {violation}")
        print(
            "\nName things by domain concept instead (spec §10.4). Prohibited words: "
            f"{', '.join(sorted(PROHIBITED_WORDS))}."
        )
        return 1

    print("\nOK — no prohibited generic identifiers found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
