"""
Negative test for the naming gate.

The gate this replaces sat in CI for months unable to fail: its pattern was BRE alternation run
under `grep -E`, so `def helper():` passed it. Nobody ever fed it a positive case. These tests
are the standing proof that this one rejects what it claims to.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_prohibited_identifiers import PROHIBITED_WORDS, analyse


def _write(root: Path, relative: str, body: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class TestGateRejectsWhatTheOldGrepMissed:
    def test_bare_helper_function(self, tmp_path: Path) -> None:
        # The exact input the old Makefile pattern let through.
        _write(tmp_path, "widget.py", "def helper():\n    pass\n")
        assert analyse(tmp_path).failed

    def test_bare_manager_class(self, tmp_path: Path) -> None:
        _write(tmp_path, "widget.py", "class Manager:\n    pass\n")
        assert analyse(tmp_path).failed

    def test_suffixed_class_name(self, tmp_path: Path) -> None:
        # The old grep matched `class Manager` but not a suffix like `WidgetCredentialManager`.
        _write(tmp_path, "widget.py", "class WidgetCredentialManager:\n    pass\n")
        report = analyse(tmp_path)
        assert report.failed
        assert "WidgetCredentialManager" in report.violations[0]

    def test_module_filename(self, tmp_path: Path) -> None:
        _write(tmp_path, "curated_utils.py", "x = 1\n")
        report = analyse(tmp_path)
        assert report.failed
        assert "module name" in report.violations[0]

    def test_package_directory(self, tmp_path: Path) -> None:
        _write(tmp_path, "sage/common/thing.py", "x = 1\n")
        report = analyse(tmp_path)
        assert report.failed
        assert any("package name" in violation for violation in report.violations)

    def test_snake_case_function_suffix(self, tmp_path: Path) -> None:
        _write(tmp_path, "widget.py", "def build_common():\n    pass\n")
        assert analyse(tmp_path).failed


class TestGateAcceptsLegitimateNames:
    def test_domain_named_code_passes(self, tmp_path: Path) -> None:
        # Positive control: a gate that always failed would pass every test above and be useless.
        _write(
            tmp_path,
            "curated_layer_reader.py",
            "class CuratedLayerReader:\n    def read_partition(self):\n        pass\n",
        )
        report = analyse(tmp_path)
        assert not report.failed, report.violations

    @pytest.mark.parametrize(
        "identifier",
        [
            "class SecretsManagerCredentialClient:\n    pass\n",
            "class SecretsManagerCredentialError:\n    pass\n",
            "def _fetch_from_secrets_manager():\n    pass\n",
        ],
    )
    def test_aws_secrets_manager_is_a_proper_noun(self, tmp_path: Path, identifier: str) -> None:
        _write(tmp_path, "credential_client.py", identifier)
        report = analyse(tmp_path)
        assert not report.failed, report.violations

    def test_the_proper_noun_exception_is_narrow(self, tmp_path: Path) -> None:
        # Excusing `SecretsManager` must not excuse `Manager` generally.
        _write(tmp_path, "widget.py", "class CredentialManager:\n    pass\n")
        assert analyse(tmp_path).failed

    def test_test_files_are_out_of_scope(self, tmp_path: Path) -> None:
        _write(tmp_path, "test_widget.py", "def helper():\n    pass\n")
        assert not analyse(tmp_path).failed


class TestVocabularyCannotBeQuietlyEmptied:
    def test_the_four_house_words_are_covered(self) -> None:
        assert {"helper", "util", "common", "manager"} <= PROHIBITED_WORDS

    def test_plural_forms_are_covered(self) -> None:
        # `utils` is the form that actually appears in filenames; singular-only would miss it.
        assert {"utils", "helpers"} <= PROHIBITED_WORDS
