"""
Traceability gate (G5): every active requirement is cited in code, or waived with a reason.

Two failure modes this catches, both real on 2026-07-28:

1. A requirement with no implementation at all — `DL-SCOPE-13` (twin edges respect the scope
   boundary) appeared in no source file, and nothing else noticed.
2. A requirement whose module exists but is unreachable, which the docs then recorded as
   "closed in code". That phrasing reads as done. This gate computes a **wired / declared-only**
   status from the reachability graph (G1) instead of trusting prose.

Deferred phases (DL-04, DL-05) and withdrawn requirements are declared here, not inferred.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_module_reachability import analyse as analyse_reachability

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
REQUIREMENTS_DIR: Final[Path] = REPO_ROOT / "requirements"
WAIVER_FILE: Final[Path] = REQUIREMENTS_DIR / "WAIVERS.md"

# Phases deferred to a separate team by agreement (see requirements/README.md). Their IDs are
# not expected to be implemented and are reported separately rather than as failures.
DEFERRED_DOCUMENTS: Final[frozenset[str]] = frozenset(
    {"DL-04-ai-agent-runtime.md", "DL-05-machine-learning-platform.md"}
)

_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"\bDL-[A-Z]+-\d+\b")
_WAIVER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\s*[-*]\s*`(?P<id>DL-[A-Z]+-\d+)`\s*[—-]\s*(?P<reason>.+\S)\s*$"
)
_SEARCHED_SUFFIXES: Final[tuple[str, ...]] = (".py", ".tf", ".yml", ".yaml", ".json")
_EXCLUDED_PARTS: Final[frozenset[str]] = frozenset({".venv", "__pycache__", "dist", ".git"})


@dataclass
class Citation:
    """Where a requirement id appears, and whether that code is reachable."""

    requirement_id: str
    modules: set[str] = field(default_factory=set)
    non_python_files: set[str] = field(default_factory=set)

    def status(self, reachable: set[str]) -> str:
        if self.modules & reachable:
            return "wired"
        if self.modules:
            return "declared-only"
        if self.non_python_files:
            return "infrastructure"
        return "missing"


def _module_name_for(path: Path) -> str:
    relative = path.relative_to(REPO_ROOT).with_suffix("")
    parts = [part for part in relative.parts if part != "__init__"]
    return ".".join(parts)


def _declared_ids() -> dict[str, str]:
    """Requirement id -> source document, excluding the deferred phases."""
    declared: dict[str, str] = {}
    for doc in sorted(REQUIREMENTS_DIR.glob("DL-*.md")):
        if doc.name in DEFERRED_DOCUMENTS:
            continue
        for match in _ID_PATTERN.finditer(doc.read_text(encoding="utf-8")):
            declared.setdefault(match.group(0), doc.name)
    return declared


def _waivers() -> dict[str, str]:
    if not WAIVER_FILE.exists():
        return {}
    waived: dict[str, str] = {}
    for line in WAIVER_FILE.read_text(encoding="utf-8").splitlines():
        match = _WAIVER_PATTERN.match(line)
        if match:
            waived[match.group("id")] = match.group("reason")
    return waived


def _citations(declared: set[str]) -> dict[str, Citation]:
    citations = {rid: Citation(requirement_id=rid) for rid in declared}
    for path in sorted(REPO_ROOT.rglob("*")):
        if path.suffix not in _SEARCHED_SUFFIXES or not path.is_file():
            continue
        relative_parts = set(path.relative_to(REPO_ROOT).parts)
        if relative_parts & _EXCLUDED_PARTS or "requirements" in relative_parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in _ID_PATTERN.finditer(text):
            rid = match.group(0)
            if rid not in citations:
                continue
            if path.suffix == ".py":
                citations[rid].modules.add(_module_name_for(path))
            else:
                citations[rid].non_python_files.add(str(path.relative_to(REPO_ROOT)))
    return citations


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail when an active requirement is uncited or unreachable."
    )
    parser.add_argument(
        "--allow-declared-only",
        action="store_true",
        help="Report declared-only requirements without failing (use while a phase is in flight).",
    )
    args = parser.parse_args()

    declared = _declared_ids()
    waived = _waivers()
    reachability = analyse_reachability()
    reachable = reachability.reachable
    # A requirement whose every citing module carries a recorded G1 waiver is waived *by module*.
    # Requiring the id to be waived again here would duplicate the same decision in two files and
    # invite them to disagree — the module waiver already names the plan item that wires it.
    waived_modules = set(reachability.waived_and_unreachable)
    citations = _citations(set(declared))

    buckets: dict[str, list[str]] = {
        "wired": [],
        "declared-only": [],
        "infrastructure": [],
        "missing": [],
    }
    for rid in sorted(declared):
        buckets[citations[rid].status(reachable)].append(rid)

    print("Requirement traceability (excludes deferred DL-04 / DL-05)")
    print(f"  declared:        {len(declared)}")
    print(f"  wired:           {len(buckets['wired'])}")
    print(f"  infrastructure:  {len(buckets['infrastructure'])}")
    print(f"  declared-only:   {len(buckets['declared-only'])}")
    print(f"  missing:         {len(buckets['missing'])}")
    print(f"  waived:          {len(waived)}")

    def _waived_by_module(rid: str) -> bool:
        modules = citations[rid].modules
        return bool(modules) and modules <= waived_modules

    unwaived_missing = [rid for rid in buckets["missing"] if rid not in waived]
    unwaived_declared_only = [
        rid for rid in buckets["declared-only"] if rid not in waived and not _waived_by_module(rid)
    ]

    _report(buckets, declared, waived, unwaived_missing, unwaived_declared_only)

    stale = sorted(set(waived) & set(buckets["wired"]))
    if stale:
        print(f"\nSTALE WAIVERS — now wired, remove from {WAIVER_FILE.name}:")
        for rid in stale:
            print(f"  - {rid}")

    if (
        bool(unwaived_missing)
        or bool(stale)
        or (unwaived_declared_only and not args.allow_declared_only)
    ):
        print("\nFAIL: traceability gate")
        return 1
    print("\nOK — every active requirement is cited and reachable, or waived with a reason.")
    return 0


def _report(
    buckets: dict[str, list[str]],
    declared: dict[str, str],
    waived: dict[str, str],
    unwaived_missing: list[str],
    unwaived_declared_only: list[str],
) -> None:
    """Render the buckets; separated from main() to keep each function simple."""
    if unwaived_declared_only:
        print("\nDECLARED-ONLY and unwaived — code exists but no deployed entry point reaches it:")
        for rid in unwaived_declared_only:
            print(f"  - {rid}  [{declared[rid]}]")
        print(
            "\nWire it to an entry point, or record the module's waiver in "
            f"{WAIVER_FILE.name} naming the plan item that will."
        )
    waived_declared_only = len(buckets["declared-only"]) - len(unwaived_declared_only)
    if waived_declared_only:
        print(
            f"\nDECLARED-ONLY by recorded decision: {waived_declared_only} "
            "(their modules carry a G1 waiver naming the plan item that wires them)"
        )

    if unwaived_missing:
        print("\nMISSING — no citation in any source or Terraform file:")
        for rid in unwaived_missing:
            print(f"  - {rid}  [{declared[rid]}]")
        print(
            f"\nCite the requirement id in the code that implements it, or record the decision "
            f"in {WAIVER_FILE.name} as:\n  - `DL-XXX-00` — why it is not implemented"
        )


if __name__ == "__main__":
    sys.exit(main())
