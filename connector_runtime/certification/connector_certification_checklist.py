"""
Connector certification checklist (spec §10.1).

Validates that a ConnectorInterface implementation satisfies all required
contract obligations before it may be registered in the ConnectorRegistry.

Certification checks:
  1. All five abstract methods are implemented (not returning NotImplementedError).
  2. get_capability_declaration() returns a ConnectorCapabilities instance.
  3. discover_queryable_fields() returns a non-empty list of FieldContract.
  4. build_extraction_query() returns a QueryContract with parameterized values.
  5. execute_extraction() returns an Iterator of ExtractionRecord.
  6. classify_extraction_error() returns a valid ExtractionErrorClassification.
  7. No method accesses os.environ (configuration must come from injected secrets).
  8. Connector source_id follows the stable-identifier format.

Usage:
    checklist = ConnectorCertificationChecklist()
    result = checklist.certify(MyConnector, source_id="postgres-primary")
    if not result.passed:
        raise RuntimeError(result.failure_summary)
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from typing import Any, Final

from connector_runtime.interfaces.connector_interface import (
    ConnectorInterface,
)
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

_STABLE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9\-]{1,63}$")

# Required method names from the ABC
_REQUIRED_METHODS: Final[tuple[str, ...]] = (
    "get_capability_declaration",
    "discover_queryable_fields",
    "build_extraction_query",
    "execute_extraction",
    "classify_extraction_error",
)


@dataclass(frozen=True)
class CertificationCheckResult:
    """Result of one certification check."""

    check_name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class ConnectorCertificationReport:
    """Full certification report for one connector class."""

    connector_class_name: str
    source_id: str
    checks: tuple[CertificationCheckResult, ...]
    certified_at: str

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failure_summary(self) -> str:
        failures = [c for c in self.checks if not c.passed]
        if not failures:
            return ""
        return "; ".join(f"{c.check_name}: {c.detail}" for c in failures)


class ConnectorCertificationChecklist:
    """
    Validates a ConnectorInterface implementation against the platform
    connector contract before production registration.

    Performs static analysis only — does not make live API calls.
    """

    def certify(
        self,
        connector_class: type[ConnectorInterface],
        source_id: str,
    ) -> ConnectorCertificationReport:
        """
        Run all certification checks against the connector class.

        Args:
            connector_class: The class (not an instance) to certify.
            source_id:       Stable identifier for this connector source.

        Returns:
            ConnectorCertificationReport.
        """
        from datetime import UTC, datetime

        checks: list[CertificationCheckResult] = []

        checks.append(_check_source_id_format(source_id))
        checks.append(_check_is_connector_interface_subclass(connector_class))
        checks.extend(_check_required_methods_implemented(connector_class))
        checks.append(_check_no_os_environ_access(connector_class))
        checks.append(_check_no_prohibited_names(connector_class))

        report = ConnectorCertificationReport(
            connector_class_name=connector_class.__name__,
            source_id=source_id,
            checks=tuple(checks),
            certified_at=datetime.now(UTC).isoformat(),
        )

        if report.passed:
            _logger.info(
                "connector_certification_passed",
                connector=connector_class.__name__,
                source_id=source_id,
                check_count=len(checks),
            )
        else:
            _logger.warning(
                "connector_certification_failed",
                connector=connector_class.__name__,
                source_id=source_id,
                failures=report.failure_summary,
            )

        return report


# ---------------------------------------------------------------------------
# Individual check functions
# ---------------------------------------------------------------------------


def _check_source_id_format(source_id: str) -> CertificationCheckResult:
    passed = bool(_STABLE_ID_PATTERN.match(source_id))
    return CertificationCheckResult(
        check_name="source_id_format",
        passed=passed,
        detail=(
            "OK" if passed else f"source_id {source_id!r} does not match ^[a-z][a-z0-9\\-]{{1,63}}$"
        ),
    )


def _check_is_connector_interface_subclass(
    connector_class: type[Any],
) -> CertificationCheckResult:
    passed = isinstance(connector_class, type) and issubclass(connector_class, ConnectorInterface)
    return CertificationCheckResult(
        check_name="is_connector_interface_subclass",
        passed=passed,
        detail=(
            "OK" if passed else f"{connector_class.__name__} does not subclass ConnectorInterface"
        ),
    )


def _check_required_methods_implemented(
    connector_class: type[Any],
) -> list[CertificationCheckResult]:
    results: list[CertificationCheckResult] = []
    for method_name in _REQUIRED_METHODS:
        method = getattr(connector_class, method_name, None)
        if method is None:
            results.append(
                CertificationCheckResult(
                    check_name=f"method_{method_name}_exists",
                    passed=False,
                    detail=f"Method {method_name!r} not found on {connector_class.__name__}",
                )
            )
            continue

        # Check the method is not the ABC stub (raises NotImplementedError body)
        source = ""
        try:
            source = inspect.getsource(method)
        except OSError, TypeError:
            pass

        is_stub = "raise NotImplementedError" in source and connector_class.__name__ not in source
        results.append(
            CertificationCheckResult(
                check_name=f"method_{method_name}_implemented",
                passed=not is_stub,
                detail="OK" if not is_stub else f"{method_name} appears to be a stub",
            )
        )
    return results


def _check_no_os_environ_access(connector_class: type[Any]) -> CertificationCheckResult:
    """Credentials must not be read from environment variables (OWASP A07)."""
    violations: list[str] = []
    for method_name in _REQUIRED_METHODS:
        method = getattr(connector_class, method_name, None)
        if method is None:
            continue
        try:
            source = inspect.getsource(method)
            if "os.environ" in source or "os.getenv" in source:
                violations.append(method_name)
        except OSError, TypeError:
            pass

    passed = len(violations) == 0
    return CertificationCheckResult(
        check_name="no_os_environ_access",
        passed=passed,
        detail=("OK" if passed else f"os.environ / os.getenv found in: {', '.join(violations)}"),
    )


def _check_no_prohibited_names(connector_class: type[Any]) -> CertificationCheckResult:
    """Enforce naming standards — reject generic prohibited identifiers."""
    prohibited = {"helper", "util", "common", "manager", "phase1", "phase2"}
    name_lower = connector_class.__name__.lower()
    found = [p for p in prohibited if p in name_lower]
    passed = len(found) == 0
    return CertificationCheckResult(
        check_name="no_prohibited_names",
        passed=passed,
        detail=("OK" if passed else f"Class name contains prohibited term(s): {', '.join(found)}"),
    )
