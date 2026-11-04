"""MAT State Machine Engine.

Orquestra as fases da esteira (Análise → Programação → Testes Paralelos → Verificador Gate)
com controle estrito de attempts (máx 3 por plano), replans (máx 2) e escalação reversa.
Persiste todo o estado no SQLite Blackboard a cada transição.
"""

import asyncio
import json
import uuid
from typing import Any
from mat_core.config import AppConfig
from mat_core.db.connection import Database
from mat_core.state_machine.context import RunContext
from mat_core.state_machine.states import AttemptStatus, Phase, RunStatus


class StateMachineEngine:
    """Motor de execução da esteira MAT."""

    def __init__(
        self,
        config: AppConfig,
        db: Database,
        analyst_agent: Any,
        programmer_agent: Any,
        tester_happy: Any,
        tester_bypass: Any,
        tester_breaker: Any,
        tester_consolidator: Any,
        verifier_agent: Any,
    ):
        self.config = config
        self.db = db
        self.analyst = analyst_agent
        self.programmer = programmer_agent
        self.tester_happy = tester_happy
        self.tester_bypass = tester_bypass
        self.tester_breaker = tester_breaker
        self.tester_consolidator = tester_consolidator
        self.verifier = verifier_agent

    async def run(self, objective: str, run_id: str | None = None) -> RunContext:
        """Executa a esteira completa até COMPLETED ou FAILED."""
        run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        worktree_path = self.config.mat.paths.worktrees / run_id

        ctx = RunContext(
            run_id=run_id,
            objective=objective,
            db=self.db,
            config=self.config,
            worktree_path=worktree_path,
            current_phase=Phase.ANALYSIS,
            status=RunStatus.RUNNING,
            plan_index=1,
            attempt_index=1,
            total_attempts=0,
        )

        # 1. Registra a run no banco
        await self.db.execute(
            """
            INSERT INTO runs (id, objective, status, current_phase, plan_count, total_attempts)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (ctx.run_id, ctx.objective, ctx.status.value, ctx.current_phase.value, ctx.plan_index, 0),
        )

        # Loop de fases da State Machine
        while ctx.current_phase not in (Phase.COMPLETED, Phase.FAILED, Phase.AWAITING_HUMAN):
            await self._checkpoint(ctx)

            if ctx.current_phase == Phase.ANALYSIS:
                await self._execute_analysis_phase(ctx)
            elif ctx.current_phase == Phase.PROGRAMMING:
                await self._execute_programming_phase(ctx)
            elif ctx.current_phase == Phase.TESTING:
                await self._execute_testing_phase(ctx)
            elif ctx.current_phase == Phase.VERIFICATION:
                await self._execute_verification_phase(ctx)

        # Atualiza status final da run no banco
        await self.db.execute(
            """
            UPDATE runs
            SET status = ?, current_phase = ?, total_attempts = ?, plan_count = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (ctx.status.value, ctx.current_phase.value, ctx.total_attempts, ctx.plan_index, ctx.run_id),
        )
        await self._checkpoint(ctx)
        return ctx

    async def _execute_analysis_phase(self, ctx: RunContext) -> None:
        """Fase 1: Analista mapeia baseline e gera plano."""
        task_id = f"task_analyst_p{ctx.plan_index}"
        await self._register_task(ctx, task_id, "analyst", Phase.ANALYSIS)

        result = await self.analyst.execute(ctx)
        await self._complete_task(task_id, result.success)

        if not result.success:
            ctx.status = RunStatus.FAILED
            ctx.current_phase = Phase.FAILED
            return

        # Grava baseline inicial se for o primeiro plano
        if ctx.baseline and ctx.plan_index == 1:
            await self.db.execute(
                """
                INSERT OR REPLACE INTO baselines
                (run_id, pytest_passed, pytest_total, coverage_pct, ruff_clean, raw_output)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    ctx.run_id,
                    ctx.baseline.pytest_passed,
                    ctx.baseline.pytest_total,
                    ctx.baseline.coverage_pct,
                    1 if ctx.baseline.ruff_clean else 0,
                    json.dumps(ctx.baseline.raw_output),
                ),
            )

        # Registra evidência do plano
        await self._save_evidence(
            ctx=ctx,
            evidence_id=f"ev_plan_p{ctx.plan_index}_{ctx.run_id}",
            task_id=task_id,
            agent_type="analyst",
            predicate="execution_plan",
            content=result.data,
            confidence="derived",
        )

        ctx.current_phase = Phase.PROGRAMMING

    async def _execute_programming_phase(self, ctx: RunContext) -> None:
        """Fase 2: Cria a Attempt e dispara o Programador."""
        ctx.total_attempts += 1
        ctx.current_attempt_id = f"att_{ctx.run_id}_p{ctx.plan_index}_a{ctx.attempt_index}"

        # Registra a tentativa no banco
        await self.db.execute(
            """
            INSERT INTO attempts (id, run_id, plan_index, attempt_index, status, token_budget)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                ctx.current_attempt_id,
                ctx.run_id,
                ctx.plan_index,
                ctx.attempt_index,
                AttemptStatus.RUNNING.value,
                self.config.mat.limits.token_budget_per_attempt,
            ),
        )

        task_id = f"task_prog_p{ctx.plan_index}_a{ctx.attempt_index}"
        await self._register_task(ctx, task_id, "programmer", Phase.PROGRAMMING)

        result = await self.programmer.execute(ctx)
        await self._complete_task(task_id, result.success)

        await self._save_dispatch(
            attempt_id=ctx.current_attempt_id,
            task_id=task_id,
            agent_type="programmer",
            status="completed" if result.success else "failed",
            input_payload={"plan_id": ctx.current_plan.plan_id if ctx.current_plan else None},
            output_payload=result.data,
            tokens_used=result.tokens_used,
        )

        if not result.success:
            await self._handle_attempt_failure(ctx, result.summary)
        else:
            ctx.current_phase = Phase.TESTING

    async def _execute_testing_phase(self, ctx: RunContext) -> None:
        """Fase 3: Testers #1-3 em paralelo + Tester #4 sequencial."""
        # Cria tasks para os 3 testers
        t1_id = f"task_thappy_p{ctx.plan_index}_a{ctx.attempt_index}"
        t2_id = f"task_tbypass_p{ctx.plan_index}_a{ctx.attempt_index}"
        t3_id = f"task_tbreaker_p{ctx.plan_index}_a{ctx.attempt_index}"

        await self._register_task(ctx, t1_id, "tester_happy", Phase.TESTING)
        await self._register_task(ctx, t2_id, "tester_bypass", Phase.TESTING)
        await self._register_task(ctx, t3_id, "tester_breaker", Phase.TESTING)

        # Execução PARALELA dos 3 primeiros testers
        res_happy, res_bypass, res_breaker = await asyncio.gather(
            self.tester_happy.execute(ctx),
            self.tester_bypass.execute(ctx),
            self.tester_breaker.execute(ctx),
        )

        await self._complete_task(t1_id, res_happy.success)
        await self._complete_task(t2_id, res_bypass.success)
        await self._complete_task(t3_id, res_breaker.success)

        # Tester #4 (Consolidador) executa sequencialmente
        t4_id = f"task_tconsol_p{ctx.plan_index}_a{ctx.attempt_index}"
        await self._register_task(ctx, t4_id, "tester_consolidator", Phase.TESTING)

        res_consolidated = await self.tester_consolidator.execute(
            ctx, res_happy, res_bypass, res_breaker
        )
        await self._complete_task(t4_id, res_consolidated.success)

        # Salva o resultado consolidado no contexto para o verificador
        ctx.last_consolidated_result = res_consolidated
        ctx.current_phase = Phase.VERIFICATION

    async def _execute_verification_phase(self, ctx: RunContext) -> None:
        """Fase 4: Verificador Final avalia os resultados e decide o Gate."""
        task_id = f"task_verifier_p{ctx.plan_index}_a{ctx.attempt_index}"
        await self._register_task(ctx, task_id, "tester_verifier", Phase.VERIFICATION)

        consolidated_res = getattr(ctx, "last_consolidated_result", None)
        result = await self.verifier.execute(ctx, consolidated_res)
        await self._complete_task(task_id, result.success)

        if result.success:
            # Aprovado!
            await self.db.execute(
                """
                UPDATE attempts
                SET status = ?, finished_at = datetime('now')
                WHERE id = ?
                """,
                (AttemptStatus.PASSED.value, ctx.current_attempt_id),
            )
            ctx.status = RunStatus.COMPLETED
            ctx.current_phase = Phase.COMPLETED
        else:
            # Reprovado pelo Gate -> aciona loop de retry / escalação
            await self._handle_attempt_failure(ctx, result.summary)

    async def _handle_attempt_failure(self, ctx: RunContext, reason: str) -> None:
        """Gerencia falhas com limites estritos: retry de attempt ou replan de análise."""
        await self.db.execute(
            """
            UPDATE attempts
            SET status = ?, failure_reason = ?, finished_at = datetime('now')
            WHERE id = ?
            """,
            (AttemptStatus.FAILED.value, reason, ctx.current_attempt_id),
        )

        limits = self.config.mat.limits

        # Caso 1: Ainda tem tentativas no plano atual (máx 3)
        if ctx.attempt_index < limits.max_attempts_per_plan:
            ctx.attempt_index += 1
            ctx.last_failure_reason = reason
            ctx.current_phase = Phase.PROGRAMMING  # Volta para o Programador

        # Caso 2: Esgotou tentativas no plano, mas ainda tem replans (máx 2 replans)
        elif ctx.plan_index <= limits.max_replans:
            ctx.plan_index += 1
            ctx.attempt_index = 1
            ctx.last_failure_reason = f"Plano esgotado (3 tentativas). Escalação para novo plano: {reason}"
            ctx.current_phase = Phase.ANALYSIS  # Escalação de volta para o Analista!

        # Caso 3: Esgotou todos os limites (6 tentativas no total)
        else:
            ctx.status = RunStatus.FAILED
            ctx.current_phase = Phase.FAILED
            ctx.last_failure_reason = f"Todos os limites de retry ({limits.max_total_attempts} tentativas) foram esgotados: {reason}"

    async def _register_task(self, ctx: RunContext, task_id: str, agent_type: str, phase: Phase) -> None:
        await self.db.execute(
            """
            INSERT OR REPLACE INTO tasks (id, run_id, agent_type, phase, status)
            VALUES (?, ?, ?, ?, 'running')
            """,
            (task_id, ctx.run_id, agent_type, phase.value),
        )

    async def _complete_task(self, task_id: str, success: bool) -> None:
        status = "completed" if success else "failed"
        await self.db.execute(
            "UPDATE tasks SET status = ?, completed_at = datetime('now') WHERE id = ?",
            (status, task_id),
        )

    async def _save_dispatch(
        self,
        attempt_id: str,
        task_id: str,
        agent_type: str,
        status: str,
        input_payload: dict,
        output_payload: dict,
        tokens_used: int,
    ) -> None:
        dispatch_id = f"disp_{uuid.uuid4().hex[:10]}"
        await self.db.execute(
            """
            INSERT INTO dispatches (id, attempt_id, task_id, agent_type, status, input_payload, output_payload, tokens_used)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dispatch_id,
                attempt_id,
                task_id,
                agent_type,
                status,
                json.dumps(input_payload),
                json.dumps(output_payload),
                tokens_used,
            ),
        )

    async def _save_evidence(
        self,
        ctx: RunContext,
        evidence_id: str,
        task_id: str,
        agent_type: str,
        predicate: str,
        content: dict,
        confidence: str = "observed",
    ) -> None:
        await self.db.execute(
            """
            INSERT OR REPLACE INTO evidence
            (evidence_id, run_id, task_id, attempt_id, agent_type, predicate, content, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence_id,
                ctx.run_id,
                task_id,
                ctx.current_attempt_id,
                agent_type,
                predicate,
                json.dumps(content),
                confidence,
            ),
        )

    async def _checkpoint(self, ctx: RunContext) -> None:
        checkpoint_id = f"chk_{uuid.uuid4().hex[:10]}"
        snapshot = {
            "phase": ctx.current_phase.value,
            "status": ctx.status.value,
            "plan_index": ctx.plan_index,
            "attempt_index": ctx.attempt_index,
            "total_attempts": ctx.total_attempts,
        }
        await self.db.execute(
            """
            INSERT INTO checkpoints (id, run_id, phase, state_snapshot)
            VALUES (?, ?, ?, ?)
            """,
            (checkpoint_id, ctx.run_id, ctx.current_phase.value, json.dumps(snapshot)),
        )
