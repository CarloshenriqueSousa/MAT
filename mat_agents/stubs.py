"""Stub agents for testing the MAT State Machine end-to-end without LLM API calls."""

import asyncio
from dataclasses import dataclass
from typing import Any
from mat_core.state_machine.context import BaselineData, ExecutionPlan, RunContext


@dataclass
class AgentResult:
    success: bool
    summary: str
    data: dict[str, Any]
    tokens_used: int = 150


class AnalystStub:
    """Stub do Analista: gera baseline inicial e plano de execução."""

    def __init__(self, baseline_pytest_passed: int = 5, coverage_pct: float = 80.0):
        self.baseline_pytest_passed = baseline_pytest_passed
        self.coverage_pct = coverage_pct

    async def execute(self, ctx: RunContext) -> AgentResult:
        ctx.baseline = BaselineData(
            pytest_passed=self.baseline_pytest_passed,
            pytest_total=self.baseline_pytest_passed,
            coverage_pct=self.coverage_pct,
            ruff_clean=True,
            raw_output={"baseline_collected": True},
        )
        plan_id = f"plan_{ctx.run_id}_v{ctx.plan_index}"
        ctx.current_plan = ExecutionPlan(
            plan_id=plan_id,
            approach="Abordagem modular com injeção de dependência",
            risk_level="Baixo",
            impacted_files=["src/core.py", "tests/test_core.py"],
            test_scenarios=[
                {"name": "happy_path_basic", "type": "positive"},
                {"name": "bypass_auth_check", "type": "adversarial"},
                {"name": "breaker_overflow", "type": "destructive"},
            ],
        )
        return AgentResult(
            success=True,
            summary=f"Plano {plan_id} gerado com sucesso.",
            data={"plan_id": plan_id, "baseline": ctx.baseline.__dict__},
            tokens_used=200,
        )


class ProgrammerStub:
    """Stub do Programador: gera código e testes no worktree simulado."""

    def __init__(self, should_fail: bool = False):
        self.should_fail = should_fail

    async def execute(self, ctx: RunContext) -> AgentResult:
        if self.should_fail:
            return AgentResult(
                success=False,
                summary="Erro de sintaxe simulado na implementação.",
                data={"error": "SyntaxError in core.py"},
                tokens_used=300,
            )
        return AgentResult(
            success=True,
            summary="Código e testes da feature implementados.",
            data={"modified_files": ["src/core.py", "tests/test_core.py"]},
            tokens_used=450,
        )


class TesterHappyStub:
    """Stub do Tester #1 (Caminho Convencional)."""

    __test__ = False

    def __init__(self, passed: bool = True):
        self.passed = passed

    async def execute(self, ctx: RunContext) -> AgentResult:
        await asyncio.sleep(0.01)  # Simula tempo de execução
        return AgentResult(
            success=self.passed,
            summary="Testes de caminho feliz executados.",
            data={"tests_run": 5, "tests_passed": 5 if self.passed else 4},
            tokens_used=120,
        )


class TesterBypassStub:
    """Stub do Tester #2 (Tentativa de Bypass)."""

    __test__ = False

    def __init__(self, bypass_prevented: bool = True):
        # Se o bypass foi prevenido pelo código, o teste do agente PASSOU
        self.bypass_prevented = bypass_prevented

    async def execute(self, ctx: RunContext) -> AgentResult:
        await asyncio.sleep(0.01)
        return AgentResult(
            success=self.bypass_prevented,
            summary="Tentativa de bypass de autenticação bloqueada pelo sistema.",
            data={"bypass_attempted": "auth_token_tampering", "blocked": self.bypass_prevented},
            tokens_used=140,
        )


class TesterBreakerStub:
    """Stub do Tester #3 (Tentativa de Quebra/Fuzzing)."""

    __test__ = False

    def __init__(self, resilience_passed: bool = True):
        self.resilience_passed = resilience_passed

    async def execute(self, ctx: RunContext) -> AgentResult:
        await asyncio.sleep(0.01)
        return AgentResult(
            success=self.resilience_passed,
            summary="Fuzzing e inputs nulos tratados adequadamente.",
            data={"fuzz_cases": 50, "crashes": 0 if self.resilience_passed else 2},
            tokens_used=160,
        )


class ConsolidatorStub:
    """Stub do Tester #4 (Consolidador): analisa resultados dos testers 1-3."""

    async def execute(
        self,
        ctx: RunContext,
        happy_res: AgentResult,
        bypass_res: AgentResult,
        breaker_res: AgentResult,
    ) -> AgentResult:
        all_passed = happy_res.success and bypass_res.success and breaker_res.success
        summary = (
            "Todos os testes (convencional, bypass e breaker) passaram."
            if all_passed
            else "Foram detectadas falhas entre os testes executados."
        )
        return AgentResult(
            success=all_passed,
            summary=summary,
            data={
                "happy_path": happy_res.data,
                "bypass": bypass_res.data,
                "breaker": breaker_res.data,
                "e2e_passed": all_passed,
            },
            tokens_used=250,
        )


class VerifierStub:
    """Stub do Verificador Final: decide aprovação do gate."""

    def __init__(self, passes_gate: bool = True, reason: str = "Aprovado"):
        self.passes_gate = passes_gate
        self.reason = reason

    async def execute(self, ctx: RunContext, consolidated_res: AgentResult) -> AgentResult:
        is_approved = self.passes_gate and consolidated_res.success
        summary = "Gate APROVADO: Código pronto para release." if is_approved else f"Gate REPROVADO: {self.reason}"
        return AgentResult(
            success=is_approved,
            summary=summary,
            data={
                "approved": is_approved,
                "criteria": {
                    "happy_path_ok": consolidated_res.data.get("happy_path", {}).get("tests_passed", 0) > 0,
                    "bypass_blocked": consolidated_res.data.get("bypass", {}).get("blocked", False),
                    "crashes_zero": consolidated_res.data.get("breaker", {}).get("crashes", 1) == 0,
                },
                "reason": summary,
            },
            tokens_used=180,
        )
