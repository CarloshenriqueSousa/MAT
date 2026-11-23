"""Retomada de pausas por falha com orientação humana e cancelamento (passo 9, D66)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mat.agents.errors import AgentOutputError, AmbiguousTaskError, BaselineFailingError
from mat.agents.stubs import (
    AnalystStub,
    GateStub,
    ProgrammerStub,
    ReviewerStub,
    TesterHappyStub,
    VerifierStub,
)
from mat.cli import cmd_cancel, cmd_resume
from mat.engine.orchestrator import PipelineOrchestrator, ResumeError, cancel_run
from mat.engine.state_machine import ExecutionMode, RunStatus
from mat.models.contracts import Baseline, DeterministicGateResult, Workspace
from mat.settings import load_config
from tests.unit.test_implementation import (
    CREATE_PING,
    DONE,
    GREEN,
    PLAN,
    RED,
    query,
    review,
    sandbox,
    scripted_llm,
    start,
    verdict,
)


class FlakyAnalyst(AnalystStub):
    """Falha com ``error`` na primeira chamada; depois planeja e guarda os pedidos."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error
        self.tasks: list[str] = []

    async def analyze(self, run_id, plan_index, task_description):
        self.tasks.append(task_description)
        if len(self.tasks) == 1:
            raise self.error
        return await super().analyze(run_id, plan_index, task_description)


class RecordingProgrammer(ProgrammerStub):
    def __init__(self, fail_first: bool = False) -> None:
        super().__init__()
        self.fail_first = fail_first
        self.feedbacks: list[str] = []

    async def implement(self, plan, *, task_description="", feedback="", budget=None):
        self.feedbacks.append(feedback)
        if self.fail_first and len(self.feedbacks) == 1:
            raise RuntimeError("sandbox caiu")
        return await super().implement(
            plan, task_description=task_description, feedback=feedback, budget=budget
        )


class GateFailingTimes:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def check(self, baseline, impl, approved_test_changes=None):
        self.calls += 1
        if self.calls <= self.failures:
            return DeterministicGateResult(passed=False, error_message="ruff: E501")
        return await GateStub().check(baseline, impl)


def orchestrator(tmp_db, *, mode=ExecutionMode.IMPLEMENTATION, **agents) -> PipelineOrchestrator:
    defaults = {
        "analyst": AnalystStub(),
        "programmer": ProgrammerStub(),
        "reviewer": ReviewerStub(),
        "tester_happy": TesterHappyStub(),
        "verifier": VerifierStub(),
        "gate": GateStub(),
    }
    return PipelineOrchestrator(
        config=load_config(), db=tmp_db, **{**defaults, **agents}, execution_mode=mode
    )


def workspace(tmp_path: Path) -> Workspace:
    return Workspace(
        repo_path=tmp_path, base_ref="main", base_sha="a" * 40, branch="mat/r", path=tmp_path
    )


async def predicates(tmp_db) -> list[str]:
    rows = await tmp_db.fetch_all("SELECT predicate FROM evidence ORDER BY created_at, rowid;")
    return [r["predicate"] for r in rows]


# ── Falhas de análise ────────────────────────────────────────────────────────


async def test_ambiguous_plan_is_reanalyzed_with_the_clarification(tmp_db, tmp_path):
    analyst = FlakyAnalyst(AmbiguousTaskError("GET ou POST?"))
    orch = orchestrator(tmp_db, analyst=analyst)
    assert await orch.run("Criar /ping", run_id="r", workspace=workspace(tmp_path)) == (
        RunStatus.AWAITING_HUMAN
    )
    assert "plan_ambiguous" in await predicates(tmp_db)

    status = await orch.resume("r", guidance="Use GET.")

    # O novo plano volta a pedir aprovação (D41).
    assert status == RunStatus.AWAITING_HUMAN
    assert analyst.tasks[1] == "Criar /ping\n\nEsclarecimento da pessoa: Use GET."
    evidence = await predicates(tmp_db)
    assert evidence[-1] == "plan_created"
    guidance = await tmp_db.fetch_one(
        "SELECT content FROM evidence WHERE predicate = 'guidance_given';"
    )
    assert json.loads(guidance["content"])["restart_phase"] == "analysis"
    assert await orch.resume("r") == RunStatus.AWAITING_HUMAN  # aprova o plano novo
    assert (await predicates(tmp_db))[-1] == "final_review"


async def test_invalid_analysis_can_be_retried_without_guidance(tmp_db, tmp_path):
    analyst = FlakyAnalyst(AgentOutputError("JSON inválido"))
    orch = orchestrator(tmp_db, analyst=analyst)
    await orch.run("x", run_id="r", workspace=workspace(tmp_path))
    assert "analysis_failed" in await predicates(tmp_db)

    assert await orch.resume("r") == RunStatus.AWAITING_HUMAN
    assert analyst.tasks == ["x", "x"]


async def test_failing_baseline_is_not_resumable_but_can_be_cancelled(tmp_db, tmp_path):
    baseline = Baseline(id="b", run_id="r", pytest_exit_code=1, raw_output="1 failed")
    orch = orchestrator(tmp_db, analyst=FlakyAnalyst(BaselineFailingError(baseline)))
    await orch.run("x", run_id="r", workspace=workspace(tmp_path))

    with pytest.raises(ResumeError, match="base"):
        await orch.resume("r")
    await cancel_run(tmp_db, "r", "base quebrada")
    assert (await tmp_db.fetch_one("SELECT status FROM runs;"))["status"] == "cancelled"


