"""Regression tests for orchestrator bugs fixed in Fase 0."""

from __future__ import annotations

import json

import pytest

from mat.agents.errors import AgentOutputError, AmbiguousTaskError
from mat.agents.stubs import (
    AnalystStub,
    GateStub,
    ProgrammerStub,
    ReviewerStub,
    TesterHappyStub,
    VerifierStub,
)
from mat.db.connection import Database
from mat.engine.orchestrator import PipelineOrchestrator
from mat.engine.state_machine import RunStatus
from mat.settings import AppConfig, MatConfig


@pytest.fixture
def config(tmp_path) -> AppConfig:
    mat_cfg = MatConfig()
    mat_cfg.paths.db = str(tmp_path / "regress.db")
    mat_cfg.limits.max_attempts_per_plan = 1
    mat_cfg.limits.max_replans = 0
    return AppConfig(mat=mat_cfg, agents={})


@pytest.fixture
async def db(config):
    database = Database(config.mat.paths.db)
    await database.init_schema()
    yield database
    await database.close()


def build(config, db, **overrides) -> PipelineOrchestrator:
    agents = {
        "analyst": AnalystStub(),
        "programmer": ProgrammerStub(),
        "reviewer": ReviewerStub(),
        "tester_happy": TesterHappyStub(),
        "verifier": VerifierStub(),
        "gate": GateStub(),
    }
    agents.update(overrides)
    return PipelineOrchestrator(config=config, db=db, **agents)


class RecordingProgrammer:
    """Captures keyword arguments and spends tokens from the attempt budget."""

    def __init__(self, tokens_per_call: int = 0) -> None:
        self.calls: list[dict] = []
        self.tokens_per_call = tokens_per_call

    async def implement(self, plan, *, task_description, feedback="", budget=None):
        self.calls.append({"task_description": task_description, "feedback": feedback})
        if budget is not None:
            budget.record_tokens(self.tokens_per_call)
        return await ProgrammerStub().implement(plan, task_description=task_description)


async def test_programmer_receives_task_and_feedback_separately(config, db):
    programmer = RecordingProgrammer()
    status = await build(config, db, programmer=programmer, reviewer=ReviewerStub(2)).run(
        "Adicionar soma"
    )

    assert status == RunStatus.PASSED
    assert programmer.calls[0] == {"task_description": "Adicionar soma", "feedback": ""}
    assert programmer.calls[1]["task_description"] == "Adicionar soma"
    assert "Corretor" in programmer.calls[1]["feedback"]


async def test_review_rounds_are_bounded_and_fail_the_attempt(config, db):
    programmer = RecordingProgrammer()
    status = await build(
        config, db, programmer=programmer, reviewer=ReviewerStub(approve_on_turn=99)
    ).run("Nunca aprovado", run_id="run_review_bound")

    assert status == RunStatus.FAILED
    # 1 implementação inicial + max_review_rounds (1) correção.
    assert len(programmer.calls) == 2
    attempt = await db.fetch_one("SELECT status, failure_reason FROM attempts;")
    assert attempt["status"] == "failed"
    assert "Corretor" in attempt["failure_reason"]


async def test_gate_failure_skips_tester_and_verifier(config, db):
    status = await build(config, db, gate=GateStub(passed=False, error_message="ruff")).run(
        "Gate reprova"
    )

    assert status == RunStatus.FAILED
    agents = [row["agent_type"] for row in await db.fetch_all("SELECT agent_type FROM dispatches;")]
    assert agents == ["programmer", "reviewer", "gate"]
    attempt = await db.fetch_one("SELECT failure_reason FROM attempts;")
    assert "Gate determinístico falhou: ruff" in attempt["failure_reason"]


async def test_real_token_usage_is_persisted(config, db):
    status = await build(config, db, programmer=RecordingProgrammer(tokens_per_call=1234)).run(
        "Contar tokens"
    )

    assert status == RunStatus.PASSED
    attempt = await db.fetch_one("SELECT tokens_used FROM attempts;")
    assert attempt["tokens_used"] == 1234


@pytest.mark.parametrize(
    ("error", "predicate"),
    [
        (AmbiguousTaskError("GET ou POST?"), "plan_ambiguous"),
        (AgentOutputError("sem JSON"), "analysis_failed"),
    ],
)
async def test_analyst_errors_pause_for_human(config, db, error, predicate):
    class FailingAnalyst:
        async def analyze(self, run_id, plan_index, task_description):
            raise error

    status = await build(config, db, analyst=FailingAnalyst()).run("x", run_id="run_analyst")

    assert status == RunStatus.AWAITING_HUMAN
    run = await db.fetch_one("SELECT status FROM runs WHERE id = 'run_analyst';")
    assert run["status"] == "awaiting_human"
    evidence = await db.fetch_one("SELECT content FROM evidence WHERE predicate = ?;", (predicate,))
    assert str(error) in json.loads(evidence["content"])["reason"]


async def test_unexpected_crash_marks_run_failed_and_reraises(config, db):
    class BrokenAnalyst:
        async def analyze(self, run_id, plan_index, task_description):
            raise KeyError("bug")

    with pytest.raises(KeyError):
        await build(config, db, analyst=BrokenAnalyst()).run("x", run_id="run_crash")

    run = await db.fetch_one("SELECT status, current_phase FROM runs WHERE id = 'run_crash';")
    assert (run["status"], run["current_phase"]) == ("failed", "failed")
    evidence = await db.fetch_one("SELECT predicate FROM evidence WHERE run_id = 'run_crash';")
    assert evidence["predicate"] == "run_failed"


async def test_permanent_dispatch_failure_records_single_failed_evidence(config, db):
    class FailingProgrammer:
        async def implement(self, plan, **kwargs):
            raise RuntimeError("down")

    await build(config, db, programmer=FailingProgrammer()).run("x", run_id="run_once")

    rows = await db.fetch_all(
        "SELECT predicate FROM evidence WHERE run_id = 'run_once' AND predicate = 'dispatch_failed';"
    )
    assert len(rows) == 1
