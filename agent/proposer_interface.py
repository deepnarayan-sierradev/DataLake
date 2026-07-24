"""
Semantic-request proposer interface (FR-3.1).

The one seam where a natural-language question becomes a structured
SemanticQueryRequest. A concrete implementation calls an LLM; the agent's
verification loop is provider-agnostic and depends only on this interface, so
the loop is fully testable without a live model. The proposer NEVER returns
SQL — only a structured request resolved against the tenant's semantic model.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from semantic.query_compiler import SemanticQueryRequest
from semantic.semantic_model import SemanticModel


class SemanticRequestProposer(ABC):
    @abstractmethod
    def propose(
        self, *, question: str, model: SemanticModel, prior_error: str | None
    ) -> SemanticQueryRequest:
        """Translate an NL question to a structured request; may use prior_error to self-correct."""
