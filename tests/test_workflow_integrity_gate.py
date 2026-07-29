"""
Negative tests for the workflow-integrity gate (G12).

Every other gate runs on a developer machine, which is why none could see either defect this one
exists for: both were failures of CI to resolve what the workflow named, and both were invisible
locally. The two cases below are the real ones — an untracked file the runner will not have, and a
`requires-python` ceiling that rejects the Python CI installs.

`test_gate_imports_only_the_standard_library` is the load-bearing one. The wiring-gates job
installs no dependencies, so a gate importing `yaml` fails there and passes here. The first draft
of this gate did exactly that.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

GATE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "check_workflow_integrity.py"
sys.path.insert(0, str(GATE_PATH.parent))

import check_workflow_integrity as gate  # noqa: E402


def _stub_repo(tmp_path: Path, workflow_text: str, files: dict[str, str]) -> Path:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(workflow_text, encoding="utf-8")
    for name, body in files.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    return workflows


def _with_root(tmp_path: Path, workflows: Path):
    original = (gate.REPO_ROOT, gate.WORKFLOW_DIR)
    gate.REPO_ROOT, gate.WORKFLOW_DIR = tmp_path, workflows
    return original


def test_untracked_reference_is_rejected(tmp_path: Path) -> None:
    """The `.secrets.baseline` defect: on disk, absent from git, so CI cannot read it."""
    workflows = _stub_repo(
        tmp_path,
        "on: push\njobs:\n  s:\n    steps:\n      - run: hook --baseline .secrets.baseline\n",
        {".secrets.baseline": "{}"},
    )
    original = _with_root(tmp_path, workflows)
    try:
        problems = gate.untracked_references(frozenset())
    finally:
        gate.REPO_ROOT, gate.WORKFLOW_DIR = original

    assert problems, "an untracked file a workflow reads must be rejected"
    assert ".secrets.baseline" in problems[0]


def test_tracked_reference_is_accepted(tmp_path: Path) -> None:
    """Positive control — the same workflow passes once the file is tracked."""
    workflows = _stub_repo(
        tmp_path,
        "on: push\njobs:\n  s:\n    steps:\n      - run: x --baseline .secrets.baseline\n",
        {".secrets.baseline": "{}"},
    )
    original = _with_root(tmp_path, workflows)
    try:
        problems = gate.untracked_references(frozenset({".secrets.baseline"}))
    finally:
        gate.REPO_ROOT, gate.WORKFLOW_DIR = original

    assert problems == []


@pytest.mark.parametrize(
    ("requires", "pinned", "rejected"),
    [
        (">=3.13,<3.14", "3.14.6", True),  # the real defect: four jobs failed at install
        (">=3.13,<3.15", "3.14.6", False),
        (">=3.14", "3.13.2", True),
        (">=3.13,<3.15", "3.13.0", False),
    ],
)
def test_python_pin_must_satisfy_requires_python(
    tmp_path: Path, requires: str, pinned: str, rejected: bool
) -> None:
    """CI's pinned interpreter must satisfy the package's own metadata, or nothing runs."""
    workflows = _stub_repo(
        tmp_path,
        f'on: push\nenv:\n  PYTHON_VERSION: "{pinned}"\njobs:\n  t:\n    steps:\n      - run: x\n',
        {"pyproject.toml": f'[project]\nname = "x"\nrequires-python = "{requires}"\n'},
    )
    original = _with_root(tmp_path, workflows)
    try:
        problems = gate.python_version_mismatch()
    finally:
        gate.REPO_ROOT, gate.WORKFLOW_DIR = original

    assert bool(problems) is rejected


def test_unsupported_specifier_raises_rather_than_passing() -> None:
    """An operator the parser does not understand must fail loudly, never silently succeed."""
    with pytest.raises(ValueError):
        gate.satisfies("3.14.6", "~=3.13")


def test_expressions_and_urls_are_not_treated_as_paths() -> None:
    """`${{ }}` expressions and URLs are not files; flagging them would make the gate unusable."""
    assert gate._candidate_paths("${{ env.PYTHON_VERSION }}") == set()
    assert gate._candidate_paths("pip install 'x @ git+https://github.com/Yelp/ds@01886c'") == set()


def test_gate_imports_only_the_standard_library() -> None:
    """The wiring-gates job installs nothing, so a third-party import here fails only in CI."""
    third_party = {"yaml", "packaging", "boto3", "botocore", "pydantic", "structlog", "requests"}
    tree = ast.parse(GATE_PATH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])

    assert not (imported & third_party), (
        f"{GATE_PATH.name} imports {sorted(imported & third_party)}, but the wiring-gates CI job "
        f"installs no dependencies — it would fail there and pass here"
    )


def test_gate_passes_on_the_real_repository() -> None:
    """The committed tree must satisfy its own gate."""
    result = subprocess.run(
        [sys.executable, "scripts/check_workflow_integrity.py"],
        cwd=GATE_PATH.parent.parent,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
