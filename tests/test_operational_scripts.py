"""
Smoke tests for the operational scripts (Finding 7, 2026-07-29).

`scripts/` holds 27 files, 14 of them Makefile targets an operator runs against a live AWS account —
including `migrate-connections` and `migrate-credentials`, which must run *before* code deploys to
each environment. It was outside the type checker, outside `testpaths`, and skipped by two gates.
Two
real defects were sitting there:

  - `seed_enterprise_semantic_model.py` called `KpiValidationHarness.run()` after that signature was
    tightened, so the only DL-SEM-04 activation path raised `TypeError`;
  - `run_sage_connector_local.py` called `load_entity_config`, a method that has never existed on
    `ConfigurationRepositoryClient`.

`make typecheck-scripts` is what catches that class — both defects were calls inside functions, so
**these tests would not have caught either one**, and saying otherwise would be the overclaiming
this
whole exercise has been about.

What these add is a different class the type checker cannot reach: the module imports at all, and
`--help` works, with no AWS credentials and a stripped environment. A migration that dies at import
or on argument parsing is one that dies after an operator has already committed to running it, and
`migrate-credentials` deletes credential paths.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
MAKEFILE: Final[Path] = REPO_ROOT / "Makefile"

EXCLUDED: Final[frozenset[str]] = frozenset(
    {"scripts/generate_presentation.py", "scripts/_gen_html.py"}
)


def _makefile_scripts() -> list[str]:
    """Every script the Makefile invokes — the definition of 'operational' here."""
    referenced = set(re.findall(r"scripts/[a-z_]+\.py", MAKEFILE.read_text(encoding="utf-8")))
    return sorted(referenced - EXCLUDED)


def test_the_makefile_references_scripts_at_all() -> None:
    assert len(_makefile_scripts()) >= 10


@pytest.mark.parametrize("script", _makefile_scripts())
def test_the_script_imports_without_touching_aws(script: str) -> None:
    """Import must not require credentials, a region, or a live table."""
    result = subprocess.run(  # noqa: S603 — argv list, never a shell string
        [sys.executable, "-c", f"import runpy; runpy.run_path({str(REPO_ROOT / script)!r})"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=90,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO_ROOT), "AWS_REGION": "us-east-1"},
    )
    for fatal in ("ImportError", "ModuleNotFoundError", "NameError", "AttributeError"):
        assert fatal not in result.stderr, f"{script} fails at import:\n{result.stderr[-1500:]}"


@pytest.mark.parametrize("script", _makefile_scripts())
def test_the_script_offers_help_without_side_effects(script: str) -> None:
    """`--help` exercises argument wiring; a script that cannot describe itself is not runnable."""
    result = subprocess.run(  # noqa: S603 — argv list, never a shell string
        [sys.executable, str(REPO_ROOT / script), "--help"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=90,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO_ROOT), "AWS_REGION": "us-east-1"},
    )
    combined = result.stdout + result.stderr
    assert "Traceback" not in combined, f"{script} --help raised:\n{combined[-1500:]}"
