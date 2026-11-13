"""Unit tests for the PipelineOrchestrator and state transitions."""

from __future__ import annotations

import json

import pytest

from mat.agents.errors import AgentExecutionError
from mat.agents.stubs import (
    AnalystStub,
    ProgrammerStub,
    ReviewerStub,
    TesterHappyStub,
    VerifierStub,
)
from mat.db.connection import Database
from mat.engine.orchestrator import PipelineOrchestrator
from mat.engine.state_machine import ExecutionMode, RunStatus
from mat.settings import AppConfig, MatConfig


@pytest.fixture
def test_config(tmp_path) -> AppConfig:
    mat_cfg = MatConfig()
    mat_cfg.paths.db = tmp_path / "test_pipe.db"
    mat_cfg.paths.worktrees = tmp_path / "worktrees"
    mat_cfg.limits.max_attempts_per_plan = 3
    mat_cfg.limits.max_replans = 1  # 2 planos x 3 = 6 tentativas
    return AppConfig(mat=mat_cfg, agents={})


@pytest.mark.asyncio
async def test_orchestrator_happy_path(tmp_path, test_config):
    db = Database(test_config.mat.paths.db)
    await db.init_schema()

    orchestrator = PipelineOrchestrator(
        config=test_config,
        db=db,
        analyst=AnalystStub(),
        programmer=ProgrammerStub(should_fail=False),
        reviewer=ReviewerStub(approve_on_turn=1),
        tester_happy=TesterHappyStub(passed=True),
        verifier=VerifierStub(approved=True),
        supervised=False,
    )

    base_sha = "a" * 40
    status = await orchestrator.run("Adicionar função de soma", base_sha=base_sha)
    assert status == RunStatus.PASSED

    # Checa persistência
    runs = await db.fetch_all("SELECT * FROM runs;")
    assert len(runs) == 1
    assert runs[0]["status"] == "passed"
    assert runs[0]["plan_count"] == 1
    assert runs[0]["total_attempts"] == 1
    assert runs[0]["base_sha"] == base_sha

    dispatches = await db.fetch_all(
        "SELECT agent_type, status, input_payload, output_payload FROM dispatches;"
    )
    assert [row["agent_type"] for row in dispatches] == [
        "programmer",
        "reviewer",
        "tester_happy",
        "verifier",
    ]
    assert all(row["status"] == "done" for row in dispatches)
    assert json.loads(dispatches[0]["input_payload"])["plan"]["approach"] == (
        "Implementar feature com cobertura de testes"
    )
    assert json.loads(dispatches[0]["output_payload"])["modified_files"] == ["src/calculator.py"]

    evidence = await db.fetch_all(
        "SELECT task_id, attempt_id, predicate FROM evidence "
        "WHERE predicate = 'dispatch_completed';"
    )
    assert len(evidence) == len(dispatches)
    assert all(row["task_id"] and row["attempt_id"] for row in evidence)

    await db.close()


@pytest.mark.asyncio
async def test_real_execution_requires_full_base_sha(test_config):
    db = Database(test_config.mat.paths.db)
    await db.init_schema()
    orchestrator = PipelineOrchestrator(
        config=test_config,
        db=db,
        analyst=AnalystStub(),
        programmer=ProgrammerStub(),
        reviewer=ReviewerStub(),
        tester_happy=TesterHappyStub(),
        verifier=VerifierStub(),
        execution_mode=ExecutionMode.REAL,
    )

    with pytest.raises(ValueError, match="base_sha"):
        await orchestrator.run("Implementar recurso")

    await db.close()


