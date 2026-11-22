"""Modo implementation: todos os agentes reais após aprovar o plano (D61/D62)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock

from mat.agents.analyst import AnalystAgent
from mat.agents.llm import LLMClient, LLMResponse
from mat.agents.programmer import ProgrammerAgent
from mat.agents.reviewer import ReviewerAgent
from mat.agents.stubs import (
    AnalystStub,
    GateStub,
    ProgrammerStub,
    ReviewerStub,
    TesterHappyStub,
    VerifierStub,
)
from mat.agents.testers.happy_path import TesterHappyAgent
from mat.agents.verifier import VerifierAgent
from mat.cli import cmd_report, cmd_resume, cmd_run
from mat.db.connection import Database
from mat.db.schema import EXECUTION_MODE_CHECK, SCHEMA_SQL, SCHEMA_VERSION
from mat.engine.orchestrator import PipelineOrchestrator
from mat.engine.state_machine import ExecutionMode, RunStatus
from mat.factory import build_implementation_agents
from mat.gate.deterministic import DeterministicGate
from mat.models.contracts import Workspace
from mat.project import list_branches
from mat.sandbox.docker_runner import CommandResult
from mat.settings import load_config
from tests.conftest import PLAN_JSON, git


@dataclass
class Block:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)


def answer(*blocks: Block, stop: str = "end_turn") -> LLMResponse:
    return LLMResponse(
        content=list(blocks), stop_reason=stop, input_tokens=7, output_tokens=3, raw_message=None
    )


def scripted_llm(*responses: LLMResponse) -> AsyncMock:
    llm = AsyncMock(spec=LLMClient)
    llm.call.side_effect = list(responses)
    return llm


PLAN = answer(Block("text", json.dumps(PLAN_JSON)))
CREATE_PING = answer(
    Block(
        "tool_use",
        id="t1",
        name="text_editor",
        input={
            "command": "create",
            "path": "ping.py",
            "file_text": "def ping():\n    return 'pong'\n",
        },
    ),
    stop="tool_use",
)
DONE = answer(Block("text", "Implementei ping()."))


def review(approved: bool) -> LLMResponse:
    verdict = {
        "approved": approved,
        "issues": [] if approved else ["faltam testes"],
        "meets_task_objective": approved,
        "feedback": "ok" if approved else "adicione testes",
    }
    return answer(Block("text", json.dumps(verdict)))


def verdict(approved: bool) -> LLMResponse:
    criteria = dict.fromkeys(
        ("meets_objective", "test_coverage_valid", "no_unintended_changes", "gate_and_tests_passed"),
        approved,
    )
    body = {
        "approved": approved,
        "criteria": criteria,
        "evidence_cited": ["ev_diff"],
        "rationale": "Atende ao pedido." if approved else "Falta o teste de /ping.",
    }
    return answer(Block("text", json.dumps(body)))


GREEN = CommandResult(exit_code=0, stdout="collected 2 items\n2 passed in 0.01s\n", stderr="")
RED = CommandResult(exit_code=1, stdout="1 failed, 1 passed\n", stderr="")


def sandbox(*pytest_results: CommandResult) -> AsyncMock:
    """Sandbox falso: baseline verde seguido dos resultados de pytest dados (gate, Tester…)."""
    runner = AsyncMock()
    runner.run_pytest.side_effect = [GREEN, *(pytest_results or (GREEN, GREEN))]
    runner.run_ruff.return_value = CommandResult(exit_code=0, stdout="", stderr="")
    return runner


async def start(repo: Path, llm, runner) -> str:
    sha = (await list_branches(repo))[0].sha
    code = await cmd_run(
        "Adicionar /ping", repo=str(repo), base="main", base_sha=sha, llm=llm, runner=runner
    )
    assert code == 0
    return sha


async def query(mat_config: Path, sql: str) -> list[sqlite3.Row]:
    db = Database(mat_config / "mat.db")
    rows = await db.fetch_all(sql)
    await db.close()
    return rows


# ── CLI de ponta a ponta com agentes reais e LLM roteirizado ─────────────────


async def test_approved_plan_is_validated_and_paused_for_final_review(repo, mat_config, capsys):
    llm = scripted_llm(PLAN, CREATE_PING, DONE, review(True), verdict(True))
    runner = sandbox()
    sha = await start(repo, llm, runner)

    run = (await query(mat_config, "SELECT * FROM runs;"))[0]
    worktree = Path(run["worktree_path"])
    # Antes da aprovação, nada foi editado.
    assert run["execution_mode"] == "implementation"
    assert not (worktree / "ping.py").exists()

    capsys.readouterr()
    assert await cmd_resume(run["id"], llm=llm, runner=runner) == 0
    out = capsys.readouterr().out

    after = (await query(mat_config, "SELECT * FROM runs;"))[0]
    # Mesmo aprovada pelo Verificador, a entrega aguarda revisão humana (D62).
    assert (after["status"], after["current_phase"]) == ("awaiting_human", "awaiting_human")
    # O Programador editou só o worktree, sem commit; o checkout original segue intacto.
    assert (worktree / "ping.py").read_text(encoding="utf-8").startswith("def ping")
    assert git(worktree, "rev-parse", "HEAD") == sha
    assert "ping.py" in git(worktree, "status", "--porcelain")
    assert not (repo / "ping.py").exists()

    attempts = await query(mat_config, "SELECT status, tokens_used FROM attempts;")
    assert [(a["status"], a["tokens_used"] > 0) for a in attempts] == [("passed", True)]
    agents = [
        r["agent_type"]
        for r in await query(mat_config, "SELECT agent_type FROM tasks ORDER BY created_at, rowid;")
    ]
    assert agents == ["analyst", "programmer", "reviewer", "gate", "tester_happy", "verifier"]
    pause = await query(mat_config, "SELECT content FROM evidence WHERE predicate = 'final_review';")
    content = json.loads(pause[0]["content"])
    assert content["modified_files"] == ["ping.py"]
    assert content["tests"] == {"passed": True, "tests_run": 2, "tests_passed": 2}
    assert content["verdict"]["approved"] is True
    assert "ping.py" in out
    assert "Atende ao pedido." in out
    # A revisão final ainda não é retomável (passo 8).
    assert await cmd_resume(run["id"], llm=llm, runner=runner) == 1
    assert await cmd_report(run["id"]) == 0


async def test_failing_gate_gets_one_correction_then_asks_for_guidance(repo, mat_config, capsys):
    # Programador + Corretor, gate vermelho; uma correção (Programador + Corretor), gate vermelho.
    llm = scripted_llm(PLAN, CREATE_PING, DONE, review(True), DONE, review(True))
    runner = sandbox(RED, RED)
    await start(repo, llm, runner)
    run_id = (await query(mat_config, "SELECT id FROM runs;"))[0]["id"]

    capsys.readouterr()
    assert await cmd_resume(run_id, llm=llm, runner=runner) == 0
    out = capsys.readouterr().out

    run = (await query(mat_config, "SELECT status FROM runs;"))[0]
    attempt = (await query(mat_config, "SELECT status, failure_reason FROM attempts;"))[0]
    # Esgotada a correção, a run pede orientação em vez de falhar (D43/D62).
    assert run["status"] == "awaiting_human"
    assert attempt["status"] == "failed"
    assert "Gate determinístico falhou" in attempt["failure_reason"]
    predicates = [r["predicate"] for r in await query(mat_config, "SELECT predicate FROM evidence;")]
    assert predicates.count("validation_correction") == 1
    assert "validation_failed" in predicates
    assert "Gate determinístico falhou" in out
    assert await cmd_resume(run_id, llm=llm, runner=runner) == 1


async def test_correction_round_can_fix_a_failing_gate(repo, mat_config):
    llm = scripted_llm(PLAN, CREATE_PING, DONE, review(True), DONE, review(True), verdict(True))
    runner = sandbox(RED, GREEN, GREEN)
    await start(repo, llm, runner)
    run_id = (await query(mat_config, "SELECT id FROM runs;"))[0]["id"]

    assert await cmd_resume(run_id, llm=llm, runner=runner) == 0

    attempts = await query(mat_config, "SELECT status FROM attempts;")
    assert [a["status"] for a in attempts] == ["passed"]
    pause = await query(mat_config, "SELECT 1 FROM evidence WHERE predicate = 'final_review';")
    assert len(pause) == 1


async def test_rejecting_needs_no_agents(repo, mat_config):
    llm = scripted_llm(PLAN)
    await start(repo, llm, sandbox())
    run_id = (await query(mat_config, "SELECT id FROM runs;"))[0]["id"]

    # Sem chave de API no ambiente e sem dublês: rejeitar não monta agentes reais.
    assert await cmd_resume(run_id, reject=True) == 0
    assert (await query(mat_config, "SELECT status FROM runs;"))[0]["status"] == "cancelled"


async def test_approving_without_api_key_keeps_the_run_paused(repo, mat_config):
    await start(repo, scripted_llm(PLAN), sandbox())
    run_id = (await query(mat_config, "SELECT id FROM runs;"))[0]["id"]

    assert await cmd_resume(run_id) == 1
    assert (await query(mat_config, "SELECT status FROM runs;"))[0]["status"] == "awaiting_human"


async def test_plan_only_requires_a_project(mat_config):
    assert await cmd_run("x", plan_only=True) == 1


# ── Orquestrador e fábrica ───────────────────────────────────────────────────


def stub_orchestrator(tmp_db, **agents) -> PipelineOrchestrator:
    defaults = {
        "analyst": AnalystStub(),
        "programmer": ProgrammerStub(),
        "reviewer": ReviewerStub(),
        "tester_happy": TesterHappyStub(),
        "verifier": VerifierStub(),
        "gate": GateStub(passed=True),
    }
    return PipelineOrchestrator(
        config=load_config(),
        db=tmp_db,
        **{**defaults, **agents},
        execution_mode=ExecutionMode.IMPLEMENTATION,
    )


def workspace(tmp_path) -> Workspace:
    return Workspace(
        repo_path=tmp_path, base_ref="main", base_sha="a" * 40, branch="mat/r", path=tmp_path
    )


async def test_rejected_review_pauses_without_calling_the_gate(tmp_db, tmp_path):
    gate = AsyncMock()
    orchestrator = stub_orchestrator(tmp_db, reviewer=ReviewerStub(approve_on_turn=99), gate=gate)

    status = await orchestrator.run("x", run_id="r", workspace=workspace(tmp_path))
    assert status == RunStatus.AWAITING_HUMAN
    assert await orchestrator.resume("r") == RunStatus.AWAITING_HUMAN
    gate.check.assert_not_awaited()
    pause = await tmp_db.fetch_one(
        "SELECT content FROM evidence WHERE predicate = 'validation_failed';"
    )
    assert "Corretor" in json.loads(pause["content"])["reason"]


async def test_verifier_approval_never_marks_the_run_passed(tmp_db, tmp_path):
    orchestrator = stub_orchestrator(tmp_db)
    await orchestrator.run("x", run_id="r", workspace=workspace(tmp_path))

    assert await orchestrator.resume("r") == RunStatus.AWAITING_HUMAN
    run = await tmp_db.fetch_one("SELECT status FROM runs;")
    assert run["status"] == "awaiting_human"
    predicates = [r["predicate"] for r in await tmp_db.fetch_all("SELECT predicate FROM evidence;")]
    assert "final_review" in predicates
    assert "agent_failure_escalated" not in predicates


async def test_failing_tester_reaches_the_correction_round(tmp_db, tmp_path):
    orchestrator = stub_orchestrator(tmp_db, tester_happy=TesterHappyStub(passed=False))
    await orchestrator.run("x", run_id="r", workspace=workspace(tmp_path))

    assert await orchestrator.resume("r") == RunStatus.AWAITING_HUMAN
    agents = [
        r["agent_type"]
        for r in await tmp_db.fetch_all("SELECT agent_type FROM dispatches ORDER BY rowid;")
    ]
    assert agents.count("tester_happy") == 2
    assert agents.count("programmer") == 2
    pause = await tmp_db.fetch_one(
        "SELECT content FROM evidence WHERE predicate = 'validation_failed';"
    )
    assert "Verificador reprovou" in json.loads(pause["content"])["reason"]


def test_implementation_set_wires_every_agent(tmp_path, mat_config):
    agents = build_implementation_agents(
        load_config(),
        worktree_path=tmp_path,
        base_sha="a" * 40,
        llm=AsyncMock(spec=LLMClient),
        runner=AsyncMock(),
    )

    assert agents.execution_mode is ExecutionMode.IMPLEMENTATION
    assert isinstance(agents.analyst, AnalystAgent)
    assert isinstance(agents.programmer, ProgrammerAgent)
    assert isinstance(agents.reviewer, ReviewerAgent)
    assert isinstance(agents.gate, DeterministicGate)
    assert isinstance(agents.tester_happy, TesterHappyAgent)
    assert isinstance(agents.verifier, VerifierAgent)
    assert agents.programmer.profile.model == "modelo-programmer"
    assert agents.verifier.profile.model == "modelo-verifier"


# ── Schema v5 ────────────────────────────────────────────────────────────────


async def test_v4_runs_migrate_to_accept_implementation(tmp_path):
    path = tmp_path / "v4.db"
    v4_check = "CHECK (execution_mode IN ('unknown', 'simulation', 'planning', 'real'))"
    with sqlite3.connect(path) as raw:
        raw.executescript(SCHEMA_SQL.replace(EXECUTION_MODE_CHECK, v4_check))
        raw.execute("INSERT INTO runs (id, execution_mode) VALUES ('r1', 'planning');")
        raw.execute("PRAGMA user_version = 4;")
    raw.close()

    db = Database(path)
    await db.init_schema()
    try:
        assert (await db.fetch_one("PRAGMA user_version;"))[0] == SCHEMA_VERSION
        await db.execute("INSERT INTO runs (id, execution_mode) VALUES ('r2', 'implementation');")
        modes = [r[0] for r in await db.fetch_all("SELECT execution_mode FROM runs ORDER BY id;")]
        assert modes == ["planning", "implementation"]
    finally:
        await db.close()
