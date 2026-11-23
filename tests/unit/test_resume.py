"""Retomada explícita de runs pausadas para aprovação do plano (D53)."""

from __future__ import annotations

import json

import pytest

from mat.agents.errors import AmbiguousTaskError
from mat.agents.stubs import (
    AnalystStub,
    GateStub,
    ProgrammerStub,
    ReviewerStub,
    TesterHappyStub,
    VerifierStub,
)
from mat.cli import cmd_resume, cmd_run
from mat.db.connection import Database
from mat.engine.orchestrator import (
    PAUSE_PLAN_APPROVAL,
    PipelineOrchestrator,
    PipelineState,
    ResumeError,
)
from mat.engine.state_machine import ExecutionMode, Phase, RunStatus
from mat.settings import AppConfig, MatConfig


@pytest.fixture
def config(tmp_path) -> AppConfig:
    mat_cfg = MatConfig()
    mat_cfg.paths.db = str(tmp_path / "resume.db")
    mat_cfg.limits.max_attempts_per_plan = 1
    mat_cfg.limits.max_replans = 1
    return AppConfig(mat=mat_cfg, agents={})


@pytest.fixture
async def db(config):
    database = Database(config.mat.paths.db)
    await database.init_schema()
    yield database
    await database.close()


def build(config, db, *, supervised=False, **overrides) -> PipelineOrchestrator:
    agents = {
        "analyst": AnalystStub(),
        "programmer": ProgrammerStub(),
        "reviewer": ReviewerStub(),
        "tester_happy": TesterHappyStub(),
        "verifier": VerifierStub(),
        "gate": GateStub(),
    }
    agents.update(overrides)
    return PipelineOrchestrator(config=config, db=db, supervised=supervised, **agents)


async def pause_for_approval(config, db, run_id="run_sup", **overrides) -> None:
    status = await build(config, db, supervised=True, **overrides).run(
        "Implementar checkout", run_id=run_id
    )
    assert status == RunStatus.AWAITING_HUMAN


async def predicates(db, run_id="run_sup") -> list[str]:
    rows = await db.fetch_all("SELECT predicate FROM evidence WHERE run_id = ?;", (run_id,))
    return [row["predicate"] for row in rows]


async def test_pause_saves_full_checkpoint(config, db):
    await pause_for_approval(config, db)

    row = await db.fetch_one(
        "SELECT phase, state_snapshot FROM checkpoints WHERE run_id = 'run_sup' "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1;"
    )
    assert row["phase"] == Phase.AWAITING_HUMAN.value
    state = PipelineState.from_snapshot(json.loads(row["state_snapshot"]))
    assert state.pause_reason == PAUSE_PLAN_APPROVAL
    assert state.task_description == "Implementar checkout"
    assert state.supervised is True
    assert state.plan is not None and state.plan.id == "plan_run_sup_v1"
    assert state.baseline is not None and state.baseline.pytest_passed == 5


async def test_approve_continues_without_reanalysis(config, db):
    await pause_for_approval(config, db)

    status = await build(config, db).resume("run_sup", reason="ok")

    assert status == RunStatus.PASSED
    run = await db.fetch_one("SELECT * FROM runs WHERE id = 'run_sup';")
    assert run["status"] == "passed"
    assert run["plan_count"] == 1  # o Analista não roda de novo
    assert run["total_attempts"] == 1
    agents = [r["agent_type"] for r in await db.fetch_all("SELECT agent_type FROM dispatches;")]
    # Um único dispatch do Analista: o da análise antes da pausa.
    assert agents == ["analyst", "programmer", "reviewer", "gate", "tester_happy", "verifier"]
    evidence = await db.fetch_one("SELECT content FROM evidence WHERE predicate = 'plan_approved';")
    assert json.loads(evidence["content"]) == {
        "plan_id": "plan_run_sup_v1",
        "plan_index": 1,
        "reason": "ok",
    }


async def test_reject_cancels_without_programming(config, db):
    await pause_for_approval(config, db)

    status = await build(config, db).resume("run_sup", approve=False, reason="escopo errado")

    assert status == RunStatus.CANCELLED
    run = await db.fetch_one("SELECT status FROM runs WHERE id = 'run_sup';")
    assert run["status"] == "cancelled"
    assert await db.fetch_all("SELECT id FROM attempts;") == []
    assert "plan_rejected" in await predicates(db)