@pytest.mark.asyncio
async def test_orchestrator_pauses_on_permanent_agent_failure(tmp_path, test_config):
    class FailingProgrammer:
        async def implement(self, plan, feedback=""):
            raise RuntimeError("agent provider unavailable")

    db = Database(test_config.mat.paths.db)
    await db.init_schema()
    orchestrator = PipelineOrchestrator(
        config=test_config,
        db=db,
        analyst=AnalystStub(),
        programmer=FailingProgrammer(),
        reviewer=ReviewerStub(),
        tester_happy=TesterHappyStub(),
        verifier=VerifierStub(),
    )

    status = await orchestrator.run("Implementar recurso", run_id="run_dispatch_failure")
    assert status == RunStatus.AWAITING_HUMAN

    dispatch = await db.fetch_one(
        "SELECT d.id AS dispatch_id, d.status, d.output_payload, t.status AS task_status "
        "FROM dispatches d JOIN tasks t ON t.id = d.task_id;"
    )
    assert dispatch["status"] == "failed"
    assert dispatch["task_status"] == "failed"
    assert json.loads(dispatch["output_payload"]) == {
        "dispatch_id": dispatch["dispatch_id"],
        "retry_index": 0,
        "error_type": "RuntimeError",
        "error": "agent provider unavailable",
        "retry_recommended": False,
    }

    evidence = await db.fetch_one(
        "SELECT predicate FROM evidence WHERE predicate = 'dispatch_failed';"
    )
    assert evidence["predicate"] == "dispatch_failed"
    run = await db.fetch_one("SELECT status FROM runs WHERE id = 'run_dispatch_failure';")
    assert run["status"] == RunStatus.AWAITING_HUMAN.value
    attempt = await db.fetch_one(
        "SELECT status FROM attempts WHERE run_id = 'run_dispatch_failure';"
    )
    assert attempt["status"] == "escalated"
    await db.close()


@pytest.mark.asyncio
async def test_orchestrator_retries_recommended_dispatch_failure(tmp_path, test_config):
    class FlakyProgrammer:
        def __init__(self):
            self.calls = 0

        async def implement(self, plan, feedback=""):
            self.calls += 1
            if self.calls == 1:
                raise AgentExecutionError("temporary provider error", retry_recommended=True)
            return await ProgrammerStub().implement(plan, feedback)

    test_config.mat.limits.max_dispatch_retries = 1
    db = Database(test_config.mat.paths.db)
    await db.init_schema()
    programmer = FlakyProgrammer()
    orchestrator = PipelineOrchestrator(
        config=test_config,
        db=db,
        analyst=AnalystStub(),
        programmer=programmer,
        reviewer=ReviewerStub(),
        tester_happy=TesterHappyStub(),
        verifier=VerifierStub(),
    )

    status = await orchestrator.run("Implementar recurso", run_id="run_dispatch_retry")
    assert status == RunStatus.PASSED
    assert programmer.calls == 2

    dispatches = await db.fetch_all(
        "SELECT id, status FROM dispatches WHERE agent_type = 'programmer';"
    )
    assert len(dispatches) == 1
    assert dispatches[0]["status"] == "done"
    retry_evidence = await db.fetch_one(
        "SELECT content FROM evidence WHERE predicate = 'dispatch_retry_recommended';"
    )
    assert json.loads(retry_evidence["content"])["retry_index"] == 0
    completed = await db.fetch_one(
        "SELECT content FROM evidence WHERE predicate = 'dispatch_completed' "
        "AND agent_type = 'programmer';"
    )
    assert json.loads(completed["content"])["retry_count"] == 1
    await db.close()


@pytest.mark.asyncio
async def test_orchestrator_escalates_after_dispatch_retry_limit(tmp_path, test_config):
    class AlwaysFailingProgrammer:
        async def implement(self, plan, feedback=""):
            raise AgentExecutionError("provider timeout", retry_recommended=True)

    test_config.mat.limits.max_dispatch_retries = 1
    db = Database(test_config.mat.paths.db)
    await db.init_schema()
    orchestrator = PipelineOrchestrator(
        config=test_config,
        db=db,
        analyst=AnalystStub(),
        programmer=AlwaysFailingProgrammer(),
        reviewer=ReviewerStub(),
        tester_happy=TesterHappyStub(),
        verifier=VerifierStub(),
    )

    status = await orchestrator.run("Implementar recurso", run_id="run_dispatch_exhausted")
    assert status == RunStatus.AWAITING_HUMAN

    dispatch = await db.fetch_one("SELECT status FROM dispatches WHERE agent_type = 'programmer';")
    assert dispatch["status"] == "failed"
    evidence = await db.fetch_all(
        "SELECT predicate FROM evidence WHERE run_id = 'run_dispatch_exhausted';"
    )
    predicates = [row["predicate"] for row in evidence]
    assert predicates.count("dispatch_retry_recommended") == 2
    assert "dispatch_failed" in predicates
    assert "agent_failure_escalated" in predicates
    await db.close()


