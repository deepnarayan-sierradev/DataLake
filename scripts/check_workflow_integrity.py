"""
Workflow-integrity gate (G12): CI must be able to run what it references.

Every other gate runs locally, so none of them can see the one failure mode that exists only in
CI: the workflow references something the runner does not have. That shape has now produced two
separate multi-month outages, both invisible on a developer machine.

`.secrets.baseline` matched `.gitignore`'s `*secret*` and was never committed, so the secret-scan
job failed on every run with `Invalid path: .secrets.baseline` while the file sat on every
developer's disk. Same root cause as the eleven source modules that pattern had already hidden,
found and fixed one file short.

`requires-python` was capped at `<3.14` while ci.yml pinned `PYTHON_VERSION: 3.14.6`, so
`pip install -e .` failed before any check ran — lint, typecheck, tests and pip-audit all reported
failure without executing one assertion. Locally the venv already existed, so nothing re-resolved
the constraint and nothing noticed.

Standard library only, deliberately: the wiring-gates job installs no dependencies so that a
resolution failure cannot take the gates down with it. Adding `yaml` here would have broken that
invariant and reproduced this gate's own subject, so the workflows are scanned as text.

Not checked: whether a pinned action SHA resolves. Two were near-misses of real ones
(`…269ef065` against setup-terraform v3.1.2's actual `…269862dd`), and only the GitHub API can
tell those apart — so that class is caught by CI running at all, not by a gate needing a token.
"""

from __future__ import annotations

import re
import subprocess  # nosec B404 — fixed-argv developer tooling only
import sys
import tomllib
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
WORKFLOW_DIR: Final[Path] = REPO_ROOT / ".github" / "workflows"

GENERATED_PATHS: Final[frozenset[str]] = frozenset(
    {"coverage.xml", "bandit-report.json", "checkov-report.sarif", "dist", "htmlcov"}
)

_PYTHON_PIN: Final[re.Pattern[str]] = re.compile(
    r"^\s*PYTHON_VERSION:\s*[\"']?([0-9]+(?:\.[0-9]+)*)[\"']?\s*$", re.MULTILINE
)

_CLAUSE: Final[re.Pattern[str]] = re.compile(r"^(>=|<=|==|!=|<|>)\s*([0-9]+(?:\.[0-9]+)*)$")


def tracked_paths() -> frozenset[str]:
    """Every path git has, relative to the repo root."""
    listing = subprocess.run(  # nosec B603 B607 — literal argv, no shell, git from PATH
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    return frozenset(line for line in listing.stdout.splitlines() if line)


def _candidate_paths(text: str) -> set[str]:
    """Tokens in a workflow that name a file actually present in the working tree."""
    found: set[str] = set()
    for raw in text.replace("=", " ").replace(",", " ").split():
        token = raw.strip("'\"`();|&$<>").removeprefix("./")
        if not token or token.startswith("-") or "://" in token or "${{" in token:
            continue
        if "/" not in token and not token.startswith("."):
            continue
        if (REPO_ROOT / token).is_file():
            found.add(token)
    return found


def untracked_references(tracked: frozenset[str]) -> list[str]:
    """Files a workflow reads that exist locally but not in git — the runner will not have them."""
    problems: list[str] = []
    for workflow in sorted(WORKFLOW_DIR.glob("*.y*ml")):
        text = workflow.read_text(encoding="utf-8")
        for candidate in sorted(_candidate_paths(text)):
            if candidate in tracked or candidate.split("/")[0] in GENERATED_PATHS:
                continue
            problems.append(
                f"{workflow.relative_to(REPO_ROOT)} references {candidate!r}, which exists "
                f"locally but is not tracked by git — the runner will not have it"
            )
    return problems


def _as_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def satisfies(version: str, specifier: str) -> bool:
    """Whether `version` meets every comma-separated clause of `specifier`."""
    actual = _as_tuple(version)
    for raw in specifier.split(","):
        clause = raw.strip()
        if not clause:
            continue
        match = _CLAUSE.match(clause)
        if match is None:
            raise ValueError(f"Unsupported requires-python clause: {clause!r}")
        operator, bound_text = match.groups()
        bound = _as_tuple(bound_text)
        if operator == ">=" and not actual >= bound:
            return False
        if operator == "<=" and not actual <= bound:
            return False
        if operator == ">" and not actual > bound:
            return False
        if operator == "<" and not actual < bound:
            return False
        if operator == "==" and actual[: len(bound)] != bound:
            return False
        if operator == "!=" and actual[: len(bound)] == bound:
            return False
    return True


def python_version_mismatch() -> list[str]:
    """The Python CI installs must satisfy the package's own `requires-python`."""
    pin = _PYTHON_PIN.search((WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8"))
    if pin is None:
        return ["ci.yml declares no PYTHON_VERSION, so nothing pins what CI installs"]
    pinned = pin.group(1)

    manifest = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requires = str(manifest["project"]["requires-python"])
    if satisfies(pinned, requires):
        return []
    return [
        f"ci.yml installs Python {pinned}, which pyproject.toml's requires-python "
        f"({requires}) rejects — `pip install -e .` fails before any check runs"
    ]


def main() -> int:
    problems = python_version_mismatch() + untracked_references(tracked_paths())

    print(f"Workflow integrity: {len(list(WORKFLOW_DIR.glob('*.y*ml')))} workflow file(s)")
    if not problems:
        print("\nOK — CI can resolve every file it references, on the Python it installs.")
        return 0

    print(f"\nFAIL: {len(problems)} problem(s) CI would hit but a local run cannot:\n")
    for problem in problems:
        print(f"  {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
