"""MAT Pipeline Orchestrator.

Orchestrates the 4-phase autonomous development workflow:
1. Analysis (scan + baseline + plan + optional human checkpoint)
2. Programming + Review (Programmer writes code & tests, Reviewer inspects; loop bounded by token budget)
3. Testing (Deterministic gate + Conventional Happy Path tester in Docker)
4. Verification (Final release gate evaluating diff, original objective, and raw evidence)
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Protocol, TypeVar

from pydantic import BaseModel

from mat.agents.errors import AgentExecutionError
from mat.db.connection import Database
from mat.db.repos import AttemptsRepo, CheckpointsRepo, EvidenceRepo, RunsRepo, TasksRepo
from mat.engine.budget import AttemptBudget
from mat.engine.state_machine import AttemptStatus, Phase, RunStatus, assert_valid_transition
from mat.models.contracts import (
    Baseline,
    DeterministicGateResult,
    Implementation,
    Plan,
    ReviewResult,
    TestResult,
    Verdict,
)
from mat.settings import AppConfig

logger = logging.getLogger(__name__)

AgentResult = TypeVar("AgentResult", bound=BaseModel)


class AgentCallFailure(Exception):
    def __init__(
        self,
        run_id: str,
        attempt_id: str,
        agent_type: str,
        reason: str,
    ) -> None:
        super().__init__(reason)
        self.run_id = run_id
        self.attempt_id = attempt_id
        self.agent_type = agent_type


# Protocolos de agentes para desacoplar implementações reais de stubs
class AnalystAgent(Protocol):
    async def analyze(
        self, run_id: str, plan_index: int, task_description: str
    ) -> tuple[Plan, Baseline]: ...


class ProgrammerAgent(Protocol):
    async def implement(self, plan: Plan, feedback: str = "") -> Implementation: ...


class ReviewerAgent(Protocol):
    async def review(
        self, plan: Plan, impl: Implementation, task_description: str
    ) -> ReviewResult: ...


class TesterAgent(Protocol):
    async def test(self, plan: Plan, impl: Implementation) -> TestResult: ...


class VerifierAgent(Protocol):
    async def verify(
        self,
        plan: Plan,
        impl: Implementation,
        gate_res: DeterministicGateResult,
        test_res: TestResult,
        task_description: str,
    ) -> Verdict: ...


class PipelineOrchestrator:
    def __init__(
        self,
        config: AppConfig,
        db: Database,
        analyst: AnalystAgent,
        programmer: ProgrammerAgent,
        reviewer: ReviewerAgent,
        tester_happy: TesterAgent,
        verifier: VerifierAgent,
        supervised: bool = False,
    ) -> None:
        self.config = config
        self.db = db
        self.analyst = analyst
        self.programmer = programmer
        self.reviewer = reviewer
        self.tester_happy = tester_happy
        self.verifier = verifier
        self.supervised = supervised

        # Repositórios
        self.runs = RunsRepo(db)
        self.tasks = TasksRepo(db)
        self.attempts = AttemptsRepo(db)
        self.evidence = EvidenceRepo(db)
        self.checkpoints = CheckpointsRepo(db)

    async def run(self, task_description: str, run_id: str | None = None) -> RunStatus:
        """Executa a esteira completa até PASSED, FAILED ou AWAITING_HUMAN."""
        run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        try:
            return await self._run(task_description, run_id)
        except AgentCallFailure as failure:
            reason = f"Falha no agente {failure.agent_type} após retries permitidos: {failure}"
            await self.attempts.update_attempt_status(
                failure.attempt_id, AttemptStatus.ESCALATED.value, reason
            )
            await self.runs.update_status(
                failure.run_id,
                RunStatus.AWAITING_HUMAN.value,
                Phase.AWAITING_HUMAN.value,
            )
            await self.evidence.record_evidence(
                run_id=failure.run_id,
                attempt_id=failure.attempt_id,
                agent_type=failure.agent_type,
                predicate="agent_failure_escalated",
                content={"reason": reason},
            )
            logger.warning("Run %s pausado para intervenção humana: %s", failure.run_id, reason)
            return RunStatus.AWAITING_HUMAN

    async def _run(self, task_description: str, run_id: str) -> RunStatus:
        await self.runs.create(
            run_id=run_id, status=RunStatus.RUNNING.value, phase=Phase.ANALYSIS.value
        )

        current_phase = Phase.ANALYSIS
        plan_index = 1
        current_plan: Plan | None = None
        last_failure_feedback = ""

        max_attempts_per_plan = self.config.mat.limits.max_attempts_per_plan

        while current_phase not in (Phase.COMPLETED, Phase.FAILED, Phase.AWAITING_HUMAN):
            # Salva checkpoint a cada iteração de fase
            await self.checkpoints.save_checkpoint(
                run_id=run_id,
                phase=current_phase.value,
                snapshot={
                    "plan_index": plan_index,
                    "current_plan_id": current_plan.id if current_plan else None,
                },
            )

            # ── FASE 1: ANÁLISE ───────────────────────────────────────────────
            if current_phase == Phase.ANALYSIS:
                logger.info("Iniciando Fase 1: Análise (Plano %d)", plan_index)
                await self.runs.increment_plans(run_id)

                current_plan, baseline = await self.analyst.analyze(
                    run_id, plan_index, task_description
                )
                await self.tasks.create_plan(current_plan)
                await self.runs.save_baseline(baseline)

                await self.evidence.record_evidence(
                    run_id=run_id,
                    agent_type="analyst",
                    predicate="plan_created",
                    content=current_plan.model_dump(),
                )

                if self.supervised:
                    logger.info(
                        "Modo supervisionado ativo: pausando em awaiting_human para aprovação do plano."
                    )
                    current_phase = Phase.AWAITING_HUMAN
                    await self.runs.update_status(
                        run_id, RunStatus.AWAITING_HUMAN.value, current_phase.value
                    )
                    return RunStatus.AWAITING_HUMAN

                assert_valid_transition(current_phase, Phase.PROGRAMMING)
                current_phase = Phase.PROGRAMMING
                attempt_index = 1

            # ── LOOP DE TENTATIVAS (PROGRAMMING → TESTING → VERIFICATION) ──────
            elif current_phase == Phase.PROGRAMMING:
                if current_plan is None:
                    raise RuntimeError("Plano não encontrado para iniciar fase de programação.")

                attempt_id = f"att_{run_id}_p{plan_index}_a{attempt_index}"
                budget = AttemptBudget(
                    token_budget=self.config.mat.limits.token_budget_per_attempt,
                    timeout_seconds=self.config.mat.limits.timeout_seconds_per_attempt,
                )

                await self.runs.increment_attempts(run_id)
                await self.attempts.create_attempt(
                    attempt_id=attempt_id,
                    run_id=run_id,
                    plan_id=current_plan.id,
                    attempt_index=attempt_index,
                    token_budget=budget.token_budget,
                )

                # ── FASE 2: PROGRAMADOR + CORRETOR ────────────────────────────
                logger.info(
                    "Iniciando Fase 2: Programação (Attempt %d/%d)",
                    attempt_index,
                    max_attempts_per_plan,
                )
                impl, review = await self._run_programming_and_review(
                    run_id=run_id,
                    attempt_id=attempt_id,
                    current_plan=current_plan,
                    task_description=task_description,
                    feedback=last_failure_feedback,
                    budget=budget,
                )

                if not review.approved and budget.is_exhausted:
                    reason = f"Budget de tokens esgotado na revisão: {review.issues}"
                    await self.attempts.update_attempt_status(
                        attempt_id, AttemptStatus.FAILED.value, reason
                    )
                    (
                        current_phase,
                        plan_index,
                        attempt_index,
                        last_failure_feedback,
                    ) = await self._handle_attempt_failure(
                        run_id, plan_index, attempt_index, reason
                    )
                    continue

                assert_valid_transition(current_phase, Phase.TESTING)
                current_phase = Phase.TESTING

                # ── FASE 3: TESTES (Gate Determinístico + Tester Convencional) ──
                logger.info("Iniciando Fase 3: Testes")
                gate_res, test_res = await self._run_testing_phase(
                    run_id, attempt_id, current_plan, impl
                )

                if not gate_res.passed:
                    # Falha determinística sumária: reprova sem chamar o Verificador LLM!
                    reason = f"Gate determinístico falhou: {gate_res.error_message}"
                    await self.attempts.update_attempt_status(
                        attempt_id, AttemptStatus.FAILED.value, reason
                    )
                    (
                        current_phase,
                        plan_index,
                        attempt_index,
                        last_failure_feedback,
                    ) = await self._handle_attempt_failure(
                        run_id, plan_index, attempt_index, reason
                    )
                    continue

                assert_valid_transition(current_phase, Phase.VERIFICATION)
                current_phase = Phase.VERIFICATION

                # ── FASE 4: VERIFICADOR FINAL ─────────────────────────────────
                logger.info("Iniciando Fase 4: Verificação Final")
                verdict = await self._dispatch_agent(
                    run_id=run_id,
                    attempt_id=attempt_id,
                    agent_type="verifier",
                    phase=Phase.VERIFICATION.value,
                    input_payload={
                        "plan": current_plan.model_dump(),
                        "implementation": impl.model_dump(),
                        "deterministic_gate": gate_res.model_dump(),
                        "test_result": test_res.model_dump(),
                        "task_description": task_description,
                    },
                    call=partial(
                        self.verifier.verify,
                        plan=current_plan,
                        impl=impl,
                        gate_res=gate_res,
                        test_res=test_res,
                        task_description=task_description,
                    ),
                )

                if verdict.approved:
                    logger.info("Gate Aprovado! Task concluída com sucesso.")
                    await self.attempts.update_attempt_status(
                        attempt_id, AttemptStatus.PASSED.value
                    )
                    await self.runs.update_status(
                        run_id, RunStatus.PASSED.value, Phase.COMPLETED.value
                    )
                    current_phase = Phase.COMPLETED
                    return RunStatus.PASSED
                else:
                    reason = f"Verificador reprovou: {verdict.rationale}"
                    await self.attempts.update_attempt_status(
                        attempt_id, AttemptStatus.FAILED.value, reason
                    )
                    (
                        current_phase,
                        plan_index,
                        attempt_index,
                        last_failure_feedback,
                    ) = await self._handle_attempt_failure(
                        run_id, plan_index, attempt_index, reason
                    )

        final_status = RunStatus.PASSED if current_phase == Phase.COMPLETED else RunStatus.FAILED
        await self.runs.update_status(run_id, final_status.value, current_phase.value)
        return final_status

    async def _run_programming_and_review(
        self,
        run_id: str,
        attempt_id: str,
        current_plan: Plan,
        task_description: str,
        feedback: str,
        budget: AttemptBudget,
    ) -> tuple[Implementation, ReviewResult]:
        """Loop Programador ↔ Corretor delimitado pelo teto de tokens."""
        impl = await self._dispatch_agent(
            run_id=run_id,
            attempt_id=attempt_id,
            agent_type="programmer",
            phase=Phase.PROGRAMMING.value,
            input_payload={"plan": current_plan.model_dump(), "feedback": feedback},
            call=lambda: self.programmer.implement(current_plan, feedback),
        )
        budget.record_tokens(1000)  # Simulado / real registrado pelo agente

        review = await self._dispatch_agent(
            run_id=run_id,
            attempt_id=attempt_id,
            agent_type="reviewer",
            phase=Phase.PROGRAMMING.value,
            input_payload={
                "plan": current_plan.model_dump(),
                "implementation": impl.model_dump(),
                "task_description": task_description,
            },
            call=lambda: self.reviewer.review(current_plan, impl, task_description),
        )
        budget.record_tokens(500)

        # Se o Corretor encontrar problemas e houver budget, o Programador corrige
        while not review.approved and not budget.is_exhausted:
            logger.info("Corretor apontou problemas: %s. Programador corrigindo...", review.issues)
            correction_feedback = f"Corrija os problemas: {review.issues}"
            impl = await self._dispatch_agent(
                run_id=run_id,
                attempt_id=attempt_id,
                agent_type="programmer",
                phase=Phase.PROGRAMMING.value,
                input_payload={
                    "plan": current_plan.model_dump(),
                    "feedback": correction_feedback,
                },
                call=lambda correction_feedback=correction_feedback: self.programmer.implement(
                    current_plan, feedback=correction_feedback
                ),
            )
            budget.record_tokens(800)

            review = await self._dispatch_agent(
                run_id=run_id,
                attempt_id=attempt_id,
                agent_type="reviewer",
                phase=Phase.PROGRAMMING.value,
                input_payload={
                    "plan": current_plan.model_dump(),
                    "implementation": impl.model_dump(),
                    "task_description": task_description,
                },
                call=lambda impl=impl: self.reviewer.review(current_plan, impl, task_description),
            )
            budget.record_tokens(500)

        return impl, review

    async def _run_testing_phase(
        self, run_id: str, attempt_id: str, current_plan: Plan, impl: Implementation
    ) -> tuple[DeterministicGateResult, TestResult]:
        """Executa validação determinística seguida pelo Tester Convencional."""
        # Stub/Wrapper que simula checagem determinística
        gate_res = DeterministicGateResult(
            passed=True, pytest_passed=5, pytest_total=5, coverage_pct=85.0, ruff_clean=True
        )

        test_res = await self._dispatch_agent(
            run_id=run_id,
            attempt_id=attempt_id,
            agent_type="tester_happy",
            phase=Phase.TESTING.value,
            input_payload={
                "plan": current_plan.model_dump(),
                "implementation": impl.model_dump(),
            },
            call=lambda: self.tester_happy.test(current_plan, impl),
        )
        return gate_res, test_res

    async def _dispatch_agent(
        self,
        run_id: str,
        attempt_id: str,
        agent_type: str,
        phase: str,
        input_payload: dict[str, object],
        call: Callable[[], Awaitable[AgentResult]],
    ) -> AgentResult:
        """Run one agent call and persist its task, dispatch, and evidence."""
        dispatch_id = f"dsp_{uuid.uuid4().hex}"
        task_id = f"task_{dispatch_id}"
        await self.tasks.create_task(
            task_id=task_id,
            run_id=run_id,
            agent_type=agent_type,
            phase=phase,
            spec=dispatch_id,
        )
        await self.tasks.update_task_status(task_id, "running")
        await self.attempts.create_dispatch(
            dispatch_id=dispatch_id,
            attempt_id=attempt_id,
            task_id=task_id,
            agent_type=agent_type,
            input_payload=input_payload,
        )

        retry_count = 0
        while True:
            try:
                result = await call()
            except Exception as exc:
                retry_recommended = isinstance(exc, AgentExecutionError) and exc.retry_recommended
                failure = {
                    "dispatch_id": dispatch_id,
                    "retry_index": retry_count,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "retry_recommended": retry_recommended,
                }
                predicate = "dispatch_retry_recommended" if retry_recommended else "dispatch_failed"
                await self.evidence.record_evidence(
                    run_id=run_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    agent_type=agent_type,
                    predicate=predicate,
                    content=failure,
                )

                if (
                    not retry_recommended
                    or retry_count >= self.config.mat.limits.max_dispatch_retries
                ):
                    await self.evidence.record_evidence(
                        run_id=run_id,
                        task_id=task_id,
                        attempt_id=attempt_id,
                        agent_type=agent_type,
                        predicate="dispatch_failed",
                        content=failure,
                    )
                    await self.attempts.complete_dispatch(
                        dispatch_id, "failed", output_payload=failure
                    )
                    await self.tasks.update_task_status(task_id, "failed")
                    raise AgentCallFailure(run_id, attempt_id, agent_type, str(exc)) from exc

                retry_count += 1
                logger.warning(
                    "Falha transitória no dispatch %s (%d/%d); repetindo a chamada.",
                    dispatch_id,
                    retry_count,
                    self.config.mat.limits.max_dispatch_retries,
                )
                continue

            output_payload = result.model_dump(mode="json")
            await self.attempts.complete_dispatch(
                dispatch_id, "done", output_payload=output_payload
            )
            await self.tasks.update_task_status(task_id, "done")
            await self.evidence.record_evidence(
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                agent_type=agent_type,
                predicate="dispatch_completed",
                content={
                    "dispatch_id": dispatch_id,
                    "retry_count": retry_count,
                    "output": output_payload,
                },
            )
            return result

    async def _handle_attempt_failure(
        self, run_id: str, plan_index: int, attempt_index: int, reason: str
    ) -> tuple[Phase, int, int, str]:
        """Controla transições de falha: nova tentativa, replan com Analista ou Fail definitivo."""
        limits = self.config.mat.limits

        if attempt_index < limits.max_attempts_per_plan:
            # Nova tentativa no mesmo plano
            logger.warning(
                "Attempt %d falhou. Iniciando attempt %d.", attempt_index, attempt_index + 1
            )
            return Phase.PROGRAMMING, plan_index, attempt_index + 1, reason
        elif plan_index <= limits.max_replans:
            # Escalação reversa para o Analista!
            logger.warning(
                "Tentativas esgotadas no Plano %d. Escalação reversa para o Analista (Plano %d).",
                plan_index,
                plan_index + 1,
            )
            return Phase.ANALYSIS, plan_index + 1, 1, f"Plano anterior esgotado: {reason}"
        else:
            # Todos os limites esgotados (3 planos x 3 tentativas = 9 tentativas máx)
            logger.error("Todos os limites de retry e replan foram esgotados. Pipeline falhou.")
            return Phase.FAILED, plan_index, attempt_index, reason
