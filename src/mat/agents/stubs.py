"""Stub implementations of MAT agents for thin-slice testing."""

from __future__ import annotations

import asyncio

from mat.models.contracts import (
    Baseline,
    DeterministicGateResult,
    Implementation,
    Plan,
    ReviewResult,
    TestResult,
    TestScenario,
    Verdict,
)


class AnalystStub:
    def __init__(self, baseline_passed: int = 5, coverage_pct: float = 80.0) -> None:
        self.baseline_passed = baseline_passed
        self.coverage_pct = coverage_pct

    async def analyze(
        self, run_id: str, plan_index: int, task_description: str
    ) -> tuple[Plan, Baseline]:
        plan = Plan(
            id=f"plan_{run_id}_v{plan_index}",
            run_id=run_id,
            plan_index=plan_index,
            approach="Implementar feature com cobertura de testes",
            risk_level="low",
            impacted_files=["src/calculator.py", "tests/test_calculator.py"],
            test_scenarios=[
                TestScenario(
                    name="happy_path_addition",
                    type="positive",
                    description="Testa soma com inteiros",
                )
            ],
        )
        baseline = Baseline(
            id=f"base_{run_id}",
            run_id=run_id,
            pytest_passed=self.baseline_passed,
            pytest_total=self.baseline_passed,
            coverage_pct=self.coverage_pct,
            ruff_clean=True,
            raw_output="5 passed in 0.05s",
        )
        return plan, baseline


class ProgrammerStub:
    def __init__(self, should_fail: bool = False) -> None:
        self.should_fail = should_fail

    async def implement(self, plan: Plan, feedback: str = "") -> Implementation:
        await asyncio.sleep(0.01)
        if self.should_fail:
            return Implementation(
                modified_files=[],
                test_files_added=[],
                diff="",
            )
        return Implementation(
            modified_files=["src/calculator.py"],
            test_files_added=["tests/test_calculator.py"],
            diff="+ def add(a, b): return a + b\n+ def test_add(): assert add(2, 3) == 5",
        )


class ReviewerStub:
    def __init__(self, approve_on_turn: int = 1) -> None:
        self.approve_on_turn = approve_on_turn
        self.current_turn = 0

    async def review(self, plan: Plan, impl: Implementation, task_description: str) -> ReviewResult:
        self.current_turn += 1
        if self.current_turn < self.approve_on_turn:
            return ReviewResult(
                approved=False,
                issues=["Código precisa de docstrings adequadas."],
                meets_task_objective=False,
                feedback="Adicione docstrings e trate tipos.",
            )
        return ReviewResult(
            approved=True,
            issues=[],
            meets_task_objective=True,
            feedback="Código e testes em conformidade.",
        )


class TesterHappyStub:
    __test__ = False

    def __init__(self, passed: bool = True) -> None:
        self.passed = passed

    async def test(self, plan: Plan, impl: Implementation) -> TestResult:
        await asyncio.sleep(0.01)
        return TestResult(
            agent_type="tester_happy",
            passed=self.passed,
            tests_run=6,
            tests_passed=6 if self.passed else 5,
            raw_output="6 passed in 0.06s" if self.passed else "1 failed, 5 passed",
        )


class VerifierStub:
    def __init__(self, approved: bool = True, rationale: str = "Critérios atendidos.") -> None:
        self.approved = approved
        self.rationale = rationale

    async def verify(
        self,
        plan: Plan,
        impl: Implementation,
        gate_res: DeterministicGateResult,
        test_res: TestResult,
        task_description: str,
    ) -> Verdict:
        return Verdict(
            approved=self.approved and gate_res.passed and test_res.passed,
            criteria={
                "gate_passed": gate_res.passed,
                "tester_passed": test_res.passed,
                "objective_met": True,
            },
            evidence_cited=["ev_plan", "ev_test_res"],
            rationale=self.rationale if self.approved else f"Reprovado: {self.rationale}",
        )
