"""Anthropic LLM wrapper with token tracking and error handling."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import anthropic


@dataclass
class LLMResponse:
    content: list[Any]
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    raw_message: Any


class LLMClient:
    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self.client = anthropic.AsyncAnthropic(api_key=key) if key else None

    async def call(
        self,
        model: str,
        messages: list[dict[str, Any]],
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4000,
    ) -> LLMResponse:
        """Call Anthropic messages API and track token usage."""
        if self.client is None:
            raise RuntimeError(
                "ANTHROPIC_API_KEY não configurada. Defina a chave no arquivo .env ou no ambiente."
            )

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools

        response = await self.client.messages.create(**kwargs)

        input_tokens = response.usage.input_tokens if response.usage else 0
        output_tokens = response.usage.output_tokens if response.usage else 0

        return LLMResponse(
            content=response.content,
            stop_reason=response.stop_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            raw_message=response,
        )