@pytest.mark.asyncio
async def test_orchestrator_reviewer_correction_loop(tmp_path, test_config):
    db = Database(test_config.mat.paths.db)
    await db.init_schema()

    # Corretor rejeita na 1ª volta e aprova na 2ª
    reviewer = ReviewerStub(approve_on_turn=2)

    orchestrator = PipelineOrchestrator(
        config=test_config,
        db=db,
        analyst=AnalystStub(),
        programmer=ProgrammerStub(),
        reviewer=reviewer,
        tester_happy=TesterHappyStub(passed=True),
        verifier=VerifierStub(approved=True),
        supervised=False,
    )

    status = await orchestrator.run("Implementar multiplicação")
    assert status == RunStatus.PASSED
    assert reviewer.current_turn == 2

    await db.close()


@pytest.mark.asyncio
async def test_orchestrator_verifier_retry_same_plan(tmp_path, test_config):
    db = Database(test_config.mat.paths.db)
    await db.init_schema()

    class MultiAttemptVerifier:
        def __init__(self):
            self.calls = 0

        async def verify(self, plan, impl, gate_res, test_res, task_description):
            self.calls += 1
            if self.calls == 1:
                return await VerifierStub(approved=False, rationale="Falta documentação").verify(
                    plan, impl, gate_res, test_res, task_description
                )
            return await VerifierStub(approved=True).verify(
                plan, impl, gate_res, test_res, task_description
            )

    orchestrator = PipelineOrchestrator(
        config=test_config,
        db=db,
        analyst=AnalystStub(),
        programmer=ProgrammerStub(),
        reviewer=ReviewerStub(approve_on_turn=1),
        tester_happy=TesterHappyStub(passed=True),
        verifier=MultiAttemptVerifier(),
        supervised=False,
    )

    status = await orchestrator.run("Implementar divisão")
    assert status == RunStatus.PASSED

    attempts = await db.fetch_all("SELECT * FROM attempts ORDER BY created_at ASC;")
    assert len(attempts) == 2
    assert attempts[0]["status"] == "failed"
    assert attempts[1]["status"] == "passed"

    await db.close()


@pytest.mark.asyncio
async def test_orchestrator_escalate_to_analyst(tmp_path, test_config):
    db = Database(test_config.mat.paths.db)
    await db.init_schema()

    class EscalatingVerifier:
        async def verify(self, plan, impl, gate_res, test_res, task_description):
            # Falha se for plano 1, passa se for plano 2
            if plan.plan_index == 1:
                return await VerifierStub(approved=False, rationale="Plano 1 incompleto").verify(
                    plan, impl, gate_res, test_res, task_description
                )
            return await VerifierStub(approved=True).verify(
                plan, impl, gate_res, test_res, task_description
            )

    orchestrator = PipelineOrchestrator(
        config=test_config,
        db=db,
        analyst=AnalystStub(),
        programmer=ProgrammerStub(),
        reviewer=ReviewerStub(approve_on_turn=1),
        tester_happy=TesterHappyStub(passed=True),
        verifier=EscalatingVerifier(),
        supervised=False,
    )

    status = await orchestrator.run("Refatorar sistema")
    assert status == RunStatus.PASSED

    runs = await db.fetch_all("SELECT * FROM runs;")
    assert runs[0]["plan_count"] == 2
    assert runs[0]["total_attempts"] == 4  # 3 no plano 1 + 1 no plano 2

    await db.close()


@pytest.mark.asyncio
async def test_orchestrator_supervised_checkpoint(tmp_path, test_config):
    db = Database(test_config.mat.paths.db)
    await db.init_schema()

    orchestrator = PipelineOrchestrator(
        config=test_config,
        db=db,
        analyst=AnalystStub(),
        programmer=ProgrammerStub(),
        reviewer=ReviewerStub(),
        tester_happy=TesterHappyStub(),
        verifier=VerifierStub(),
        supervised=True,  # Modo supervisionado!
    )

    status = await orchestrator.run("Implementar checkout")
    assert status == RunStatus.AWAITING_HUMAN

    runs = await db.fetch_all("SELECT * FROM runs;")
    assert runs[0]["status"] == "awaiting_human"
    assert runs[0]["current_phase"] == "awaiting_human"

    await db.close()
