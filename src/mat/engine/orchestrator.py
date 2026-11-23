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

In ``implementation`` mode (D61/D62) every agent is real: after plan approval the
Programmer edits the worktree and the change goes through Reviewer, gate, Tester and
Verifier. A validation failure gets the single automatic correction round of D43;
after that the run pauses for guidance (``validation_failed``) instead of failing.
Even an approved change only pauses for final human review (``final_review``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, Field, ValidationError

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
    TestFileChange,
    TestResult,
    Verdict,
    Workspace,
)
from mat.settings import AppConfig
from mat.tools.command_policy import CommandApprovalRequired, CommandPolicy

logger = logging.getLogger(__name__)

AgentResult = TypeVar("AgentResult", bound=BaseModel)

# v2 (D63) acrescenta o progresso da tentativa e as aprovações de testes; v1 continua legível.
SNAPSHOT_VERSION = 2
_READABLE_SNAPSHOT_VERSIONS = (1, 2)
PAUSE_PLAN_APPROVAL = "plan_approval"
PAUSE_EXECUTION_UNAVAILABLE = "execution_unavailable"
# Pausas do modo implementation após a validação (D62). `final_review` é decidida
# com `mat resume` (aceitar) ou `--reject` (recusar) (D65).
PAUSE_FINAL_REVIEW = "final_review"
PAUSE_VALIDATION_FAILED = "validation_failed"
# Testes existentes alterados aguardam aprovação no meio da tentativa (D63); retomável.
PAUSE_TEST_CHANGE_APPROVAL = "test_change_approval"
# Comando fora da política aguarda decisão humana no meio da tentativa (D64); retomável.
PAUSE_COMMAND_APPROVAL = "command_approval"
# Pausas por falha retomáveis com orientação humana (passo 9, D66).
PAUSE_AGENT_FAILURE = "agent_failure_escalated"
PAUSE_PLAN_AMBIGUOUS = "plan_ambiguous"
PAUSE_ANALYSIS_FAILED = "analysis_failed"
PAUSE_BASELINE_FAILING = "baseline_failing"
FAILURE_PAUSES = (
    PAUSE_VALIDATION_FAILED,
    PAUSE_AGENT_FAILURE,
    PAUSE_PLAN_AMBIGUOUS,
    PAUSE_ANALYSIS_FAILED,
)
# Pausas no meio da tentativa: a decisão humana continua a mesma tentativa.
MID_ATTEMPT_PAUSES = (PAUSE_TEST_CHANGE_APPROVAL, PAUSE_COMMAND_APPROVAL)

# Modos que trabalham sobre um worktree real e exigem aprovação do plano (D59/D61).
PROJECT_MODES = (ExecutionMode.PLANNING, ExecutionMode.IMPLEMENTATION)


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


@dataclass
class AttemptOutcome:
    """Resultado de uma tentativa: ``failure`` é ``None`` quando tudo aprovou."""

    impl: Implementation
    failure: str | None
    gate: DeterministicGateResult | None = None
    test: TestResult | None = None
    verdict: Verdict | None = None
    # Comando que interrompeu um agente pedindo aprovação (D64).
    pending_command: CommandApprovalRequired | None = None

    @property
    def pending_test_changes(self) -> list[TestFileChange]:
        return self.gate.pending_test_changes if self.gate is not None else []


class ResumeError(Exception):
    """A run não pode ser retomada: inexistente, não pausada ou pausa não retomável."""


async def cancel_run(db: Database, run_id: str, reason: str = "") -> None:
    """Cancela uma run pausada ou pendente (passo 9, D66).

    Uma run em execução não é interrompida daqui: o MAT não tem como parar outro
    processo com segurança. O worktree e a branch ficam como estão.
    """
    runs = RunsRepo(db)
    run = await runs.get(run_id)
    if run is None:
        raise ResumeError(f"Run '{run_id}' não encontrada.")
    status = run["status"]
    if status not in (RunStatus.AWAITING_HUMAN.value, RunStatus.PENDING.value):
        raise ResumeError(
            f"Run '{run_id}' está em '{status}'; só runs pausadas ou pendentes podem ser "
            "canceladas."
        )
    if not await runs.transition_status(
        run_id, status, RunStatus.CANCELLED.value, run["current_phase"]
    ):
        raise ResumeError(f"Run '{run_id}' mudou de estado durante o cancelamento.")
    await EvidenceRepo(db).record_evidence(
        run_id=run_id,
        agent_type="human",
        predicate="run_cancelled",
        content={"previous_status": status, "reason": reason},
    )


