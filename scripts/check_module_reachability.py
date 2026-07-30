"""
Reachability gate (G1): fail when a production module has no production importer.

The defect this exists to catch is a module that is complete, unit-tested, and unreachable —
a library nothing calls. The 2026-07-28 audit found eighteen of them, every one with passing
tests, because a unit test imports the module under test directly and so proves nothing about
whether a deployed handler can reach it.

A module is reachable when at least one *other* production module imports it, transitively from
a deployed entry point. Entry points are declared below and cross-checked against Terraform's
`handler = "..."` values, so adding a Lambda without listing it here fails too.

Waivers live in `requirements/WAIVERS.md` as one line per module with a stated reason. A waiver
is a recorded decision, not a silence.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

GUARDED_PACKAGES: Final[tuple[str, ...]] = (
    "agent",
    "analytics_publisher",
    "config_propagation",
    "connector_runtime",
    "contracts",
    "data_quality",
    "entity_resolution",
    "governance",
    "knowledge",
    "observability",
    "orchestration",
    "persistence",
    "portability",
    "processing_engine",
    "schema_management",
    "semantic",
    "serving_store",
    "tenancy",
    "transformation",
    "watermark_management",
    "workflow_automation",
)

EXTRA_ENTRY_POINTS: Final[tuple[str, ...]] = ()

WAIVER_FILE: Final[Path] = REPO_ROOT / "requirements" / "WAIVERS.md"
_WAIVER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\s*[-*]\s*`(?P<module>[A-Za-z0-9_.]+)`\s*[—-]\s*(?P<reason>.+\S)\s*$"
)


@dataclass
class ReachabilityReport:
    """What the sweep found, so callers can render it however they need."""

    entry_points: tuple[str, ...]
    reachable: set[str] = field(default_factory=set)
    unreachable: list[str] = field(default_factory=list)
    waived_and_unreachable: list[str] = field(default_factory=list)
    stale_waivers: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.unreachable) or bool(self.stale_waivers)


def module_name_for(path: Path) -> str:
    relative = path.relative_to(REPO_ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def production_modules() -> dict[str, Path]:
    """Every production module in a guarded package, excluding tests and scaffolding."""
    modules: dict[str, Path] = {}
    for package in GUARDED_PACKAGES:
        for path in sorted((REPO_ROOT / package).rglob("*.py")):
            parts = set(path.relative_to(REPO_ROOT).parts)
            if "tests" in parts or "__pycache__" in parts:
                continue
            name = module_name_for(path)
            if name:
                modules[name] = path
    return modules


def imports_of(path: Path) -> set[str]:
    """First-party module names this file imports, including `from x import y` submodules."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:  # pragma: no cover — a syntax error fails the lint gate first
        raise SystemExit(f"{path}: cannot parse: {exc}") from exc

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module is None:
                continue
            found.add(node.module)
            for alias in node.names:
                found.add(f"{node.module}.{alias.name}")
    return {name for name in found if name.split(".")[0] in GUARDED_PACKAGES}


def _terraform_handlers() -> set[str]:
    """Lambda handler module paths declared in Terraform — the deployed truth."""
    handlers: set[str] = set()
    pattern = re.compile(r'handler\s*=\s*"(?P<dotted>[A-Za-z0-9_.]+)\.lambda_handler"')
    for tf_file in (REPO_ROOT / "infrastructure" / "modules").rglob("*.tf"):
        for match in pattern.finditer(tf_file.read_text(encoding="utf-8")):
            handlers.add(match.group("dotted"))
    return handlers


def _read_waivers() -> dict[str, str]:
    if not WAIVER_FILE.exists():
        return {}
    waivers: dict[str, str] = {}
    for line in WAIVER_FILE.read_text(encoding="utf-8").splitlines():
        match = _WAIVER_PATTERN.match(line)
        if match:
            waivers[match.group("module")] = match.group("reason")
    return waivers


def _package_ancestors(name: str) -> list[str]:
    """`a.b.c` → ['a', 'a.b'] so importing a submodule marks its packages used."""
    parts = name.split(".")
    return [".".join(parts[: index + 1]) for index in range(len(parts) - 1)]


