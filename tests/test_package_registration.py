"""
Every first-party package is registered in every place that must list it (G11).

`requirements/README.md` names four registration places for a new module. There are **six**, and the
two it omits are the ones that decide what ships:

  1. `pyproject.toml` `[tool.pytest.ini_options].testpaths`   — or the tests never run
  2. `pyproject.toml` `[tool.coverage.run].source`            — or coverage is overstated
  3. `pyproject.toml` isort `known-first-party`               — or imports sort wrongly
  4. `pyproject.toml` `[tool.hatch.build...].packages`        — or the wheel omits it
  5. the `Makefile`'s `lambda-package` copy list              — **or every Lambda fails to import
  it**
  6. the CI `typecheck` mypy scope                            — or it is never type-checked

`persistence/` was created on 2026-07-29 and registered in four of the six. Seventeen production
modules import it, and it was absent from both the wheel and the Lambda copy list — so the deployed
artefact would have raised `ModuleNotFoundError` on the first invocation of any function.

This is the same failure the 2026-07-29 remediation pass proved with
`git clone . /tmp/check && python -c "import connector_runtime.writeback_handler"`, arriving by a
different route: there, `.gitignore` excluded the source; here, the packaging lists omitted it. Both
times the whole test suite was green, because the suite imports from the working tree.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
PYPROJECT: Final[dict] = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
MAKEFILE: Final[str] = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
CI: Final[str] = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

EXCLUDED: Final[dict[str, str]] = {
    "agent": "DL-04 deferred; ships in the Lambda package but is not type-checked",
    "scripts": "operational scripts, checked by `make typecheck-scripts` with relaxed settings",
    "pptx": "presentation generation, excluded from ruff and mypy alike",
}


def _first_party_packages() -> set[str]:
    """Directories that are importable first-party packages with production source."""
    found: set[str] = set()
    for path in sorted(REPO_ROOT.iterdir()):
        if not path.is_dir() or path.name.startswith((".", "_")):
            continue
        if path.name in {"tests", "dist", "docs", "infrastructure", "requirements", "config"}:
            continue
        if not (path / "__init__.py").exists():
            continue
        if any(p.suffix == ".py" and p.name != "__init__.py" for p in path.rglob("*.py")):
            found.add(path.name)
    return found - set(EXCLUDED)


def _hatch_packages() -> set[str]:
    return set(PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"])


def _lambda_copy_list() -> set[str]:
    match = re.search(r"for pkg in ([a-z_ ]+); do", MAKEFILE)
    assert match, "could not parse the lambda-package copy list — the parser has drifted"
    return set(match.group(1).split())


def _testpaths() -> set[str]:
    paths = PYPROJECT["tool"]["pytest"]["ini_options"]["testpaths"]
    return {entry.split("/")[0] for entry in paths}


def _coverage_source() -> set[str]:
    return set(PYPROJECT["tool"]["coverage"]["run"]["source"])


def _isort_first_party() -> set[str]:
    return set(PYPROJECT["tool"]["ruff"]["lint"]["isort"]["known-first-party"])


def _ci_mypy_scope() -> set[str]:
    return set(re.findall(r"-p ([a-z_]+)", CI))


class TestEveryPackageIsRegisteredEverywhere:
    def test_the_discovery_finds_the_packages(self) -> None:
        packages = _first_party_packages()
        assert len(packages) >= 15, f"only discovered {sorted(packages)}"
        assert "persistence" in packages

    @pytest.mark.parametrize(
        ("place", "reader", "consequence"),
        [
            ("hatch wheel packages", _hatch_packages, "the wheel omits it"),
            (
                "Makefile lambda-package copy list",
                _lambda_copy_list,
                "every Lambda fails to import it",
            ),
            ("coverage source", _coverage_source, "coverage is overstated"),
            ("isort known-first-party", _isort_first_party, "imports sort as third-party"),
        ],
    )
    def test_no_package_is_missing_from(self, place: str, reader, consequence: str) -> None:
        missing = sorted(_first_party_packages() - reader())
        assert not missing, f"{missing} absent from {place} — {consequence}"

    def test_every_package_with_tests_is_in_testpaths(self) -> None:
        with_tests = {p for p in _first_party_packages() if (REPO_ROOT / p / "tests").is_dir()}
        missing = sorted(with_tests - _testpaths())
        assert not missing, f"{missing} have tests that CI never runs"

    def test_every_package_is_type_checked_or_excluded_with_a_reason(self) -> None:
        missing = sorted(_first_party_packages() - _ci_mypy_scope())
        assert not missing, (
            f"{missing} are outside the CI mypy scope. Add them, or add them to EXCLUDED here with "
            "the reason — an unstated exclusion is how `knowledge` went unchecked."
        )


class TestExclusionsAreDeliberate:
    @pytest.mark.parametrize("name", sorted(EXCLUDED))
    def test_the_excluded_package_still_exists(self, name: str) -> None:
        assert (REPO_ROOT / name).is_dir(), f"{name} is excluded but no longer exists"

    def test_agent_still_ships_even_though_it_is_not_type_checked(self) -> None:
        assert "agent" in _lambda_copy_list()
        assert "agent" in _hatch_packages()
