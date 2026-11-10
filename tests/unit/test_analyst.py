"""Unit tests for AnalystAgent."""

from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from mat.agents.analyst import AmbiguousTaskError, AnalystAgent
from mat.agents.llm import LLMClient, LLMResponse
from mat.gate.baseline import BaselineMeasurer
from mat.models.contracts import Baseline
from mat.settings import AgentProfile


@dataclass
class MockContentBlock:
    type: str
    text: str = ""


@pytest.mark.asyncio
async def test_analyst_generates_valid_plan(tmp_path):
    mock_llm = AsyncMock(spec=LLMClient)
    plan_json = {
        "approach": "Criar classe Calculator",
        "risk_level": "low",
        "impacted_files": ["src/calculator.py", "tests/test_calculator.py"],
        "test_scenarios": [{"name": "soma_simples", "type": "positive"}],
        "is_ambiguous": False,
    }
    mock_llm.call.return_value = LLMResponse(
        content=[MockContentBlock(type="text", text=f"```json\n{json.dumps(plan_json)}\n```")],
        stop_reason="end_turn",
        input_tokens=100,
        output_tokens=100,
        raw_message=None,
    )

    mock_measurer = AsyncMock(spec=BaselineMeasurer)
    mock_measurer.measure.return_value = Baseline(
        id="base_1", run_id="run_1", pytest_passed=5, pytest_total=5, coverage_pct=90.0
    )

    profile = AgentProfile(
        model="claude-opus-5-5", temperature=0.2, max_tokens=4000, system_role="Architect"
    )

    analyst = AnalystAgent(
        llm=mock_llm, profile=profile, worktree_path=tmp_path, baseline_measurer=mock_measurer
    )

    plan, baseline = await analyst.analyze("run_1", 1, "Implementar calculadora")
    assert plan.approach == "Criar classe Calculator"
    assert plan.risk_level == "low"
    assert "src/calculator.py" in plan.impacted_files
    assert baseline.pytest_passed == 5


@pytest.mark.asyncio
async def test_analyst_raises_ambiguous_task_error(tmp_path):
    mock_llm = AsyncMock(spec=LLMClient)
    plan_json = {
        "is_ambiguous": True,
        "ambiguity_reason": "Requisitos contraditórios entre endpoint GET e POST.",
    }
    mock_llm.call.return_value = LLMResponse(
        content=[MockContentBlock(type="text", text=json.dumps(plan_json))],
        stop_reason="end_turn",
        input_tokens=50,
        output_tokens=50,
        raw_message=None,
    )

    mock_measurer = AsyncMock(spec=BaselineMeasurer)
    mock_measurer.measure.return_value = Baseline(id="base_2", run_id="run_2")

    profile = AgentProfile(
        model="claude-opus-5-5", temperature=0.2, max_tokens=4000, system_role="Architect"
    )

    analyst = AnalystAgent(
        llm=mock_llm, profile=profile, worktree_path=tmp_path, baseline_measurer=mock_measurer
    )

    with pytest.raises(AmbiguousTaskError) as exc_info:
        await analyst.analyze("run_2", 1, "Fazer algo contraditório")

    assert "Requisitos contraditórios" in str(exc_info.value)
