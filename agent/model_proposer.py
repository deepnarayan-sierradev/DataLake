"""
Model-backed semantic request proposer (FR-3.1).

Turns a natural-language question into a structured SemanticQueryRequest using a
provider-neutral LlmStructuredClient. The prompt lists only the entities,
metrics and dimensions declared in the tenant's SemanticModel, so the model is
grounded in valid names; the compiler still re-validates every name and enforces
access tags downstream (defence in depth). The proposer NEVER emits SQL.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent.llm_client import LlmStructuredClient
from agent.proposer_interface import SemanticRequestProposer
from semantic.query_compiler import SemanticQueryRequest
from semantic.semantic_model import SemanticModel

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity": {"type": "string"},
        "metrics": {"type": "array", "items": {"type": "string"}},
        "dimensions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["entity", "metrics"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = (
    "You translate a business question into a structured analytics request. "
    "Choose exactly one entity and only metric/dimension names from the catalog. "
    "Never write SQL. Respond only with the requested JSON object."
)


class ModelSemanticRequestProposer(SemanticRequestProposer):
    def __init__(self, client: LlmStructuredClient) -> None:
        self._client = client

    def propose(
        self, *, question: str, model: SemanticModel, prior_error: str | None
    ) -> SemanticQueryRequest:
        raw = self._client.complete_structured(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=self._build_prompt(question, model, prior_error),
            response_schema=_RESPONSE_SCHEMA,
        )
        return self._to_request(raw)

    @staticmethod
    def _build_prompt(question: str, model: SemanticModel, prior_error: str | None) -> str:
        lines = ["Catalog:"]
        for entity in model.entities:
            metrics = ", ".join(m.name for m in entity.metrics) or "(none)"
            dimensions = ", ".join(d.name for d in entity.dimensions) or "(none)"
            lines.append(f"- entity {entity.name}: metrics=[{metrics}] dimensions=[{dimensions}]")
        lines.append(f"Question: {question}")
        if prior_error:
            lines.append(f"Your previous attempt was rejected: {prior_error}. Correct it.")
        return "\n".join(lines)

    @staticmethod
    def _to_request(raw: Mapping[str, Any]) -> SemanticQueryRequest:
        # Model output is untrusted — coerce shape only; the compiler re-validates
        # names and enforces access tags downstream (OWASP A03 / A01).
        entity = raw.get("entity")
        if not isinstance(entity, str) or not entity:
            raise ValueError("Proposer response is missing a non-empty string 'entity'.")
        metrics = tuple(str(metric) for metric in raw.get("metrics") or ())
        dimensions = tuple(str(dimension) for dimension in raw.get("dimensions") or ())
        if not metrics:
            raise ValueError("Proposer response must include at least one metric.")
        return SemanticQueryRequest(entity=entity, metrics=metrics, dimensions=dimensions)
