"""
Every agent's reasoning step goes through an LLMClient, never a direct
`anthropic.Anthropic()` call scattered through agent code. This is what makes
$0-cost development possible: FakeLLMClient is deterministic and free, and is
exactly what runs when ANTHROPIC_API_KEY is unset (packages/core/config.py).
Swapping to real reasoning in production is a config change, not a code change.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Optional

from packages.core.config import get_settings


class LLMClient(ABC):
    @abstractmethod
    def complete_json(self, *, system: str, prompt: str, model_tier: str = "default") -> dict:
        """Returns a parsed JSON object. model_tier is one of 'cheap' | 'default' | 'strong'
        (see packages/core/config.py for what each maps to)."""
        raise NotImplementedError


class FakeLLMClient(LLMClient):
    """Deterministic, $0-cost, no network calls. Used whenever no ANTHROPIC_API_KEY
    is configured, and always used in tests -- a test suite that needs a live LLM
    to pass is a test suite that can't run in CI without secrets and can't run for
    free. Agents are written to fall back to sensible rule-based behavior against
    this client rather than producing nonsense when it can't semantically reason."""

    def __init__(self, canned_responses: Optional[dict] = None):
        # canned_responses maps a substring of the prompt to the JSON dict to return,
        # so individual tests can steer specific fake answers without a live model.
        self._canned = canned_responses or {}

    def complete_json(self, *, system: str, prompt: str, model_tier: str = "default") -> dict:
        for key, response in self._canned.items():
            if key in prompt:
                return response
        # Default fallback: an honest "I can't reason about this without a real
        # model" response, never invented content dressed up as a real answer.
        return {"_fake_llm": True, "note": "No canned response matched; configure ANTHROPIC_API_KEY for real reasoning."}


class ClaudeClient(LLMClient):
    """Real reasoning, only active when ANTHROPIC_API_KEY is set."""

    def __init__(self):
        import anthropic  # imported lazily so FakeLLMClient-only usage never requires the package to be configured
        settings = get_settings()
        if not settings.anthropic_api_key:
            raise RuntimeError("ClaudeClient constructed without ANTHROPIC_API_KEY set -- use FakeLLMClient instead.")
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self._models = {
            "cheap": settings.anthropic_model_cheap,
            "default": settings.anthropic_model_default,
            "strong": settings.anthropic_model_strong,
        }

    def complete_json(self, *, system: str, prompt: str, model_tier: str = "default") -> dict:
        model = self._models.get(model_tier, self._models["default"])
        response = self._client.messages.create(
            model=model,
            max_tokens=2048,
            system=system + "\n\nRespond with ONLY a JSON object, no other text, no markdown fences.",
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        return json.loads(text)


def get_llm_client() -> LLMClient:
    settings = get_settings()
    if settings.has_live_llm:
        return ClaudeClient()
    return FakeLLMClient()
