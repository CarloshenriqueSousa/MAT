"""Modo planning: Analista real, aprovação obrigatória e nenhuma edição (passo 5, D59)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mat.agents.analyst import AnalystAgent
from mat.agents.errors import AgentExecutionError
from mat.cli import cmd_report, cmd_resume, cmd_run
from mat.db.connection import Database
from mat.db.schema import EXECUTION_MODE_CHECK, SCHEMA_SQL, SCHEMA_VERSION
from mat.engine.orchestrator import PipelineOrchestrator
from mat.engine.state_machine import ExecutionMode
from mat.factory import (
    AgentConfigError,
    NotWiredAgent,
    build_planning_agents,
    build_unwired_agents,
)
from mat.project import list_branches
from mat.settings import load_config
from tests.conftest import PLAN_JSON, git, llm_answering


async def start(repo: Path, llm, runner) -> int:
    sha = (await list_branches(repo))[0].sha
    return await cmd_run(
        "Adicionar /ping",
        repo=str(repo),
        base="main",
        base_sha=sha,
        plan_only=True,
        llm=llm,
        runner=runner,
    )


async def only_run(mat_config: Path) -> dict:
    db = Database(mat_config / "mat.db")
    run = await db.fetch_one("SELECT * FROM runs;")
    await db.close()
    assert run is not None
    return dict(run)


async def predicates(mat_config: Path, run_id: str) -> list[str]:
    db = Database(mat_config / "mat.db")
    rows = await db.fetch_all(
        "SELECT predicate FROM evidence WHERE run_id = ? ORDER BY created_at, rowid;", (run_id,)
    )
    await db.close()
    return [row["predicate"] for row in rows]


# ── CLI ──────────────────────────────────────────────────────────────────────


async def test_planning_run_pauses_with_plan_and_leaves_worktree_untouched(
    repo, mat_config, fake_llm, fake_runner, capsys
):
    assert await start(repo, fake_llm, fake_runner) == 0

    run = await only_run(mat_config)
    assert (run["status"], run["current_phase"]) == ("awaiting_human", "awaiting_human")
    assert run["execution_mode"] == "planning"
    assert run["plan_count"] == 1
    assert run["total_attempts"] == 0
    # O Analista usa o perfil de agents.yaml e mede o baseline no sandbox.
    assert fake_llm.call.await_args.kwargs["model"] == "modelo-analyst"
    fake_runner.run_pytest.assert_awaited_once()
    # Nada foi editado: o worktree segue idêntico à base.
    assert git(Path(run["worktree_path"]), "status", "--porcelain") == ""
    assert "plan_created" in await predicates(mat_config, run["id"])

    capsys.readouterr()
    assert await cmd_report(run["id"]) == 0
    out = capsys.readouterr().out
    assert "ping_ok" in out
    assert "tests/test_ping.py" in out
    # Texto do LLM é exibido literalmente, nunca interpretado como markup.
    assert "[bold]" in out


async def test_approving_a_planning_plan_records_it_without_executing(
    repo, mat_config, fake_llm, fake_runner
):
    await start(repo, fake_llm, fake_runner)
    run = await only_run(mat_config)

    assert await cmd_resume(run["id"], reason="ok") == 0

    after = await only_run(mat_config)
    assert (after["status"], after["current_phase"]) == ("awaiting_human", "awaiting_human")
    assert after["total_attempts"] == 0
    evidence = await predicates(mat_config, run["id"])
    assert evidence[-2:] == ["plan_approved", "execution_unavailable"]
    assert fake_llm.call.await_count == 1
    assert git(Path(run["worktree_path"]), "status", "--porcelain") == ""
    # A pausa seguinte não é uma aprovação de plano: não há nada a retomar.
    assert await cmd_resume(run["id"]) == 1


async def test_rejecting_a_planning_plan_cancels_the_run(repo, mat_config, fake_llm, fake_runner):
    await start(repo, fake_llm, fake_runner)
    run = await only_run(mat_config)

    assert await cmd_resume(run["id"], reject=True, reason="escopo errado") == 0

    assert (await only_run(mat_config))["status"] == "cancelled"
    assert (await predicates(mat_config, run["id"]))[-1] == "plan_rejected"


async def test_ambiguous_plan_pauses_and_is_not_approvable(repo, mat_config, fake_runner):
    llm = llm_answering({**PLAN_JSON, "is_ambiguous": True, "ambiguity_reason": "qual rota?"})

    assert await start(repo, llm, fake_runner) == 0

    run = await only_run(mat_config)
    assert run["status"] == "awaiting_human"
    assert "plan_ambiguous" in await predicates(mat_config, run["id"])
    assert await cmd_resume(run["id"]) == 1


async def test_missing_api_key_fails_before_creating_anything(repo, mat_config):
    sha = (await list_branches(repo))[0].sha

    assert await cmd_run("x", repo=str(repo), base="main", base_sha=sha) == 1

    assert not (mat_config / "worktrees").exists()
    assert "mat/" not in git(repo, "branch", "--list")


async def test_missing_analyst_profile_fails_before_creating_anything(repo, mat_config, fake_llm):
    (mat_config / "configs" / "agents.yaml").write_text("agents: {}\n", encoding="utf-8")
    sha = (await list_branches(repo))[0].sha

    assert await cmd_run("x", repo=str(repo), base="main", base_sha=sha, llm=fake_llm) == 1

    assert not (mat_config / "worktrees").exists()


# ── Orquestrador e fábrica ───────────────────────────────────────────────────


async def test_planning_mode_always_supervised_and_needs_a_worktree(tmp_db, mat_config):
    orchestrator = PipelineOrchestrator(
        config=load_config(),
        db=tmp_db,
        supervised=False,
        **build_unwired_agents(ExecutionMode.PLANNING).orchestrator_kwargs(),
    )

    assert orchestrator.supervised is True
    with pytest.raises(ValueError, match="worktree"):
        await orchestrator.run("x", base_sha="a" * 40)


def test_planning_set_has_only_the_analyst_wired(tmp_path, mat_config, fake_llm, fake_runner):
    agents = build_planning_agents(
        load_config(), worktree_path=tmp_path, base_sha="a" * 40, llm=fake_llm, runner=fake_runner
    )

    assert agents.execution_mode is ExecutionMode.PLANNING
    assert isinstance(agents.analyst, AnalystAgent)
    for role in ("programmer", "reviewer", "tester_happy", "verifier", "gate"):
        assert isinstance(getattr(agents, role), NotWiredAgent)
    with pytest.raises(AgentConfigError):
        build_planning_agents(load_config(), worktree_path=tmp_path, base_sha="main", llm=fake_llm)


def test_not_wired_agent_refuses_every_call():
    agent = NotWiredAgent("programmer", ExecutionMode.PLANNING)

    with pytest.raises(AgentExecutionError, match="programmer") as exc:
        agent.implement()
    assert exc.value.retry_recommended is False


# ── Schema v4 ────────────────────────────────────────────────────────────────


async def test_v3_runs_migrate_to_accept_planning(tmp_path):
    path = tmp_path / "v3.db"
    old_check = "CHECK (execution_mode IN ('unknown', 'simulation', 'real'))"
    with sqlite3.connect(path) as raw:
        raw.executescript(SCHEMA_SQL.replace(EXECUTION_MODE_CHECK, old_check))
        raw.execute(
            "INSERT INTO runs (id, status, execution_mode) VALUES ('r1', 'passed', 'real');"
        )
        raw.execute("INSERT INTO plans (id, run_id, plan_index) VALUES ('p1', 'r1', 1);")
        raw.execute("PRAGMA user_version = 3;")
    raw.close()

    db = Database(path)
    await db.init_schema()
    try:
        assert (await db.fetch_one("PRAGMA user_version;"))[0] == SCHEMA_VERSION
        assert (await db.fetch_one("SELECT execution_mode FROM runs WHERE id = 'r1';"))[0] == "real"
        assert (await db.fetch_one("SELECT run_id FROM plans WHERE id = 'p1';"))[0] == "r1"
        await db.execute("INSERT INTO runs (id, execution_mode) VALUES ('r2', 'planning');")
        assert await db.fetch_all("PRAGMA foreign_key_check;") == []
        # Migrar de novo é inócuo.
        await db.init_schema()
        assert (await db.fetch_one("SELECT COUNT(*) FROM runs;"))[0] == 2
        # As chaves estrangeiras continuam valendo após a troca da tabela.
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute("INSERT INTO plans (id, run_id, plan_index) VALUES ('p2', 'nada', 1);")
    finally:
        await db.close()
