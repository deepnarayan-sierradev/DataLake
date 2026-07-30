"""
Every Lambda handler must bind correlation context and clear it (F12).

`CLAUDE.md` documents the pattern, and nine of thirteen handlers followed it via
`observability/stage_execution.py`. Three did not — the control-plane API, the pipeline trigger,
and the DLQ processor — so fields were passed ad hoc per call site and an API-initiated action
could not be traced end to end. The pipeline trigger is where DL-11 pins config versions at the
run boundary, which is the single most valuable place to have `run_id` on every line.

The `finally` matters as much as the bind: a warm container that never clears leaks one
invocation's tenant into the next one's logs. That was a real, previously-fixed bug elsewhere in
the platform, which is why this is a test and not a convention.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

EXCLUDED_PARTS: Final[frozenset[str]] = frozenset({".venv", "dist", "__pycache__", "tests"})


def _handler_modules() -> list[Path]:
    """Every production module defining a top-level `lambda_handler`."""
    found: list[Path] = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        if EXCLUDED_PARTS & set(path.relative_to(REPO_ROOT).parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.FunctionDef) and node.name == "lambda_handler"
            for node in tree.body
        ):
            found.append(path)
    return found


HANDLERS: Final[list[Path]] = _handler_modules()


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestEveryHandlerCarriesCorrelationContext:
    def test_the_sweep_finds_the_handlers(self) -> None:
        assert len(HANDLERS) >= 12, [str(p) for p in HANDLERS]

    @pytest.mark.parametrize("path", HANDLERS, ids=lambda p: p.stem)
    def test_binds_context_or_delegates_to_stage_execution(self, path: Path) -> None:
        text = _source(path)
        delegates = "stage_execution" in text
        binds = "bind_contextvars" in text
        assert delegates or binds, (
            f"{path.relative_to(REPO_ROOT)} defines lambda_handler but neither uses "
            "observability/stage_execution.py nor binds structlog contextvars. Logs from this "
            "handler cannot be correlated to a tenant or a run."
        )

    @pytest.mark.parametrize("path", HANDLERS, ids=lambda p: p.stem)
    def test_hand_rolled_binding_also_clears(self, path: Path) -> None:
        text = _source(path)
        if "stage_execution" in text or "bind_contextvars" not in text:
            return
        assert "clear_contextvars" in text, (
            f"{path.relative_to(REPO_ROOT)} binds contextvars without clearing them. On a warm "
            "container this leaks one invocation's context into the next."
        )
        assert "finally" in text, (
            f"{path.relative_to(REPO_ROOT)} clears contextvars outside a `finally`, so an "
            "exception path leaves stale context bound."
        )