# ── Falhas de validação e de agente ──────────────────────────────────────────


async def test_validation_failure_opens_a_new_attempt_with_the_guidance(tmp_db, tmp_path):
    programmer = RecordingProgrammer()
    orch = orchestrator(tmp_db, programmer=programmer, gate=GateFailingTimes(2))
    await orch.run("x", run_id="r", workspace=workspace(tmp_path))
    assert await orch.resume("r") == RunStatus.AWAITING_HUMAN
    assert (await predicates(tmp_db))[-1] == "validation_failed"

    assert await orch.resume("r", guidance="quebre a linha longa") == RunStatus.AWAITING_HUMAN

    assert (await predicates(tmp_db))[-1] == "final_review"
    attempts = await tmp_db.fetch_all(
        "SELECT id, status FROM attempts ORDER BY created_at, rowid;"
    )
    assert [a["status"] for a in attempts] == ["failed", "passed"]
    assert attempts[1]["id"].endswith("_a2")
    feedback = programmer.feedbacks[-1]
    assert "A tentativa anterior falhou: Gate determinístico falhou: ruff: E501" in feedback
    assert "Orientação da pessoa: quebre a linha longa" in feedback


async def test_agent_failure_during_programming_is_retried_in_a_new_attempt(tmp_db, tmp_path):
    programmer = RecordingProgrammer(fail_first=True)
    orch = orchestrator(tmp_db, programmer=programmer)
    orch.config.mat.limits.max_dispatch_retries = 0
    await orch.run("x", run_id="r", workspace=workspace(tmp_path))
    assert await orch.resume("r") == RunStatus.AWAITING_HUMAN
    assert "agent_failure_escalated" in await predicates(tmp_db)

    assert await orch.resume("r") == RunStatus.AWAITING_HUMAN

    attempts = await tmp_db.fetch_all("SELECT status FROM attempts ORDER BY created_at, rowid;")
    assert [a["status"] for a in attempts] == ["escalated", "passed"]
    assert "sandbox caiu" in programmer.feedbacks[-1]
    assert (await predicates(tmp_db))[-1] == "final_review"


async def test_rejecting_a_failure_pause_cancels_the_run(tmp_db, tmp_path):
    orch = orchestrator(tmp_db, gate=GateFailingTimes(99))
    await orch.run("x", run_id="r", workspace=workspace(tmp_path))
    await orch.resume("r")

    assert await orch.resume("r", approve=False, reason="desisto") == RunStatus.CANCELLED
    row = await tmp_db.fetch_one("SELECT content FROM evidence WHERE predicate = 'run_cancelled';")
    assert json.loads(row["content"]) == {"pause": "validation_failed", "reason": "desisto"}


# ── Cancelamento ─────────────────────────────────────────────────────────────


async def test_cancel_refuses_running_finished_and_unknown_runs(tmp_db):
    await tmp_db.execute("INSERT INTO runs (id, status) VALUES ('rodando', 'running');")
    await tmp_db.execute("INSERT INTO runs (id, status) VALUES ('feita', 'passed');")
    await tmp_db.execute("INSERT INTO runs (id, status) VALUES ('nova', 'pending');")

    for run_id in ("rodando", "feita", "nenhuma"):
        with pytest.raises(ResumeError):
            await cancel_run(tmp_db, run_id)
    await cancel_run(tmp_db, "nova")
    statuses = {
        r["id"]: r["status"] for r in await tmp_db.fetch_all("SELECT id, status FROM runs;")
    }
    assert statuses == {"rodando": "running", "feita": "passed", "nova": "cancelled"}


# ── CLI de ponta a ponta ─────────────────────────────────────────────────────


async def test_cli_resumes_a_validation_failure_with_guidance(repo, mat_config, capsys):
    llm = scripted_llm(
        PLAN, CREATE_PING, DONE, review(True), DONE, review(True),  # tentativa 1 + correção
        DONE, review(True), verdict(True),  # tentativa 2, com a orientação
    )
    runner = sandbox(RED, RED, GREEN, GREEN)
    await start(repo, llm, runner)
    run_id = (await query(mat_config, "SELECT id FROM runs;"))[0]["id"]
    await cmd_resume(run_id, llm=llm, runner=runner)

    capsys.readouterr()
    assert await cmd_resume(run_id, guidance="rode o ruff", llm=llm, runner=runner) == 0
    out = capsys.readouterr().out
    assert "revisão final" in out
    prompts = [str(call.kwargs["messages"][0]["content"]) for call in llm.call.await_args_list]
    assert any("Orientação da pessoa: rode o ruff" in p for p in prompts)


async def test_cli_cancel(repo, mat_config, capsys):
    await start(repo, scripted_llm(PLAN), sandbox())
    run_id = (await query(mat_config, "SELECT id FROM runs;"))[0]["id"]

    assert await cmd_cancel(run_id, reason="mudei de ideia") == 0
    assert (await query(mat_config, "SELECT status FROM runs;"))[0]["status"] == "cancelled"
    assert await cmd_cancel(run_id) == 1
