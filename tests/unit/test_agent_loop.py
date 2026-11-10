"""Unit tests for the AgentLoop turn-by-turn tool dispatcher."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from mat.agents.llm import LLMClient, LLMResponse
from mat.agents.loop import AgentLoop
from mat.engine.budget import AttemptBudget


@dataclass
class MockContentBlock:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict = None


@pytest.mark.asyncio
async def test_agent_loop_single_turn_no_tools():
    mock_llm = AsyncMock(spec=LLMClient)
    mock_llm.call.return_value = LLMResponse(
        content=[MockContentBlock(type="text", text="Implementação finalizada.")],
        stop_reason="end_turn",
        input_tokens=100,
        output_tokens=50,
        raw_message=None,
    )

    loop = AgentLoop(
        llm=mock_llm,
        model="claude-opus-5-5",
        system_prompt="Você é um assistente.",
        tools=[],
        tool_dispatchers={},
    )

    result = await loop.run("Faça x")
    assert result.turns_executed == 1
    assert result.total_tokens_used == 150
    assert result.final_text == "Implementação finalizada."


@pytest.mark.asyncio
async def test_agent_loop_dispatches_tool_then_completes():
    mock_llm = AsyncMock(spec=LLMClient)

    # Turno 1: LLM pede tool_use
    turn_1_resp = LLMResponse(
        content=[
            MockContentBlock(
                type="tool_use",
                id="call_1",
                name="my_tool",
                input={"arg": "hello"},
            )
        ],
        stop_reason="tool_use",
        input_tokens=100,
        output_tokens=30,
        raw_message=None,
    )

    # Turno 2: LLM recebe tool_result e encerra
    turn_2_resp = LLMResponse(
        content=[MockContentBlock(type="text", text="Ferramenta executada com sucesso.")],
        stop_reason="end_turn",
        input_tokens=150,
        output_tokens=40,
        raw_message=None,
    )

    mock_llm.call.side_effect = [turn_1_resp, turn_2_resp]

    # Handler mock da ferramenta
    mock_tool = AsyncMock(return_value="world")

    loop = AgentLoop(
        llm=mock_llm,
        model="claude-opus-5-5",
        system_prompt="Prompt",
        tools=[{"name": "my_tool"}],
        tool_dispatchers={"my_tool": mock_tool},
    )

    budget = AttemptBudget(token_budget=1000)
    result = await loop.run("Execute my_tool", budget=budget)

    assert result.turns_executed == 2
    assert mock_tool.call_count == 1
    mock_tool.assert_called_once_with(arg="hello")
    assert result.final_text == "Ferramenta executada com sucesso."
    assert budget.tokens_used == 320