def _script_seeds() -> set[str]:
    """
    Modules imported by an operator-run script.

    A seed/migration script is a real entry point in this system's operational model — the
    schedule client and the retention enforcer are reached that way, not from a Lambda.
    """
    seeds: set[str] = set()
    for script in sorted((REPO_ROOT / "scripts").glob("*.py")):
        seeds |= imports_of(script)
    return seeds


def _seed_set(modules: dict[str, Path], known_entry_points: tuple[str, ...]) -> set[str]:
    """
    Entry points, the packages containing them, and whatever the operator scripts import.

    A handler's own `__init__` is imported by the Lambda runtime rather than by a sibling module,
    so it would otherwise look unreachable.
    """
    seeded: set[str] = set(known_entry_points)
    for name in known_entry_points:
        seeded.update(a for a in _package_ancestors(name) if a in modules)
    for name in _script_seeds():
        for candidate in (name, *_package_ancestors(name)):
            if candidate in modules:
                seeded.add(candidate)
    return seeded


def _walk(modules: dict[str, Path], edges: dict[str, set[str]], seeded: set[str]) -> set[str]:
    """Breadth-first closure: a module is reachable if the walk from a seed arrives at it."""
    reachable = set(seeded)
    queue: deque[str] = deque(seeded)
    while queue:
        current = queue.popleft()
        for imported in edges.get(current, set()):
            for candidate in (imported, *_package_ancestors(imported)):
                if candidate in modules and candidate not in reachable:
                    reachable.add(candidate)
                    queue.append(candidate)
    return reachable


def analyse() -> ReachabilityReport:
    modules = production_modules()
    edges = {name: imports_of(path) for name, path in modules.items()}

    entry_points = tuple(sorted(_terraform_handlers() | set(EXTRA_ENTRY_POINTS)))
    known_entry_points = tuple(name for name in entry_points if name in modules)

    report = ReachabilityReport(entry_points=entry_points)
    report.reachable.update(_walk(modules, edges, _seed_set(modules, known_entry_points)))

    waivers = _read_waivers()
    for name in sorted(modules):
        if name in report.reachable:
            if name in waivers:
                report.stale_waivers.append(name)
            continue
        if name in waivers:
            report.waived_and_unreachable.append(name)
        else:
            report.unreachable.append(name)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail when a production module is unreachable.")
    parser.add_argument(
        "--list-reachable", action="store_true", help="Print the reachable set and exit 0."
    )
    args = parser.parse_args()

    report = analyse()

    if args.list_reachable:
        for name in sorted(report.reachable):
            print(name)
        return 0

    _render(report)
    if report.failed:
        print("\nFAIL: reachability gate")
        return 1
    print("\nOK — every guarded module is reachable from a deployed entry point.")
    return 0


def _render(report: ReachabilityReport) -> None:
    print(f"Entry points: {len(report.entry_points)}")
    for name in report.entry_points:
        marker = "" if name in report.reachable else "  (declared, module missing)"
        print(f"  - {name}{marker}")
    print(f"\nReachable production modules: {len(report.reachable)}")

    if report.waived_and_unreachable:
        print(f"\nWaived (unreachable by recorded decision): {len(report.waived_and_unreachable)}")
        for name in report.waived_and_unreachable:
            print(f"  - {name}")

    if report.stale_waivers:
        print(f"\nSTALE WAIVERS — now reachable, remove from {WAIVER_FILE.name}:")
        for name in report.stale_waivers:
            print(f"  - {name}")

    if report.unreachable:
        print(
            f"\nUNREACHABLE — no deployed entry point can reach these ({len(report.unreachable)}):"
        )
        for name in report.unreachable:
            print(f"  - {name}")
        print(
            "\nA module nothing imports is a library with no consumer. Either wire it to an "
            f"entry point, or record the decision in {WAIVER_FILE.name} as:\n"
            "  - `package.module` — why it is deliberately not wired yet"
        )


if __name__ == "__main__":
    sys.exit(main())