async def test_resume_twice_is_refused(config, db):
    await pause_for_approval(config, db)
    await build(config, db).resume("run_sup")

    with pytest.raises(ResumeError, match="não em 'awaiting_human'"):
        await build(config, db).resume("run_sup")


async def test_unknown_run_is_refused(config, db):
    with pytest.raises(ResumeError, match="não encontrada"):
        await build(config, db).resume("run_inexistente")


async def test_execution_mode_must_match(config, db):
    await pause_for_approval(config, db)
    orchestrator = build(config, db)
    orchestrator.execution_mode = ExecutionMode.SIMULATION  # a run foi criada como 'unknown'

    with pytest.raises(ResumeError, match="modo"):
        await orchestrator.resume("run_sup")
    run = await db.fetch_one("SELECT status FROM runs WHERE id = 'run_sup';")
    assert run["status"] == "awaiting_human"


async def test_ambiguous_plan_needs_guidance_to_resume(config, db):
    class AmbiguousAnalyst:
        async def analyze(self, run_id, plan_index, task_description):
            raise AmbiguousTaskError("GET ou POST?")

    status = await build(config, db, analyst=AmbiguousAnalyst()).run("x", run_id="run_amb")
    assert status == RunStatus.AWAITING_HUMAN

    # Sem orientação, um pedido ambíguo não é retomado (D66).
    with pytest.raises(ResumeError, match="--guidance"):
        await build(config, db).resume("run_amb")
    run = await db.fetch_one("SELECT status FROM runs WHERE id = 'run_amb';")
    assert run["status"] == "awaiting_human"


async def test_replanned_plan_pauses_again_for_approval(config, db):
    # Plano 1 reprova no gate → replan → plano 2 precisa de nova aprovação, mesmo que
    # o orquestrador que retoma tenha sido criado sem `supervised` (vale o do checkpoint).
    await pause_for_approval(config, db, gate=GateStub(passed=False))

    status = await build(config, db, gate=GateStub(passed=False)).resume("run_sup")
    assert status == RunStatus.AWAITING_HUMAN
    run = await db.fetch_one("SELECT plan_count FROM runs WHERE id = 'run_sup';")
    assert run["plan_count"] == 2

    status = await build(config, db, gate=GateStub(passed=False)).resume("run_sup")
    assert status == RunStatus.FAILED  # limites esgotados: 2 planos × 1 tentativa
    assert (await predicates(db)).count("plan_approved") == 2


def test_snapshot_with_unknown_version_is_refused():
    snapshot = PipelineState(task_description="x", supervised=True).to_snapshot()
    snapshot["version"] = 99
    with pytest.raises(ResumeError, match="formato desconhecido"):
        PipelineState.from_snapshot(snapshot)


def test_snapshot_round_trip():
    state = PipelineState(task_description="x", supervised=False, plan_index=2, feedback="f")
    assert PipelineState.from_snapshot(state.to_snapshot()) == state


async def test_cli_resume_approves_supervised_run(tmp_path):
    db_file = tmp_path / "cli_resume.db"
    assert await cmd_run("Refatorar models", supervised=True, db_path=str(db_file)) == 0
    db = Database(db_file)
    run_id = (await db.fetch_one("SELECT id FROM runs;"))["id"]
    await db.close()

    assert await cmd_resume(run_id, db_path=str(db_file)) == 0
    assert await cmd_resume(run_id, db_path=str(db_file)) == 1  # já não está pausada

    db = Database(db_file)
    run = await db.fetch_one("SELECT status, execution_mode FROM runs;")
    await db.close()
    assert run["status"] == "passed"
    assert run["execution_mode"] == "simulation"


async def test_cli_resume_reject(tmp_path):
    db_file = tmp_path / "cli_reject.db"
    await cmd_run("Refatorar models", supervised=True, db_path=str(db_file))
    db = Database(db_file)
    run_id = (await db.fetch_one("SELECT id FROM runs;"))["id"]
    await db.close()

    assert await cmd_resume(run_id, reject=True, reason="não", db_path=str(db_file)) == 0

    db = Database(db_file)
    run = await db.fetch_one("SELECT status FROM runs;")
    await db.close()
    assert run["status"] == "cancelled"
