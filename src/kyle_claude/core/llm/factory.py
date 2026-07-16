from __future__ import annotations

from kyle_claude.core.config import LlmConfig
from kyle_claude.core.llm.base import LLMProvider
from kyle_claude.core.llm.openai_compatible import OpenAICompatibleProvider
from kyle_claude.core.llm.provider import AnthropicProvider


def create_llm_provider(config: LlmConfig) -> LLMProvider:
    provider = config.provider.lower().replace("-", "_")
    if provider == "anthropic":
        return AnthropicProvider(config.default_model)
    if provider in {"openai", "openai_compatible"}:
        return OpenAICompatibleProvider(
            config.default_model,
            base_url=config.base_url,
            api_key_env=config.api_key_env,
        )
    raise SystemExit(f"Unsupported LLM provider: {config.provider}")
