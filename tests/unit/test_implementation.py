"""Modo implementation: Programador, Corretor e gate reais após aprovar o plano (D61)."""

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
from mat.agents.stubs import AnalystStub, GateStub, ProgrammerStub, ReviewerStub
from mat.cli import cmd_report, cmd_resume, cmd_run
from mat.db.connection import Database
from mat.db.schema import EXECUTION_MODE_CHECK, SCHEMA_SQL, SCHEMA_VERSION
from mat.engine.orchestrator import PipelineOrchestrator
from mat.engine.state_machine import ExecutionMode, RunStatus
from mat.factory import NotWiredAgent, build_implementation_agents
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


def sandbox(*, gate_ok: bool = True) -> AsyncMock:
    """Sandbox falso: baseline verde e gate conforme ``gate_ok``."""
    runner = AsyncMock()
    green = CommandResult(exit_code=0, stdout="collected 2 items\n2 passed in 0.01s\n", stderr="")
    red = CommandResult(exit_code=1, stdout="1 failed, 1 passed\n", stderr="")
    runner.run_pytest.side_effect = [green, green if gate_ok else red]
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


async def test_approved_plan_is_implemented_reviewed_gated_and_paused(repo, mat_config, capsys):
    llm = scripted_llm(PLAN, CREATE_PING, DONE, review(True))
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
    assert (after["status"], after["current_phase"]) == ("awaiting_human", "awaiting_human")
    # O Programador editou só o worktree, sem commit; o checkout original segue intacto.
    assert (worktree / "ping.py").read_text(encoding="utf-8").startswith("def ping")
    assert git(worktree, "rev-parse", "HEAD") == sha
    assert "ping.py" in git(worktree, "status", "--porcelain")
    assert not (repo / "ping.py").exists()

    attempts = await query(mat_config, "SELECT status, tokens_used FROM attempts;")
    assert [(a["status"], a["tokens_used"] > 0) for a in attempts] == [("escalated", True)]
    agents = [
        r["agent_type"]
        for r in await query(mat_config, "SELECT agent_type FROM tasks ORDER BY created_at, rowid;")
    ]
    assert agents == ["analyst", "programmer", "reviewer", "gate"]
    pause = await query(
        mat_config, "SELECT content FROM evidence WHERE predicate = 'validation_unavailable';"
    )
    assert json.loads(pause[0]["content"])["modified_files"] == ["ping.py"]
    assert "ping.py" in out
    assert "Tester e Verificador" in out
    # A pausa após o gate não é retomável.
    assert await cmd_resume(run["id"], llm=llm, runner=runner) == 1
    assert await cmd_report(run["id"]) == 0


async def test_failing_gate_fails_the_attempt(repo, mat_config):
    llm = scripted_llm(PLAN, CREATE_PING, DONE, review(True))
    runner = sandbox(gate_ok=False)
    await start(repo, llm, runner)
    run_id = (await query(mat_config, "SELECT id FROM runs;"))[0]["id"]

    assert await cmd_resume(run_id, llm=llm, runner=runner) == 1

    run = (await query(mat_config, "SELECT status FROM runs;"))[0]
    attempt = (await query(mat_config, "SELECT status, failure_reason FROM attempts;"))[0]
    assert run["status"] == "failed"
    assert attempt["status"] == "failed"
    assert "Gate determinístico falhou" in attempt["failure_reason"]


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


async def test_rejected_review_fails_without_calling_the_gate(tmp_db, tmp_path):
    gate = AsyncMock()
    orchestrator = PipelineOrchestrator(
        config=load_config(),
        db=tmp_db,
        analyst=AnalystStub(),
        programmer=ProgrammerStub(),
        reviewer=ReviewerStub(approve_on_turn=99),
        tester_happy=NotWiredAgent("tester_happy", ExecutionMode.IMPLEMENTATION),
        verifier=NotWiredAgent("verifier", ExecutionMode.IMPLEMENTATION),
        gate=gate,
        execution_mode=ExecutionMode.IMPLEMENTATION,
    )
    workspace = Workspace(
        repo_path=tmp_path, base_ref="main", base_sha="a" * 40, branch="mat/r", path=tmp_path
    )

    assert await orchestrator.run("x", run_id="r", workspace=workspace) == RunStatus.AWAITING_HUMAN
    assert await orchestrator.resume("r") == RunStatus.FAILED
    gate.check.assert_not_awaited()


async def test_gate_pass_never_reaches_tester_or_verifier(tmp_db, tmp_path):
    orchestrator = PipelineOrchestrator(
        config=load_config(),
        db=tmp_db,
        analyst=AnalystStub(),
        programmer=ProgrammerStub(),
        reviewer=ReviewerStub(),
        tester_happy=NotWiredAgent("tester_happy", ExecutionMode.IMPLEMENTATION),
        verifier=NotWiredAgent("verifier", ExecutionMode.IMPLEMENTATION),
        gate=GateStub(passed=True),
        execution_mode=ExecutionMode.IMPLEMENTATION,
    )
    workspace = Workspace(
        repo_path=tmp_path, base_ref="main", base_sha="a" * 40, branch="mat/r", path=tmp_path
    )
    await orchestrator.run("x", run_id="r", workspace=workspace)

    assert await orchestrator.resume("r") == RunStatus.AWAITING_HUMAN
    predicates = [r["predicate"] for r in await tmp_db.fetch_all("SELECT predicate FROM evidence;")]
    assert "validation_unavailable" in predicates
    assert "agent_failure_escalated" not in predicates


def test_implementation_set_wires_programmer_reviewer_and_gate(tmp_path, mat_config):
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
    assert isinstance(agents.tester_happy, NotWiredAgent)
    assert isinstance(agents.verifier, NotWiredAgent)
    assert agents.programmer.profile.model == "modelo-programmer"


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
