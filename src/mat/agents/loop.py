"""Autonomous agent loop implementing tool-use turn by turn with Anthropic SDK."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mat.agents.llm import LLMClient
from mat.engine.budget import AttemptBudget

logger = logging.getLogger(__name__)


@dataclass
class LoopResult:
    final_text: str
    turns_executed: int
    total_tokens_used: int
    history: list[dict[str, Any]] = field(default_factory=list)


class AgentLoop:
    def __init__(
        self,
        llm: LLMClient,
        model: str,
        system_prompt: str,
        tools: list[dict[str, Any]],
        tool_dispatchers: dict[str, Callable[..., Any]],
        temperature: float = 0.2,
        max_tokens_per_turn: int = 4000,
    ) -> None:
        self.llm = llm
        self.model = model
        self.system_prompt = system_prompt
        self.tools = tools
        self.tool_dispatchers = tool_dispatchers
        self.temperature = temperature
        self.max_tokens_per_turn = max_tokens_per_turn

    async def run(
        self,
        user_prompt: str,
        budget: AttemptBudget | None = None,
        max_turns: int = 30,
    ) -> LoopResult:
        """Executes the autonomous agent turn-by-turn until completion or budget exhaustion."""
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]
        total_tokens = 0
        turn = 0
        final_text = ""

        while turn < max_turns:
            turn += 1

            if budget and budget.is_exhausted:
                logger.warning("Budget esgotado antes do turno %d.", turn)
                break

            response = await self.llm.call(
                model=self.model,
                messages=messages,
                system=self.system_prompt,
                tools=self.tools,
                temperature=self.temperature,
                max_tokens=self.max_tokens_per_turn,
            )

            turn_tokens = response.input_tokens + response.output_tokens
            total_tokens += turn_tokens
            if budget:
                budget.record_tokens(turn_tokens)

            # Extrai blocos de texto e de tool_use
            tool_calls = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
            text_blocks = [
                getattr(b, "text", "") for b in response.content if getattr(b, "type", None) == "text"
            ]
            if text_blocks:
                final_text = "\n".join(text_blocks)

            # Adiciona resposta do assistente ao histórico
            messages.append({"role": "assistant", "content": response.content})

            # Se não houver chamadas de ferramenta, o agente concluiu sua vez
            if not tool_calls or response.stop_reason == "end_turn":
                break

            # Executa ferramentas e monta resposta de tool_result
            tool_results: list[dict[str, Any]] = []
            for tool_call in tool_calls:
                tool_name = tool_call.name
                tool_input = tool_call.input
                tool_id = tool_call.id

                dispatcher = self.tool_dispatchers.get(tool_name)
                if not dispatcher:
                    res_content = f"Erro: ferramenta '{tool_name}' não suportada."
                else:
                    try:
                        # Se for corrotina assíncrona, faz await
                        raw_res = dispatcher(**tool_input)
                        if hasattr(raw_res, "__await__"):
                            res_content = str(await raw_res)
                        else:
                            res_content = str(raw_res)
                    except Exception as exc:  # noqa: BLE001
                        res_content = f"Erro ao executar '{tool_name}': {exc}"

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": res_content,
                    }
                )

            messages.append({"role": "user", "content": tool_results})

        return LoopResult(
            final_text=final_text,
            turns_executed=turn,
            total_tokens_used=total_tokens,
            history=messages,
        )
