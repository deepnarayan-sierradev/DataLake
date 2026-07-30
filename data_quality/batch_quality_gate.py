"""
The bridge between the live transformation path and the DL-02 quality machinery.

Two check granularities exist and both are needed:

- `transformation/quality_evaluation/quality_policy_evaluator.py` checks **one record's fields**
  (null, range, pattern, allowed values) and decides whether that record blocks publication;
- `data_quality/quality_checks.py` checks **the batch** (completeness rate, duplicate rate,
  referential integrity, date sanity) and decides whether the *run* is trustworthy.

They are complementary, not alternatives — a batch of individually-valid records can still be 40%
incomplete, and a single malformed record does not make a batch untrustworthy. The 2026-07-28 audit
initially read them as duplicates; they are not.

What was genuinely missing is this module: the live path ran only the per-record checks and wrote no
structured exception anywhere, so DL-DQ-14's exception store had no producer and the batch
specifications had no caller. This assembles the batch checks from the entity's policy attachment,
runs them, and persists every exception.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from data_quality.exception_repository import (
    DataQualityExceptionRepository,
    ExceptionKind,
    ExceptionSeverity,
    QualityException,
)
from data_quality.quality_checks import (
    BatchCheckContext,
    BatchQualityResult,
    BatchQualitySpecification,
    CompletenessCheck,
    DateValidationCheck,
    DuplicateCheck,
    evaluate_batch_checks,
)
from data_quality.quality_policy_repository import (
    PolicyEnforcementMode,
    QualityPolicyAttachment,
    QualityPolicyNotAttachedError,
    QualityPolicyRepository,
)
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

MAX_PERSISTED_EXCEPTIONS: Final[int] = 500


class QualityGateBlockedError(Exception):
    """Raised when a batch fails a check whose attachment is in blocking mode."""


def build_batch_checks(
    attachment: QualityPolicyAttachment,
) -> tuple[BatchQualitySpecification, ...]:
    """
    Assemble the batch specifications this entity's attachment declares.

    Only checks the attachment can parameterise are built: a completeness check with no required
    fields would measure nothing and report a vacuous pass, which is worse than no check at all
    because it looks like coverage.
    """
    checks: list[BatchQualitySpecification] = []
    if attachment.required_fields:
        checks.append(
            CompletenessCheck(
                required_fields=attachment.required_fields,
                minimum_population_rate_pct=attachment.minimum_population_rate_pct,
            )
        )
    if attachment.natural_key_fields:
        checks.append(
            DuplicateCheck(
                natural_key_fields=attachment.natural_key_fields,
                maximum_duplicate_rate_pct=attachment.maximum_duplicate_rate_pct,
            )
        )
    if attachment.date_fields:
        checks.append(DateValidationCheck(date_fields=attachment.date_fields))
    return tuple(checks)


def run_batch_quality_gate(
    *,
    records: Sequence[dict[str, Any]],
    tenant_code: str,
    entity_id: str,
    run_id: str,
    correlation_id: str,
    environment: str,
    region_name: str,
    source_id: str = "",
    connection_id: str | None = None,
) -> BatchQualityResult | None:
    """
    Run the batch quality gate for one entity, persisting every exception it produces.

    Returns None when the entity has no policy attachment, which is a configuration state rather
    than a failure: an entity nobody has attached a policy to is not silently assumed to be
    perfect, it is simply ungated, and `docs/KNOWN_GAPS_AND_ROADMAP.md` tracks the fact that no
    entity has one attached yet.

    Raises `QualityGateBlockedError` when a check fails and the attachment is in blocking mode.
    The exceptions are persisted **before** the raise, so a blocked run leaves the evidence that
    explains why it blocked (DL-DQ-14).
    """
    try:
        attachment = QualityPolicyRepository(environment=environment, region_name=region_name).get(
            tenant_code, entity_id
        )
    except QualityPolicyNotAttachedError as exc:
        if _is_table_absent(exc):
            _logger.warning(
                "batch_quality_gate_not_deployed",
                tenant_code=tenant_code,
                entity_id=entity_id,
                detail="quality policy table is absent; running ungated",
            )
            return None
        raise
    if attachment is None:
        return None

    checks = build_batch_checks(attachment)
    if not checks:
        _logger.info(
            "batch_quality_gate_no_parameterised_checks",
            tenant_code=tenant_code,
            entity_id=entity_id,
            policy_id=attachment.policy_id,
        )
        return None

    context = BatchCheckContext(
        tenant_code=tenant_code,
        run_id=run_id,
        entity_id=entity_id,
        correlation_id=correlation_id,
        source_id=source_id,
        connection_id=connection_id,
    )
    result = evaluate_batch_checks(checks, records, context)
    _persist(result.exceptions, environment=environment, region_name=region_name)

    if not result.all_passed and attachment.enforcement_mode is PolicyEnforcementMode.BLOCK:
        failed = [outcome.rule_id for outcome in result.outcomes if not outcome.passed]
        raise QualityGateBlockedError(
            f"Entity {entity_id!r} failed batch quality check(s) {failed} and its policy "
            f"attachment {attachment.policy_id!r} is in blocking mode. The exceptions explaining "
            "this decision are recorded in the exception store."
        )
    if not result.all_passed:
        _logger.warning(
            "batch_quality_gate_failed_in_observe_mode",
            tenant_code=tenant_code,
            entity_id=entity_id,
            failed_rules=[o.rule_id for o in result.outcomes if not o.passed],
            enforcement_mode=attachment.enforcement_mode.value,
        )
    return result


def persist_record_violations(
    *,
    violations: Sequence[Any],
    tenant_code: str,
    entity_id: str,
    run_id: str,
    correlation_id: str,
    environment: str,
    region_name: str,
) -> int:
    """
    Persist per-record violations from the field-level evaluator into the same exception store.

    One store, not two: an operator asking "what is outstanding for this entity" must get one
    answer, and a per-record violation and a batch failure are both answers to that question.
    Returns the number persisted.
    """
    if not violations:
        return 0
    exceptions = [
        QualityException(
            tenant_code=tenant_code,
            run_id=run_id,
            rule_id=str(getattr(violation, "check_kind", "field_check")),
            entity_id=entity_id,
            kind=ExceptionKind.QUALITY_VIOLATION,
            severity=_severity_for(violation),
            message=str(getattr(violation, "message", "Field-level quality violation.")),
            correlation_id=correlation_id,
            sequence=index,
            key_field_name=str(getattr(violation, "field_name", "")),
        )
        for index, violation in enumerate(violations)
    ]
    return _persist(exceptions, environment=environment, region_name=region_name)


def _severity_for(violation: Any) -> ExceptionSeverity:
    raw = str(getattr(getattr(violation, "severity", ""), "value", "") or "").lower()
    if raw in {"error", "blocking", "block"}:
        return ExceptionSeverity.ERROR
    return ExceptionSeverity.WARN


def _persist(exceptions: Sequence[QualityException], *, environment: str, region_name: str) -> int:
    if not exceptions:
        return 0
    repository = DataQualityExceptionRepository(environment=environment, region_name=region_name)
    persisted = 0
    for exception in exceptions[:MAX_PERSISTED_EXCEPTIONS]:
        try:
            repository.record(exception)
            persisted += 1
        except Exception as exc:
            _logger.warning(
                "quality_exception_persist_failed",
                tenant_code=exception.tenant_code,
                entity_id=exception.entity_id,
                rule_id=exception.rule_id,
                error=str(exc),
            )
    if len(exceptions) > MAX_PERSISTED_EXCEPTIONS:
        _logger.warning(
            "quality_exceptions_truncated",
            produced=len(exceptions),
            persisted=persisted,
            cap=MAX_PERSISTED_EXCEPTIONS,
        )
    return persisted


def _is_table_absent(error: QualityPolicyNotAttachedError) -> bool:
    """True when the lookup failed because the table itself does not exist."""
    cause = error.__cause__
    code = getattr(cause, "response", {}).get("Error", {}).get("Code", "") if cause else ""
    return str(code) == "ResourceNotFoundException"
