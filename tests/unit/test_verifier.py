"""Unit tests for VerifierAgent."""

from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from mat.agents.llm import LLMClient, LLMResponse
from mat.agents.verifier import VerifierAgent
from mat.models.contracts import (
    DeterministicGateResult,
    Implementation,
    Plan,
    TestResult,
)
from mat.settings import AgentProfile


@dataclass
class MockContentBlock:
    type: str
    text: str = ""


@pytest.mark.asyncio
async def test_verifier_approves_when_criteria_met():
    mock_llm = AsyncMock(spec=LLMClient)
    verdict_json = {
        "approved": True,
        "criteria": {
            "meets_objective": True,
            "test_coverage_valid": True,
            "no_unintended_changes": True,
            "gate_and_tests_passed": True,
        },
        "evidence_cited": ["ev_diff"],
        "rationale": "Código perfeito e com testes robustos.",
    }
    mock_llm.call.return_value = LLMResponse(
        content=[MockContentBlock(type="text", text=f"```json\n{json.dumps(verdict_json)}\n```")],
        stop_reason="end_turn",
        input_tokens=100,
        output_tokens=100,
        raw_message=None,
    )

    profile = AgentProfile(
        model="claude-opus-5-5", temperature=0.1, max_tokens=4000, system_role="Release authority"
    )
    verifier = VerifierAgent(llm=mock_llm, profile=profile)

    plan = Plan(id="p1", run_id="r1", plan_index=1, approach="Add add()")
    impl = Implementation(diff="+ def add(a, b): return a + b")
    gate_res = DeterministicGateResult(passed=True, pytest_passed=5, coverage_pct=85.0)
    test_res = TestResult(agent_type="tester_happy", passed=True, tests_run=5, tests_passed=5)

    verdict = await verifier.verify(plan, impl, gate_res, test_res, "Adicionar função add")
    assert verdict.approved
    assert verdict.criteria.get("meets_objective") is True


@pytest.mark.asyncio
async def test_verifier_rejects_immediately_if_gate_failed():
    mock_llm = AsyncMock(spec=LLMClient)
    profile = AgentProfile(
        model="claude-opus-5-5", temperature=0.1, max_tokens=4000, system_role="Release authority"
    )
    verifier = VerifierAgent(llm=mock_llm, profile=profile)

    plan = Plan(id="p1", run_id="r1", plan_index=1, approach="Add add()")
    impl = Implementation(diff="+ def add(a, b): return a + b")
    gate_res = DeterministicGateResult(passed=False, error_message="pytest failed")
    test_res = TestResult(agent_type="tester_happy", passed=True)

    verdict = await verifier.verify(plan, impl, gate_res, test_res, "Adicionar função add")
    assert not verdict.approved
    # Nem chama o LLM se o gate determinístico falhou!
    mock_llm.call.assert_not_called()
