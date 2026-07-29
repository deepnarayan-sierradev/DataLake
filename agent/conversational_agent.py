"""
Conversational agent with verification loop (FR-3.1 / FR-3.2 / FR-3.6).

Answers a natural-language question over the semantic layer, read-only and
tenant-scoped. The verification loop is mandatory: on each attempt the agent
(1) asks the proposer for a structured request, (2) compiles it against the
semantic model — catching hallucinated metrics/entities (schema check) and
enforcing access tags, (3) executes the compiled, parameterized query on the
processing engine, and (4) returns the grounded rows with citations. A
schema-invalid proposal is retried with the error fed back (self-correction);
an access-denied field is terminal (retrying cannot grant access); after
max_attempts with no valid query the agent returns an explicit
"cannot answer confidently" rather than an ungrounded guess.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent.proposer_interface import SemanticRequestProposer
from observability.structured_logger import get_platform_logger
from processing_engine.interfaces.set_based_engine_interface import SetBasedQueryEngine
from semantic.query_compiler import (
    AccessDeniedError,
    QueryCompiler,
    SemanticQueryError,
)
from semantic.semantic_model import SemanticModel
from tenancy.scope_predicate import ScopePredicate

_logger = get_platform_logger(__name__)
_ENTITY_VIEW = "entity_data"


@dataclass(frozen=True)
class VerificationChecks:
    schema_valid: bool
    executed: bool
    grounded: bool


@dataclass(frozen=True)
class AgentTurnResult:
    question: str
    answered: bool
    attempts: int
    rows: list[dict[str, Any]] = field(default_factory=list)
    compiled_sql: str | None = None
    checks: VerificationChecks | None = None
    citations: tuple[str, ...] = ()
    reason: str | None = None


class ConversationalAgent:
    def __init__(
        self,
        *,
        proposer: SemanticRequestProposer,
        model: SemanticModel,
        engine: SetBasedQueryEngine,
        entity_uri_resolver: Callable[[str], str],
        granted_access_tags: frozenset[str],
        # The agent queries on a user's behalf, so it carries that user's scope predicate. It is
        # required rather than defaulted: an agent that silently queried tenant-wide would be
        # the worst place for the fail-open of DL-SCOPE-14, since the caller never sees the SQL.
        scope_predicate: ScopePredicate | None,
        max_attempts: int = 3,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1.")
        self._scope_predicate = scope_predicate
        self._proposer = proposer
        self._model = model
        self._compiler = QueryCompiler(model)
        self._engine = engine
        self._resolve_uri = entity_uri_resolver
        self._granted = granted_access_tags
        self._max_attempts = max_attempts

    def ask(self, question: str) -> AgentTurnResult:
        prior_error: str | None = None
        for attempt in range(1, self._max_attempts + 1):
            request = self._proposer.propose(
                question=question, model=self._model, prior_error=prior_error
            )
            try:
                compiled = self._compiler.compile(
                    request,
                    granted_access_tags=self._granted,
                    scope_predicate=self._scope_predicate,
                )
            except AccessDeniedError as exc:
                _logger.info("agent_access_denied", attempt=attempt, reason=str(exc))
                return AgentTurnResult(
                    question=question, answered=False, attempts=attempt, reason=str(exc)
                )
            except SemanticQueryError as exc:
                prior_error = str(exc)
                _logger.info("agent_schema_check_failed", attempt=attempt, reason=prior_error)
                continue

            entity_uri = self._resolve_uri(request.entity)
            try:
                batches = list(
                    self._engine.stream(
                        sql=compiled.sql,
                        inputs={_ENTITY_VIEW: entity_uri},
                        params=compiled.parameters,
                    )
                )
            except Exception as exc:
                prior_error = f"query execution failed: {exc}"
                _logger.info("agent_execution_failed", attempt=attempt, reason=prior_error)
                continue

            rows = [row for batch in batches for row in batch]
            checks = VerificationChecks(schema_valid=True, executed=True, grounded=True)
            _logger.info(
                "agent_answer_verified",
                attempt=attempt,
                entity=request.entity,
                row_count=len(rows),
            )
            return AgentTurnResult(
                question=question,
                answered=True,
                attempts=attempt,
                rows=rows,
                compiled_sql=compiled.sql,
                checks=checks,
                citations=(request.entity,),
            )

        return AgentTurnResult(
            question=question,
            answered=False,
            attempts=self._max_attempts,
            reason=f"Could not produce a verified query after {self._max_attempts} attempts. "
            f"Last error: {prior_error}",
        )
