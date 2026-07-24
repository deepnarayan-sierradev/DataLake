"""
LLM structured-completion port (FR-3.1).

Provider-neutral seam for structured model output: a concrete adapter wraps any
provider (Anthropic, OpenAI, Bedrock, a self-hosted model) behind this port, so
the agent and its proposer never import a provider SDK and the model is chosen
by configuration. The client returns a parsed object matching the requested JSON
schema; it NEVER returns SQL.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any


class LlmStructuredClient(ABC):
    @abstractmethod
    def complete_structured(
        self, *, system_prompt: str, user_prompt: str, response_schema: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Return a parsed object conforming to response_schema for the given prompts."""
