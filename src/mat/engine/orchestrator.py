"""MAT Pipeline Orchestrator.

Orchestrates the 4-phase autonomous development workflow:
1. Analysis (scan + baseline + plan + optional human checkpoint)
2. Programming + Review (Programmer writes code & tests, Reviewer inspects; bounded correction rounds)
3. Testing (Deterministic gate first; Happy Path tester only if the gate passes)
4. Verification (Final release gate evaluating diff, original objective, and raw evidence)

A run paused for plan approval (supervised mode) can be resumed explicitly with
``PipelineOrchestrator.resume`` from its latest checkpoint (D53).

In ``planning`` mode (D59) only the Analyst is real: every run pauses for plan
approval, and approving records the decision without starting any code edit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from mat.agents.errors import AgentExecutionError, AmbiguousTaskError, BaselineFailingError
from mat.db.connection import Database
from mat.db.repos import AttemptsRepo, CheckpointsRepo, EvidenceRepo, RunsRepo, TasksRepo
from mat.engine.budget import AttemptBudget
from mat.engine.state_machine import (
    AttemptStatus,
    ExecutionMode,
    Phase,
    RunStatus,
    assert_valid_transition,
)
from mat.models.contracts import (
    Analysis,
    Baseline,
    DeterministicGateResult,
    Implementation,
    Plan,
    ReviewResult,
    TestResult,
    Verdict,
    Workspace,
)
from mat.settings import AppConfig

logger = logging.getLogger(__name__)

AgentResult = TypeVar("AgentResult", bound=BaseModel)

SNAPSHOT_VERSION = 1
PAUSE_PLAN_APPROVAL = "plan_approval"
PAUSE_EXECUTION_UNAVAILABLE = "execution_unavailable"


class AgentCallFailure(Exception):
    def __init__(
        self,
        run_id: str,
        attempt_id: str | None,
        agent_type: str,
        reason: str,
    ) -> None:
        super().__init__(reason)
        self.run_id = run_id
        self.attempt_id = attempt_id
        self.agent_type = agent_type


EVIDENCE_OUTPUT_TAIL = 4000


def _tail(text: str, limit: int = EVIDENCE_OUTPUT_TAIL) -> str:
    return text if len(text) <= limit else "…" + text[-limit:]


def _baseline_failure_content(error: BaselineFailingError) -> dict[str, Any]:
    baseline = error.baseline
    return {
        "reason": str(error),
        "reasons": error.reasons,
        "pytest_exit_code": baseline.pytest_exit_code,
        "pytest_timed_out": baseline.pytest_timed_out,
        "pytest_passed": baseline.pytest_passed,
        "pytest_total": baseline.pytest_total,
        "ruff_clean": baseline.ruff_clean,
        "pytest_output_tail": _tail(baseline.raw_output),
        "ruff_output_tail": _tail(baseline.ruff_output),
    }


class ResumeError(Exception):
    """A run não pode ser retomada: inexistente, não pausada ou pausa não retomável."""


@dataclass
class PipelineState:
    """Tudo o que o laço precisa para continuar; serializado em cada checkpoint."""

    task_description: str
    supervised: bool
    phase: Phase = Phase.ANALYSIS
    plan_index: int = 1
    attempt_index: int = 1
    plan: Plan | None = None
    baseline: Baseline | None = None
    feedback: str = ""
    pause_reason: str | None = None

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "version": SNAPSHOT_VERSION,
            "task_description": self.task_description,
            "supervised": self.supervised,
            "phase": self.phase.value,
            "plan_index": self.plan_index,
            "attempt_index": self.attempt_index,
            "current_plan_id": self.plan.id if self.plan else None,
            "plan": self.plan.model_dump(mode="json") if self.plan else None,
            "baseline": self.baseline.model_dump(mode="json") if self.baseline else None,
            "feedback": self.feedback,
            "pause_reason": self.pause_reason,
        }

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> PipelineState:
        if snapshot.get("version") != SNAPSHOT_VERSION:
            raise ResumeError(
                f"Checkpoint em formato desconhecido (versão {snapshot.get('version')!r})."
            )
        try:
            plan = snapshot.get("plan")
            baseline = snapshot.get("baseline")
            return cls(
                task_description=str(snapshot["task_description"]),
                supervised=bool(snapshot["supervised"]),
                phase=Phase(snapshot["phase"]),
                plan_index=int(snapshot["plan_index"]),
                attempt_index=int(snapshot["attempt_index"]),
                plan=Plan.model_validate(plan) if plan is not None else None,
                baseline=Baseline.model_validate(baseline) if baseline is not None else None,
                feedback=str(snapshot.get("feedback", "")),
                pause_reason=snapshot.get("pause_reason"),
            )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise ResumeError(f"Checkpoint inválido: {exc}") from exc


# Protocolos de agentes para desacoplar implementações reais de stubs
class AnalystAgent(Protocol):
    async def analyze(
        self, run_id: str, plan_index: int, task_description: str
    ) -> tuple[Plan, Baseline]: ...


class ProgrammerAgent(Protocol):
    async def implement(
        self,
        plan: Plan,
        *,
        task_description: str,
        feedback: str = "",
        budget: AttemptBudget | None = None,
    ) -> Implementation: ...


class ReviewerAgent(Protocol):
    async def review(
        self,
        plan: Plan,
        impl: Implementation,
        task_description: str,
        *,
        budget: AttemptBudget | None = None,
    ) -> ReviewResult: ...


class GateEvaluator(Protocol):
    async def check(self, baseline: Baseline, impl: Implementation) -> DeterministicGateResult: ...


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
        *,
        gate: GateEvaluator,
        supervised: bool = False,
        execution_mode: ExecutionMode = ExecutionMode.UNKNOWN,
    ) -> None:
        self.config = config
        self.db = db
        self.analyst = analyst
        self.programmer = programmer
        self.reviewer = reviewer
        self.tester_happy = tester_happy
        self.verifier = verifier
        self.gate = gate
        # Sem aprovação humana do plano, nada segue para a edição (D41/D59).
        self.supervised = supervised or execution_mode == ExecutionMode.PLANNING
        self.execution_mode = execution_mode

        # Repositórios
        self.runs = RunsRepo(db)
        self.tasks = TasksRepo(db)
        self.attempts = AttemptsRepo(db)
        self.evidence = EvidenceRepo(db)
        self.checkpoints = CheckpointsRepo(db)

    async def run(
        self,
        task_description: str,
        run_id: str | None = None,
        base_sha: str | None = None,
        workspace: Workspace | None = None,
    ) -> RunStatus:
        """Executa a esteira completa até PASSED, FAILED ou AWAITING_HUMAN."""
        base_sha = self._check_base(base_sha, workspace)
        if self.execution_mode in (ExecutionMode.REAL, ExecutionMode.PLANNING) and (
            base_sha is None or not re.fullmatch(r"[0-9a-f]{40,64}", base_sha)
        ):
            raise ValueError("Execuções reais exigem base_sha completo e imutável.")
        if self.execution_mode == ExecutionMode.PLANNING and (
            workspace is None or workspace.path is None
        ):
            raise ValueError("O modo planning exige um worktree criado para a run.")
        run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        state = PipelineState(task_description=task_description, supervised=self.supervised)

        async def start() -> RunStatus:
            await self.runs.create(
                run_id=run_id,
                status=RunStatus.RUNNING.value,
                phase=Phase.ANALYSIS.value,
                execution_mode=self.execution_mode.value,
                base_sha=base_sha,
                workspace=workspace,
            )
            return await self._drive(run_id, state)

        return await self._guarded(run_id, start)

    async def pause_before_start(
        self,
        task_description: str,
        *,
        run_id: str,
        predicate: str,
        reason: str,
        details: dict[str, Any] | None = None,
        base_sha: str | None = None,
        workspace: Workspace | None = None,
    ) -> RunStatus:
        """Registra uma run que não pode começar (ex.: checkout sujo) em awaiting_human.

        Nenhum agente é chamado; a evidência explica o motivo e o que fazer.
        """
        base_sha = self._check_base(base_sha, workspace)
        await self.runs.create(
            run_id=run_id,
            status=RunStatus.AWAITING_HUMAN.value,
            phase=Phase.AWAITING_HUMAN.value,
            execution_mode=self.execution_mode.value,
            base_sha=base_sha,
            workspace=workspace,
        )
        await self.evidence.record_evidence(
            run_id=run_id,
            agent_type="orchestrator",
            predicate=predicate,
            content={"reason": reason, "task_description": task_description, **(details or {})},
        )
        logger.warning("Run %s pausada antes de começar: %s", run_id, reason)
        return RunStatus.AWAITING_HUMAN

    @staticmethod
    def _check_base(base_sha: str | None, workspace: Workspace | None) -> str | None:
        if workspace is None:
            return base_sha
        if base_sha is not None and base_sha != workspace.base_sha:
            raise ValueError("base_sha diverge do commit-base do workspace.")
        return workspace.base_sha

    async def resume(self, run_id: str, *, approve: bool = True, reason: str = "") -> RunStatus:
        """Retoma uma run pausada para aprovação do plano.

        ``approve=True`` segue para a programação a partir do checkpoint salvo;
        ``approve=False`` encerra a run como CANCELLED. Qualquer outra pausa
        (falha de agente, análise ambígua) não é retomável aqui e gera ResumeError.
        """
        run = await self.runs.get(run_id)
        if run is None:
            raise ResumeError(f"Run '{run_id}' não encontrada.")
        if run["status"] != RunStatus.AWAITING_HUMAN.value:
            raise ResumeError(
                f"Run '{run_id}' está em '{run['status']}', não em '{RunStatus.AWAITING_HUMAN.value}'."
            )
        if run["execution_mode"] != self.execution_mode.value:
            raise ResumeError(
                f"Run '{run_id}' foi criada em modo '{run['execution_mode']}' e não pode ser "
                f"retomada em modo '{self.execution_mode.value}'."
            )

        checkpoint = await self.checkpoints.get_latest_checkpoint(run_id)
        state: PipelineState | None = None
        if checkpoint is not None and checkpoint["phase"] == Phase.AWAITING_HUMAN.value:
            try:
                snapshot = json.loads(checkpoint["state_snapshot"])
            except json.JSONDecodeError as exc:
                raise ResumeError(f"Checkpoint ilegível: {exc}") from exc
            if not isinstance(snapshot, dict):
                raise ResumeError("Checkpoint ilegível: snapshot não é um objeto.")
            state = PipelineState.from_snapshot(snapshot)
        if (
            state is None
            or state.pause_reason != PAUSE_PLAN_APPROVAL
            or state.plan is None
            or state.baseline is None
        ):
            raise ResumeError(
                f"A pausa da run '{run_id}' não é uma aprovação de plano; ela exige "
                "intervenção humana e ainda não pode ser retomada pela CLI."
            )

        if not approve:
            if not await self.runs.transition_status(
                run_id,
                RunStatus.AWAITING_HUMAN.value,
                RunStatus.CANCELLED.value,
                Phase.AWAITING_HUMAN.value,
            ):
                raise ResumeError(
                    f"Run '{run_id}' já foi retomada ou encerrada por outro processo."
                )
            await self.evidence.record_evidence(
                run_id=run_id,
                agent_type="human",
                predicate="plan_rejected",
                content={"plan_id": state.plan.id, "reason": reason},
            )
            logger.info("Plano %s rejeitado; run %s cancelada.", state.plan.id, run_id)
            return RunStatus.CANCELLED

        if self.execution_mode == ExecutionMode.PLANNING:
            # Reivindica a run para impedir duas aprovações concorrentes (D53).
            if not await self.runs.transition_status(
                run_id,
                RunStatus.AWAITING_HUMAN.value,
                RunStatus.RUNNING.value,
                Phase.AWAITING_HUMAN.value,
            ):
                raise ResumeError(
                    f"Run '{run_id}' já foi retomada ou encerrada por outro processo."
                )
            return await self._guarded(
                run_id, partial(self._approve_without_execution, run_id, state, reason)
            )

        assert_valid_transition(Phase.AWAITING_HUMAN, Phase.PROGRAMMING)
        if not await self.runs.transition_status(
            run_id,
            RunStatus.AWAITING_HUMAN.value,
            RunStatus.RUNNING.value,
            Phase.PROGRAMMING.value,
        ):
            raise ResumeError(f"Run '{run_id}' já foi retomada ou encerrada por outro processo.")
        await self.evidence.record_evidence(
            run_id=run_id,
            agent_type="human",
            predicate="plan_approved",
            content={"plan_id": state.plan.id, "plan_index": state.plan_index, "reason": reason},
        )
        state.phase = Phase.PROGRAMMING
        state.attempt_index = 1
        state.pause_reason = None
        logger.info("Plano %s aprovado; retomando a run %s.", state.plan.id, run_id)
        return await self._guarded(run_id, partial(self._drive, run_id, state))

    async def _approve_without_execution(
        self, run_id: str, state: PipelineState, reason: str
    ) -> RunStatus:
        """Registra a aprovação de um plano ``planning`` sem iniciar a programação (D59).

        A execução real ainda não está ligada; a run continua em ``awaiting_human``
        com uma pausa não retomável, em vez de seguir com agentes simulados. A run já
        foi reivindicada (``running``) por ``resume``.
        """
        assert state.plan is not None
        await self.evidence.record_evidence(
            run_id=run_id,
            agent_type="human",
            predicate="plan_approved",
            content={"plan_id": state.plan.id, "plan_index": state.plan_index, "reason": reason},
        )
        state.pause_reason = PAUSE_EXECUTION_UNAVAILABLE
        await self.checkpoints.save_checkpoint(
            run_id=run_id, phase=Phase.AWAITING_HUMAN.value, snapshot=state.to_snapshot()
        )
        await self.evidence.record_evidence(
            run_id=run_id,
            agent_type="orchestrator",
            predicate="execution_unavailable",
            content={
                "plan_id": state.plan.id,
                "reason": "Execução real ainda não disponível no modo planning; "
                "nenhuma edição foi feita no worktree.",
            },
        )
        await self.runs.update_status(
            run_id, RunStatus.AWAITING_HUMAN.value, Phase.AWAITING_HUMAN.value
        )
        logger.info("Plano %s aprovado; execução indisponível no modo planning.", state.plan.id)
        return RunStatus.AWAITING_HUMAN

    async def _guarded(self, run_id: str, body: Callable[[], Awaitable[RunStatus]]) -> RunStatus:
        """Converte falhas em AWAITING_HUMAN/FAILED/CANCELLED sem deixar a run em 'running'."""
        try:
            return await body()
        except AgentCallFailure as failure:
            reason = f"Falha no agente {failure.agent_type} após retries permitidos: {failure}"
            if failure.attempt_id is not None:
                await self.attempts.update_attempt_status(
                    failure.attempt_id, AttemptStatus.ESCALATED.value, reason
                )
            await self.runs.update_status(
                failure.run_id,
                RunStatus.AWAITING_HUMAN.value,
                Phase.AWAITING_HUMAN.value,
            )
            cause = failure.__cause__
            predicate = "agent_failure_escalated"
            content: dict[str, Any] = {"reason": reason, "error_type": type(cause).__name__}
            if isinstance(cause, BaselineFailingError):
                # Base já quebrada antes de editar: pausa com a evidência medida (D43/D60).
                predicate = "baseline_failing"
                content = _baseline_failure_content(cause)
                await self.runs.save_baseline(cause.baseline)
            elif failure.agent_type == "analyst":
                # Plano ambíguo ou saída fora do contrato: pausa, não inventa (D51).
                predicate = (
                    "plan_ambiguous" if isinstance(cause, AmbiguousTaskError) else "analysis_failed"
                )
            await self.evidence.record_evidence(
                run_id=failure.run_id,
                attempt_id=failure.attempt_id,
                agent_type=failure.agent_type,
                predicate=predicate,
                content=content,
            )
            logger.warning("Run %s pausado para intervenção humana: %s", failure.run_id, reason)
            return RunStatus.AWAITING_HUMAN
        except asyncio.CancelledError:
            await self._mark_terminal(run_id, RunStatus.CANCELLED, None, "cancelled")
            raise
        except Exception as exc:
            # Nunca deixar a run presa em 'running' após um erro inesperado.
            await self._mark_terminal(
                run_id, RunStatus.FAILED, Phase.FAILED, f"{type(exc).__name__}: {exc}"
            )
            raise

    async def _mark_terminal(
        self, run_id: str, status: RunStatus, phase: Phase | None, reason: str
    ) -> None:
        """Best-effort: record why a run stopped without masking the original error."""
        try:
            if await self.runs.get(run_id) is None:
                return
            await self.runs.update_status(run_id, status.value, phase.value if phase else None)
            await self.evidence.record_evidence(
                run_id=run_id,
                agent_type="orchestrator",
                predicate=f"run_{status.value}",
                content={"reason": reason},
            )
        except Exception:  # o erro original é o que importa; não mascará-lo
            logger.exception("Não foi possível registrar o término da run %s.", run_id)

    async def _drive(self, run_id: str, state: PipelineState) -> RunStatus:
        """Avança a run a partir de ``state`` até um estado terminal ou de pausa."""
        max_attempts_per_plan = self.config.mat.limits.max_attempts_per_plan

        while state.phase not in (Phase.COMPLETED, Phase.FAILED, Phase.AWAITING_HUMAN):
            # Salva checkpoint a cada iteração de fase
            await self.checkpoints.save_checkpoint(
                run_id=run_id, phase=state.phase.value, snapshot=state.to_snapshot()
            )

            # ── FASE 1: ANÁLISE ───────────────────────────────────────────────
            if state.phase == Phase.ANALYSIS:
                logger.info("Iniciando Fase 1: Análise (Plano %d)", state.plan_index)
                await self.runs.increment_plans(run_id)

                analysis = await self._dispatch_agent(
                    run_id=run_id,
                    attempt_id=None,  # o Analista roda antes de existir tentativa (D54)
                    agent_type="analyst",
                    phase=Phase.ANALYSIS.value,
                    input_payload={
                        "plan_index": state.plan_index,
                        "task_description": state.task_description,
                    },
                    call=partial(self._analyze, run_id, state.plan_index, state.task_description),
                )
                state.plan, state.baseline = analysis.plan, analysis.baseline
                await self.tasks.create_plan(state.plan)
                await self.runs.save_baseline(state.baseline)

                await self.evidence.record_evidence(
                    run_id=run_id,
                    agent_type="analyst",
                    predicate="plan_created",
                    content=state.plan.model_dump(),
                )
                state.attempt_index = 1

                if state.supervised:
                    logger.info(
                        "Modo supervisionado ativo: pausando em awaiting_human para aprovação do plano."
                    )
                    assert_valid_transition(state.phase, Phase.AWAITING_HUMAN)
                    state.phase = Phase.AWAITING_HUMAN
                    state.pause_reason = PAUSE_PLAN_APPROVAL
                    # O checkpoint vem antes do status: uma run vista como pausada
                    # sempre tem o estado necessário para ser retomada.
                    await self.checkpoints.save_checkpoint(
                        run_id=run_id, phase=state.phase.value, snapshot=state.to_snapshot()
                    )
                    await self.runs.update_status(
                        run_id, RunStatus.AWAITING_HUMAN.value, state.phase.value
                    )
                    return RunStatus.AWAITING_HUMAN

                assert_valid_transition(state.phase, Phase.PROGRAMMING)
                state.phase = Phase.PROGRAMMING

            # ── LOOP DE TENTATIVAS (PROGRAMMING → TESTING → VERIFICATION) ──────
            elif state.phase == Phase.PROGRAMMING:
                current_plan, baseline = state.plan, state.baseline
                if current_plan is None or baseline is None:
                    raise RuntimeError("Plano/baseline ausentes ao iniciar a programação.")

                attempt_id = f"att_{run_id}_p{state.plan_index}_a{state.attempt_index}"
                budget = AttemptBudget(
                    token_budget=self.config.mat.limits.token_budget_per_attempt,
                    timeout_seconds=self.config.mat.limits.timeout_seconds_per_attempt,
                )

                await self.runs.increment_attempts(run_id)
                await self.attempts.create_attempt(
                    attempt_id=attempt_id,
                    run_id=run_id,
                    plan_id=current_plan.id,
                    attempt_index=state.attempt_index,
                    token_budget=budget.token_budget,
                )

                # ── FASE 2: PROGRAMADOR + CORRETOR ────────────────────────────
                logger.info(
                    "Iniciando Fase 2: Programação (Attempt %d/%d)",
                    state.attempt_index,
                    max_attempts_per_plan,
                )
                impl, review = await self._run_programming_and_review(
                    run_id=run_id,
                    attempt_id=attempt_id,
                    current_plan=current_plan,
                    task_description=state.task_description,
                    feedback=state.feedback,
                    budget=budget,
                )

                if not review.approved:
                    reason = f"Corretor não aprovou após as rodadas permitidas: {review.issues}"
                    await self._fail_attempt(run_id, attempt_id, budget, state, reason)
                    continue

                assert_valid_transition(state.phase, Phase.TESTING)
                state.phase = Phase.TESTING

                # ── FASE 3: TESTES (Gate Determinístico + Tester Convencional) ──
                logger.info("Iniciando Fase 3: Testes")
                gate_res = await self._dispatch_agent(
                    run_id=run_id,
                    attempt_id=attempt_id,
                    agent_type="gate",
                    phase=Phase.TESTING.value,
                    input_payload={
                        "baseline": baseline.model_dump(),
                        "modified_files": impl.modified_files,
                    },
                    call=partial(self.gate.check, baseline, impl),
                )

                if not gate_res.passed:
                    # Falha determinística sumária: reprova sem chamar Tester nem Verificador.
                    reason = f"Gate determinístico falhou: {gate_res.error_message}"
                    await self._fail_attempt(run_id, attempt_id, budget, state, reason)
                    continue

                test_res = await self._dispatch_agent(
                    run_id=run_id,
                    attempt_id=attempt_id,
                    agent_type="tester_happy",
                    phase=Phase.TESTING.value,
                    input_payload={
                        "plan": current_plan.model_dump(),
                        "implementation": impl.model_dump(),
                    },
                    call=partial(self.tester_happy.test, current_plan, impl),
                )

                assert_valid_transition(state.phase, Phase.VERIFICATION)
                state.phase = Phase.VERIFICATION

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
                        "task_description": state.task_description,
                    },
                    call=partial(
                        self.verifier.verify,
                        plan=current_plan,
                        impl=impl,
                        gate_res=gate_res,
                        test_res=test_res,
                        task_description=state.task_description,
                    ),
                )

                if verdict.approved:
                    logger.info("Gate Aprovado! Task concluída com sucesso.")
                    await self.attempts.update_attempt_status(
                        attempt_id,
                        AttemptStatus.PASSED.value,
                        tokens_used=budget.tokens_used,
                        duration_ms=budget.elapsed_ms,
                    )
                    await self.runs.update_status(
                        run_id, RunStatus.PASSED.value, Phase.COMPLETED.value
                    )
                    state.phase = Phase.COMPLETED
                    return RunStatus.PASSED

                reason = f"Verificador reprovou: {verdict.rationale}"
                await self._fail_attempt(run_id, attempt_id, budget, state, reason)

        final_status = RunStatus.PASSED if state.phase == Phase.COMPLETED else RunStatus.FAILED
        await self.runs.update_status(run_id, final_status.value, state.phase.value)
        return final_status

    async def _fail_attempt(
        self,
        run_id: str,
        attempt_id: str,
        budget: AttemptBudget,
        state: PipelineState,
        reason: str,
    ) -> None:
        """Persist a failed attempt (with real usage) and advance ``state`` to the next phase."""
        await self.attempts.update_attempt_status(
            attempt_id,
            AttemptStatus.FAILED.value,
            reason,
            tokens_used=budget.tokens_used,
            duration_ms=budget.elapsed_ms,
        )
        (
            state.phase,
            state.plan_index,
            state.attempt_index,
            state.feedback,
        ) = await self._handle_attempt_failure(
            run_id, state.plan_index, state.attempt_index, reason
        )

    async def _run_programming_and_review(
        self,
        run_id: str,
        attempt_id: str,
        current_plan: Plan,
        task_description: str,
        feedback: str,
        budget: AttemptBudget,
    ) -> tuple[Implementation, ReviewResult]:
        """Programador -> Corretor, com no máximo `max_review_rounds` correções.

        O orçamento é repassado aos agentes, que registram o consumo real de tokens.
        """
        max_rounds = self.config.mat.limits.max_review_rounds
        review_round = 0
        while True:
            impl = await self._dispatch_agent(
                run_id=run_id,
                attempt_id=attempt_id,
                agent_type="programmer",
                phase=Phase.PROGRAMMING.value,
                input_payload={"plan": current_plan.model_dump(), "feedback": feedback},
                call=partial(
                    self.programmer.implement,
                    current_plan,
                    task_description=task_description,
                    feedback=feedback,
                    budget=budget,
                ),
            )
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
                call=partial(
                    self.reviewer.review, current_plan, impl, task_description, budget=budget
                ),
            )
            if review.approved or review_round >= max_rounds or budget.is_exhausted:
                return impl, review

            review_round += 1
            logger.info(
                "Corretor apontou problemas (rodada %d/%d): %s",
                review_round,
                max_rounds,
                review.issues,
            )
            feedback = f"Corrija os problemas apontados pelo Corretor: {review.issues}"

    async def _analyze(self, run_id: str, plan_index: int, task_description: str) -> Analysis:
        plan, baseline = await self.analyst.analyze(run_id, plan_index, task_description)
        return Analysis(plan=plan, baseline=baseline)

    async def _dispatch_agent(
        self,
        run_id: str,
        attempt_id: str | None,
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
                    if retry_recommended:  # sem retry, a evidência acima já é dispatch_failed
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
            # Limites esgotados (padrão do MVP: 1 x 1; teto configurável: 3 planos x 3 = 9, D55)
            logger.error("Todos os limites de retry e replan foram esgotados. Pipeline falhou.")
            return Phase.FAILED, plan_index, attempt_index, reason