class AttemptProgress(BaseModel):
    """Tentativa interrompida por uma pausa no meio da validação (D63).

    Guarda o necessário para continuar a mesma tentativa: rodadas de correção já
    usadas, consumo de tokens/tempo, a implementação a validar e, se a pessoa recusou
    algo, a falha a devolver ao Programador.
    """

    attempt_id: str
    rounds_used: int = 0
    tokens_used: int = 0
    elapsed_ms: int = 0
    impl: Implementation | None = None
    pending_failure: str | None = None
    pending_test_changes: list[TestFileChange] = Field(default_factory=list)
    # Comando aguardando decisão (D64) e o aviso ao Programador quando a tentativa volta.
    pending_command: str | None = None
    pending_command_reason: str = ""
    resume_note: str | None = None


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
    attempt: AttemptProgress | None = None
    # Caminho do teste existente -> hash da alteração aprovada pela pessoa (D63).
    approved_test_changes: dict[str, str] = field(default_factory=dict)
    # Comandos exatos aprovados ou recusados pela pessoa nesta run (D64).
    approved_commands: list[str] = field(default_factory=list)
    denied_commands: list[str] = field(default_factory=list)
    # Fase em que um agente falhou, para retomar no ponto certo (D66).
    failed_phase: str | None = None

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
            "attempt": self.attempt.model_dump(mode="json") if self.attempt else None,
            "approved_test_changes": dict(self.approved_test_changes),
            "approved_commands": list(self.approved_commands),
            "denied_commands": list(self.denied_commands),
            "failed_phase": self.failed_phase,
        }

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> PipelineState:
        if snapshot.get("version") not in _READABLE_SNAPSHOT_VERSIONS:
            raise ResumeError(
                f"Checkpoint em formato desconhecido (versão {snapshot.get('version')!r})."
            )
        try:
            plan = snapshot.get("plan")
            baseline = snapshot.get("baseline")
            attempt = snapshot.get("attempt")
            approved = snapshot.get("approved_test_changes") or {}
            if not isinstance(approved, dict):
                raise TypeError("approved_test_changes deve ser um objeto.")
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
                attempt=AttemptProgress.model_validate(attempt) if attempt is not None else None,
                approved_test_changes={str(k): str(v) for k, v in approved.items()},
                approved_commands=[str(c) for c in snapshot.get("approved_commands") or []],
                denied_commands=[str(c) for c in snapshot.get("denied_commands") or []],
                failed_phase=snapshot.get("failed_phase"),
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
        command_policy: CommandPolicy | None = None,
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
        self.supervised = supervised or execution_mode in PROJECT_MODES
        self.execution_mode = execution_mode
        # Política de comandos compartilhada com Programador e Corretor (D64).
        self.command_policy = command_policy
        # Estado do laço em andamento: permite gravar o checkpoint de uma pausa por
        # falha de agente, que então pode ser retomada (D66).
        self._active_state: PipelineState | None = None

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
        if self.execution_mode in (ExecutionMode.REAL, *PROJECT_MODES) and (
            base_sha is None or not re.fullmatch(r"[0-9a-f]{40,64}", base_sha)
        ):
            raise ValueError("Execuções reais exigem base_sha completo e imutável.")
        if self.execution_mode in PROJECT_MODES and (
            workspace is None or workspace.path is None
        ):
            raise ValueError(
                f"O modo {self.execution_mode.value} exige um worktree criado para a run."
            )
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

    async def resume(
        self, run_id: str, *, approve: bool = True, reason: str = "", guidance: str = ""
    ) -> RunStatus:
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
            state is not None
            and state.pause_reason == PAUSE_TEST_CHANGE_APPROVAL
            and self.execution_mode in PROJECT_MODES
        ):
            return await self._resume_test_changes(run_id, state, approve=approve, reason=reason)
        if (
            state is not None
            and state.pause_reason == PAUSE_COMMAND_APPROVAL
            and self.execution_mode in PROJECT_MODES
        ):
            return await self._resume_command(run_id, state, approve=approve, reason=reason)
        if state is not None and state.pause_reason == PAUSE_FINAL_REVIEW:
            return await self._decide_delivery(run_id, state, accept=approve, reason=reason)
        if state is not None and state.pause_reason in FAILURE_PAUSES:
            return await self._resume_failure(
                run_id, state, approve=approve, reason=reason, guidance=guidance
            )
        if state is not None and state.pause_reason == PAUSE_BASELINE_FAILING:
            raise ResumeError(
                f"A base da run '{run_id}' já falha: nada foi editado. Corrija a base e inicie "
                "outra run, aceite a proposta de remediação ou cancele com `mat cancel`."
            )
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

    async def _resume_test_changes(
        self, run_id: str, state: PipelineState, *, approve: bool, reason: str
    ) -> RunStatus:
        """Aprova ou recusa testes existentes alterados e continua a mesma tentativa (D63).

        Aprovar vale só para o conteúdo exato mostrado (hash da seção do diff): se o
        Programador mudar o teste de novo, o gate pede outra aprovação. Recusar não
        cancela a run: a recusa vira falha de validação e segue a rodada de correção.
        """
        progress = state.attempt
        if progress is None or not progress.pending_test_changes or state.plan is None:
            raise ResumeError(f"Checkpoint da run '{run_id}' não tem alterações a aprovar.")
        assert_valid_transition(Phase.AWAITING_HUMAN, Phase.PROGRAMMING)
        if not await self.runs.transition_status(
            run_id,
            RunStatus.AWAITING_HUMAN.value,
            RunStatus.RUNNING.value,
            Phase.PROGRAMMING.value,
        ):
            raise ResumeError(f"Run '{run_id}' já foi retomada ou encerrada por outro processo.")

        changes = progress.pending_test_changes
        paths = [change.path for change in changes]
        if approve:
            state.approved_test_changes.update({c.path: c.sha256 for c in changes})
        else:
            progress.pending_failure = (
                "A pessoa recusou a alteração em testes existentes ("
                + ", ".join(paths)
                + "). Reverta essas alterações e implemente sem mudar testes existentes."
                + (f" Motivo: {reason}" if reason else "")
            )
        await self.evidence.record_evidence(
            run_id=run_id,
            attempt_id=progress.attempt_id,
            agent_type="human",
            predicate="test_change_approved" if approve else "test_change_rejected",
            content={"files": paths, "sha256": [c.sha256 for c in changes], "reason": reason},
        )
        progress.pending_test_changes = []
        state.phase = Phase.PROGRAMMING
        state.pause_reason = None
        logger.info(
            "Alteração em testes existentes %s na run %s.",
            "aprovada" if approve else "recusada",
            run_id,
        )
        return await self._guarded(run_id, partial(self._drive, run_id, state))

    async def _resume_failure(
        self,
        run_id: str,
        state: PipelineState,
        *,
        approve: bool,
        reason: str,
        guidance: str,
    ) -> RunStatus:
        """Retoma uma pausa por falha com orientação humana, ou cancela a run (D66).

        Falhas de análise (plano ambíguo, saída inválida, Analista com erro) refazem a
        análise com o esclarecimento somado ao pedido; o novo plano volta a exigir
        aprovação. As demais abrem uma tentativa nova no mesmo plano, com a falha e a
        orientação como feedback; o worktree mantém o que já foi feito. Cada retomada
        é uma autorização humana explícita, então não conta para os limites de D55.
        """
        pause = state.pause_reason
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
                predicate="run_cancelled",
                content={"pause": pause, "reason": reason},
            )
            return RunStatus.CANCELLED

        guidance = guidance.strip()
        if pause == PAUSE_PLAN_AMBIGUOUS and not guidance:
            raise ResumeError(
                "O Analista considerou o pedido ambíguo: retome com `--guidance` explicando "
                "o que deve ser feito."
            )
        reanalyze = (
            state.plan is None
            or state.baseline is None
            or pause in (PAUSE_PLAN_AMBIGUOUS, PAUSE_ANALYSIS_FAILED)
            or state.failed_phase == Phase.ANALYSIS.value
        )
        target = Phase.ANALYSIS if reanalyze else Phase.PROGRAMMING
        assert_valid_transition(Phase.AWAITING_HUMAN, target)
        if not await self.runs.transition_status(
            run_id, RunStatus.AWAITING_HUMAN.value, RunStatus.RUNNING.value, target.value
        ):
            raise ResumeError(f"Run '{run_id}' já foi retomada ou encerrada por outro processo.")
        await self.evidence.record_evidence(
            run_id=run_id,
            agent_type="human",
            predicate="guidance_given",
            content={
                "pause": pause,
                "guidance": guidance,
                "restart_phase": target.value,
                "paused_at_plan": state.plan_index,
                "paused_at_attempt": state.attempt_index,
            },
        )
        if reanalyze:
            if guidance:
                state.task_description = (
                    f"{state.task_description}\n\nEsclarecimento da pessoa: {guidance}"
                )
            if state.plan is not None:
                state.plan_index += 1  # cada plano tem id próprio (plan_<run>_v<n>)
            state.attempt_index = 1
        else:
            previous = state.feedback or "a tentativa anterior não foi concluída"
            state.feedback = f"A tentativa anterior falhou: {previous}" + (
                f"\n\nOrientação da pessoa: {guidance}" if guidance else ""
            )
            state.attempt_index += 1
        state.attempt = None
        state.phase = target
        state.pause_reason = None
        state.failed_phase = None
        logger.info("Run %s retomada após %s (fase %s).", run_id, pause, target.value)
        return await self._guarded(run_id, partial(self._drive, run_id, state))

    async def _decide_delivery(
        self, run_id: str, state: PipelineState, *, accept: bool, reason: str
    ) -> RunStatus:
        """Decisão humana sobre a entrega validada (passo 8, D65).

        Aceitar marca a run ``passed``; recusar, ``cancelled``. Em ambos os casos o
        worktree e a branch ficam como estão: o MAT não faz commit, push nem PR (D40).
        Nenhum agente é chamado.
        """
        target = RunStatus.PASSED if accept else RunStatus.CANCELLED
        phase = Phase.COMPLETED if accept else Phase.AWAITING_HUMAN
        if accept:
            assert_valid_transition(Phase.AWAITING_HUMAN, Phase.COMPLETED)
        if not await self.runs.transition_status(
            run_id, RunStatus.AWAITING_HUMAN.value, target.value, phase.value
        ):
            raise ResumeError(f"Run '{run_id}' já foi retomada ou encerrada por outro processo.")
        await self.evidence.record_evidence(
            run_id=run_id,
            agent_type="human",
            predicate="delivery_accepted" if accept else "delivery_rejected",
            content={"plan_id": state.plan.id if state.plan else None, "reason": reason},
        )
        logger.info("Entrega da run %s %s.", run_id, "aceita" if accept else "recusada")
        return target

    async def _resume_command(
        self, run_id: str, state: PipelineState, *, approve: bool, reason: str
    ) -> RunStatus:
        """Aprova ou recusa o comando pedido e continua a mesma tentativa (D64).

        A decisão vale para o comando exato até o fim da run. O agente interrompido é
        chamado de novo a partir do Programador, com um aviso: o worktree mantém o que
        já foi feito, e a conversa anterior não é retomada.
        """
        progress = state.attempt
        if progress is None or not progress.pending_command or state.plan is None:
            raise ResumeError(f"Checkpoint da run '{run_id}' não tem comando a decidir.")
        assert_valid_transition(Phase.AWAITING_HUMAN, Phase.PROGRAMMING)
        if not await self.runs.transition_status(
            run_id,
            RunStatus.AWAITING_HUMAN.value,
            RunStatus.RUNNING.value,
            Phase.PROGRAMMING.value,
        ):
            raise ResumeError(f"Run '{run_id}' já foi retomada ou encerrada por outro processo.")

        command = progress.pending_command
        (state.approved_commands if approve else state.denied_commands).append(command)
        decision = "aprovou" if approve else "recusou"
        progress.resume_note = (
            f"Sua execução anterior foi interrompida para pedir aprovação do comando "
            f"`{command}`, e a pessoa {decision} esse comando"
            + (f" (motivo: {reason})" if reason else "")
            + ". As alterações que você já fez continuam no worktree; revise-as e continue."
        )
        await self.evidence.record_evidence(
            run_id=run_id,
            attempt_id=progress.attempt_id,
            agent_type="human",
            predicate="command_approved" if approve else "command_rejected",
            content={"command": command, "reason": reason},
        )
        progress.pending_command, progress.pending_command_reason = None, ""
        state.phase = Phase.PROGRAMMING
        state.pause_reason = None
        logger.info("Comando %s na run %s: %s", decision, run_id, command)
        return await self._guarded(run_id, partial(self._drive, run_id, state))

    def _sync_command_policy(self, state: PipelineState) -> None:
        """Aplica à política compartilhada as decisões humanas gravadas na run."""
        if self.command_policy is not None:
            self.command_policy.approved.update(state.approved_commands)
            self.command_policy.denied.update(state.denied_commands)

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
            state = self._active_state
            if state is not None:
                # Checkpoint da pausa: torna a falha retomável com orientação (D66).
                state.failed_phase = state.phase.value
                state.phase = Phase.AWAITING_HUMAN
                state.pause_reason = predicate
                state.attempt = None
                if failure.agent_type != "analyst":
                    state.feedback = _tail(reason)
                await self.checkpoints.save_checkpoint(
                    run_id=failure.run_id, phase=state.phase.value, snapshot=state.to_snapshot()
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

        self._sync_command_policy(state)
        self._active_state = state
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

                budget = AttemptBudget(
                    token_budget=self.config.mat.limits.token_budget_per_attempt,
                    timeout_seconds=self.config.mat.limits.timeout_seconds_per_attempt,
                )
                if state.attempt is not None:
                    # Retomada no meio da tentativa (D63): mesma linha em ``attempts``,
                    # com o consumo anterior somado ao orçamento.
                    attempt_id = state.attempt.attempt_id
                    budget.tokens_used = state.attempt.tokens_used
                    budget.start_time -= state.attempt.elapsed_ms / 1000
                    await self.attempts.update_attempt_status(
                        attempt_id, AttemptStatus.RUNNING.value
                    )
                else:
                    attempt_id = f"att_{run_id}_p{state.plan_index}_a{state.attempt_index}"
                    await self.runs.increment_attempts(run_id)
                    await self.attempts.create_attempt(
                        attempt_id=attempt_id,
                        run_id=run_id,
                        plan_id=current_plan.id,
                        attempt_index=state.attempt_index,
                        token_budget=budget.token_budget,
                    )
                    state.attempt = AttemptProgress(attempt_id=attempt_id)

                # ── FASES 2–4: PROGRAMAÇÃO, TESTES E VERIFICAÇÃO ────────────────
                logger.info(
                    "Iniciando Fase 2: Programação (Attempt %d/%d)",
                    state.attempt_index,
                    max_attempts_per_plan,
                )
                outcome = await self._run_attempt(
                    run_id, attempt_id, state, current_plan, baseline, budget
                )

                if outcome.pending_command is not None:
                    return await self._pause_for_command(
                        run_id, attempt_id, budget, state, current_plan, outcome.pending_command
                    )
                if outcome.pending_test_changes and self.execution_mode in PROJECT_MODES:
                    return await self._pause_for_test_changes(
                        run_id, attempt_id, budget, state, current_plan, outcome
                    )

                state.attempt = None
                if outcome.failure is not None:
                    if not await self._fail_attempt(
                        run_id, attempt_id, budget, state, outcome.failure
                    ):
                        state.feedback = _tail(outcome.failure)
                        return await self._pause(
                            run_id,
                            state,
                            PAUSE_VALIDATION_FAILED,
                            {
                                "reason": _tail(outcome.failure),
                                "attempt_id": attempt_id,
                                "plan_id": current_plan.id,
                                "modified_files": outcome.impl.modified_files,
                            },
                            attempt_id=attempt_id,
                        )
                    continue

                assert outcome.gate and outcome.test and outcome.verdict
                await self.attempts.update_attempt_status(
                    attempt_id,
                    AttemptStatus.PASSED.value,
                    tokens_used=budget.tokens_used,
                    duration_ms=budget.elapsed_ms,
                )
                if self.execution_mode == ExecutionMode.IMPLEMENTATION:
                    # Validação automática aprovada; a entrega ainda depende da
                    # revisão humana (passo 8), então a run não é ``passed`` (D62).
                    return await self._pause(
                        run_id,
                        state,
                        PAUSE_FINAL_REVIEW,
                        {
                            "reason": outcome.verdict.rationale,
                            "attempt_id": attempt_id,
                            "plan_id": current_plan.id,
                            "modified_files": outcome.impl.modified_files,
                            "deterministic_gate": outcome.gate.model_dump(),
                            "tests": {
                                "passed": outcome.test.passed,
                                "tests_run": outcome.test.tests_run,
                                "tests_passed": outcome.test.tests_passed,
                            },
                            "verdict": outcome.verdict.model_dump(),
                        },
                        attempt_id=attempt_id,
                    )

                logger.info("Gate Aprovado! Task concluída com sucesso.")
                await self.runs.update_status(
                    run_id, RunStatus.PASSED.value, Phase.COMPLETED.value
                )
                state.phase = Phase.COMPLETED
                return RunStatus.PASSED

        final_status = RunStatus.PASSED if state.phase == Phase.COMPLETED else RunStatus.FAILED
        await self.runs.update_status(run_id, final_status.value, state.phase.value)
        return final_status

    async def _run_attempt(
        self,
        run_id: str,
        attempt_id: str,
        state: PipelineState,
        current_plan: Plan,
        baseline: Baseline,
        budget: AttemptBudget,
    ) -> AttemptOutcome:
        """Programação, Corretor e validação, com as rodadas de correção de D43/D51.

        ``max_review_rounds`` limita as rodadas de correção automática da tentativa
        inteira: um apontamento do Corretor e uma falha de gate, Tester ou Verificador
        consomem o mesmo orçamento. Esgotado, a falha é devolvida ao chamador.
        """
        max_rounds = self.config.mat.limits.max_review_rounds
        progress = state.attempt or AttemptProgress(attempt_id=attempt_id)
        state.attempt = progress
        feedback = state.feedback
        if progress.resume_note:
            feedback = f"{feedback}\n\n{progress.resume_note}".strip()
            progress.resume_note = None
        # Numa retomada (D63), a implementação já existe ou a pessoa recusou algo.
        impl, failure = progress.impl, progress.pending_failure
        progress.impl, progress.pending_failure, progress.pending_test_changes = None, None, []
        while True:
            if failure is None:
                if impl is None:
                    if state.phase != Phase.PROGRAMMING:
                        assert_valid_transition(state.phase, Phase.PROGRAMMING)
                        state.phase = Phase.PROGRAMMING
                    try:
                        impl, review, progress.rounds_used = (
                            await self._run_programming_and_review(
                                run_id=run_id,
                                attempt_id=attempt_id,
                                current_plan=current_plan,
                                task_description=state.task_description,
                                feedback=feedback,
                                budget=budget,
                                rounds_used=progress.rounds_used,
                            )
                        )
                    except AgentCallFailure as exc:
                        request = exc.__cause__
                        if not isinstance(request, CommandApprovalRequired) or (
                            self.execution_mode not in PROJECT_MODES
                        ):
                            raise
                        return AttemptOutcome(
                            Implementation(),
                            f"Comando requer aprovação: {request.command}",
                            pending_command=request,
                        )
                    if not review.approved:
                        # O laço do Corretor só devolve reprovação com as rodadas esgotadas.
                        return AttemptOutcome(
                            impl,
                            f"Corretor não aprovou após as rodadas permitidas: {review.issues}",
                        )

                outcome = await self._validate(
                    run_id, attempt_id, state, current_plan, baseline, impl
                )
                if outcome.failure is None:
                    return outcome
                if outcome.pending_test_changes and self.execution_mode in PROJECT_MODES:
                    # Pausa para aprovação humana sem gastar a rodada de correção (D63).
                    return outcome
                failure = outcome.failure
            else:
                outcome = AttemptOutcome(impl or Implementation(), failure)

            if progress.rounds_used >= max_rounds or budget.is_exhausted:
                return outcome

            progress.rounds_used += 1
            logger.info(
                "Validação falhou (correção %d/%d): %s", progress.rounds_used, max_rounds, failure
            )
            await self.evidence.record_evidence(
                run_id=run_id,
                attempt_id=attempt_id,
                agent_type="orchestrator",
                predicate="validation_correction",
                content={"round": progress.rounds_used, "reason": _tail(failure)},
            )
            feedback = f"Corrija a falha de validação: {_tail(failure)}"
            impl, failure = None, None

    async def _pause_for_command(
        self,
        run_id: str,
        attempt_id: str,
        budget: AttemptBudget,
        state: PipelineState,
        current_plan: Plan,
        request: CommandApprovalRequired,
    ) -> RunStatus:
        """Pausa a tentativa até a pessoa aprovar ou recusar o comando pedido (D64)."""
        assert state.attempt is not None
        state.attempt.pending_command = request.command
        state.attempt.pending_command_reason = request.reason
        state.attempt.tokens_used = budget.tokens_used
        state.attempt.elapsed_ms = budget.elapsed_ms
        await self.attempts.update_attempt_status(
            attempt_id,
            AttemptStatus.ESCALATED.value,
            "Aguardando aprovação de comando.",
            tokens_used=budget.tokens_used,
            duration_ms=budget.elapsed_ms,
        )
        return await self._pause(
            run_id,
            state,
            PAUSE_COMMAND_APPROVAL,
            {
                "reason": f"Comando fora da política: {request.reason}.",
                "command": request.command,
                "policy_reason": request.reason,
                "attempt_id": attempt_id,
                "plan_id": current_plan.id,
            },
            attempt_id=attempt_id,
        )

    async def _pause_for_test_changes(
        self,
        run_id: str,
        attempt_id: str,
        budget: AttemptBudget,
        state: PipelineState,
        current_plan: Plan,
        outcome: AttemptOutcome,
    ) -> RunStatus:
        """Pausa a tentativa até a pessoa aprovar ou recusar testes existentes alterados."""
        assert state.attempt is not None
        changes = outcome.pending_test_changes
        state.attempt.impl = outcome.impl
        state.attempt.pending_test_changes = changes
        state.attempt.tokens_used = budget.tokens_used
        state.attempt.elapsed_ms = budget.elapsed_ms
        await self.attempts.update_attempt_status(
            attempt_id,
            AttemptStatus.ESCALATED.value,
            "Aguardando aprovação de alteração em testes existentes.",
            tokens_used=budget.tokens_used,
            duration_ms=budget.elapsed_ms,
        )
        return await self._pause(
            run_id,
            state,
            PAUSE_TEST_CHANGE_APPROVAL,
            {
                "reason": outcome.failure or "",
                "attempt_id": attempt_id,
                "plan_id": current_plan.id,
                "modified_files": outcome.impl.modified_files,
                "test_changes": [change.model_dump() for change in changes],
            },
            attempt_id=attempt_id,
        )

    async def _validate(
        self,
        run_id: str,
        attempt_id: str,
        state: PipelineState,
        current_plan: Plan,
        baseline: Baseline,
        impl: Implementation,
    ) -> AttemptOutcome:
        """Gate determinístico, Tester e Verificador, nessa ordem (D09/D51)."""
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
            call=partial(
                self.gate.check,
                baseline,
                impl,
                approved_test_changes=dict(state.approved_test_changes),
            ),
        )
        if not gate_res.passed:
            # Falha determinística sumária: reprova sem chamar Tester nem Verificador.
            return AttemptOutcome(
                impl, f"Gate determinístico falhou: {gate_res.error_message}", gate=gate_res
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
        failure = None if verdict.approved else f"Verificador reprovou: {verdict.rationale}"
        return AttemptOutcome(impl, failure, gate=gate_res, test=test_res, verdict=verdict)

    async def _pause(
        self,
        run_id: str,
        state: PipelineState,
        predicate: str,
        content: dict[str, Any],
        *,
        attempt_id: str | None = None,
    ) -> RunStatus:
        """Registra a evidência da pausa e deixa a run em ``awaiting_human``.

        A evidência é endereçada pelo conteúdo; o instante da pausa a torna única, para
        que uma pausa repetida (ex.: nova aprovação do mesmo teste) não se funda à anterior.
        """
        await self.evidence.record_evidence(
            run_id=run_id,
            attempt_id=attempt_id,
            agent_type="orchestrator",
            predicate=predicate,
            content={**content, "paused_at": datetime.now(UTC).isoformat()},
        )
        assert_valid_transition(state.phase, Phase.AWAITING_HUMAN)
        state.phase = Phase.AWAITING_HUMAN
        state.pause_reason = predicate
        await self.checkpoints.save_checkpoint(
            run_id=run_id, phase=state.phase.value, snapshot=state.to_snapshot()
        )
        await self.runs.update_status(run_id, RunStatus.AWAITING_HUMAN.value, state.phase.value)
        logger.info("Run %s pausada em awaiting_human (%s).", run_id, predicate)
        return RunStatus.AWAITING_HUMAN

    async def _fail_attempt(
        self,
        run_id: str,
        attempt_id: str,
        budget: AttemptBudget,
        state: PipelineState,
        reason: str,
    ) -> bool:
        """Persiste a tentativa falha (com o consumo real) e avança ``state``.

        Devolve ``False`` quando os limites se esgotaram numa run sobre projeto: em vez
        de marcar a run ``failed``, o chamador pede orientação humana (D43/D62).
        """
        await self.attempts.update_attempt_status(
            attempt_id,
            AttemptStatus.FAILED.value,
            reason,
            tokens_used=budget.tokens_used,
            duration_ms=budget.elapsed_ms,
        )
        phase, plan_index, attempt_index, feedback = await self._handle_attempt_failure(
            run_id, state.plan_index, state.attempt_index, reason
        )
        if phase == Phase.FAILED and self.execution_mode in PROJECT_MODES:
            return False
        state.phase, state.plan_index, state.attempt_index, state.feedback = (
            phase,
            plan_index,
            attempt_index,
            feedback,
        )
        return True

    async def _run_programming_and_review(
        self,
        run_id: str,
        attempt_id: str,
        current_plan: Plan,
        task_description: str,
        feedback: str,
        budget: AttemptBudget,
        rounds_used: int = 0,
    ) -> tuple[Implementation, ReviewResult, int]:
        """Programador -> Corretor, até esgotar `max_review_rounds` (já usadas: ``rounds_used``).

        O orçamento é repassado aos agentes, que registram o consumo real de tokens.
        Devolve também as rodadas de correção consumidas até aqui.
        """
        max_rounds = self.config.mat.limits.max_review_rounds
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
            if review.approved or rounds_used >= max_rounds or budget.is_exhausted:
                return impl, review, rounds_used

            rounds_used += 1
            logger.info(
                "Corretor apontou problemas (rodada %d/%d): %s",
                rounds_used,
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
