"""Erro fora do escopo vira proposta de remediação em worktree separado (passo 9, D67)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

from mat.agents.analyst import AnalystAgent
from mat.cli import cmd_remediate, cmd_resume, cmd_run
from mat.engine.orchestrator import remediation_task
from mat.gate.baseline import BaselineMeasurer
from mat.project import list_branches
from mat.sandbox.docker_runner import CommandResult
from mat.settings import AgentProfile
from tests.conftest import git
from tests.unit.test_baseline_failing import runner_with
from tests.unit.test_implementation import (
    PLAN,
    Block,
    answer,
    query,
    scripted_llm,
)

RED_BASE = CommandResult(
    exit_code=1, stdout="collected 3 items\n1 failed, 2 passed\nFAILED tests/test_x.py::t\n", stderr=""
)


def test_remediation_task_asks_only_for_the_base_fix():
    task = remediation_task("main", "a" * 40, ["pytest saiu com 1"], "Adicionar /ping")
    assert "`main` (aaaaaaaaaaaa)" in task
    assert "pytest saiu com 1" in task
    assert "Não implemente a tarefa original" in task
    assert task.endswith("Adicionar /ping")


async def test_remediation_analyst_plans_over_a_failing_base(tmp_path):
    llm = scripted_llm(PLAN)
    analyst = AnalystAgent(
        llm=llm,
        profile=AgentProfile(model="m", temperature=0.1, max_tokens=10),
        worktree_path=tmp_path,
        baseline_measurer=BaselineMeasurer(runner_with(1)),
        allow_failing_baseline=True,
    )

    plan, baseline = await analyst.analyze("r", 1, "corrigir a base")

    assert baseline.pytest_exit_code == 1
    prompt = llm.call.await_args.kwargs["messages"][0]["content"]
    assert "A base já falha" in prompt
    assert "FAILED tests/test_x.py::t" in prompt
    assert plan.approach


async def failing_run(repo: Path, mat_config: Path) -> tuple[str, str]:
    sha = (await list_branches(repo))[0].sha
    llm = AsyncMock()
    assert await cmd_run(
        "Adicionar /ping", repo=str(repo), base="main", base_sha=sha, llm=llm, runner=runner_with(1)
    ) == 0
    llm.call.assert_not_awaited()
    run_id = (await query(mat_config, "SELECT id FROM runs;"))[0]["id"]
    return run_id, sha


async def test_failing_base_records_a_proposal(repo, mat_config, capsys):
    capsys.readouterr()
    run_id, sha = await failing_run(repo, mat_config)
    out = capsys.readouterr().out

    assert "mat remediate" in out  # o painel pode quebrar a linha antes do id
    assert run_id in out
    row = (
        await query(
            mat_config, "SELECT content FROM evidence WHERE predicate = 'remediation_proposed';"
        )
    )[0]
    proposal = json.loads(row["content"])
    assert (proposal["base_ref"], proposal["base_sha"]) == ("main", sha)
    assert "Adicionar /ping" in proposal["task"]
    # Nenhuma run de remediação é criada sem aceite.
    assert len(await query(mat_config, "SELECT id FROM runs;")) == 1


async def test_accepting_creates_a_separate_worktree_from_the_observed_base(
    repo, mat_config, capsys
):
    source, sha = await failing_run(repo, mat_config)
    llm = scripted_llm(PLAN)

    capsys.readouterr()
    runner = AsyncMock()
    runner.run_pytest.return_value = RED_BASE
    runner.run_ruff.return_value = CommandResult(exit_code=0, stdout="", stderr="")
    assert await cmd_remediate(source, llm=llm, runner=runner) == 0
    out = capsys.readouterr().out

    runs = {
        r["id"]: r
        for r in await query(
            mat_config, "SELECT id, status, base_sha, worktree_path, work_branch FROM runs;"
        )
    }
    assert runs[source]["status"] == "cancelled"
    [remediation] = [r for r in runs.values() if r["id"] != source]
    # Base observada, worktree e branch próprios; o plano aguarda aprovação.
    assert remediation["base_sha"] == sha
    assert remediation["status"] == "awaiting_human"
    assert remediation["worktree_path"] != runs[source]["worktree_path"]
    assert remediation["work_branch"] == f"mat/{remediation['id']}"
    assert git(Path(remediation["worktree_path"]), "rev-parse", "HEAD") == sha
    assert "Proposta de remediação aceita" in out

    links = await query(
        mat_config,
        "SELECT run_id, predicate, content FROM evidence "
        "WHERE predicate IN ('remediation_of', 'remediation_accepted');",
    )
    by_predicate = {r["predicate"]: r for r in links}
    assert by_predicate["remediation_of"]["run_id"] == remediation["id"]
    assert json.loads(by_predicate["remediation_of"]["content"])["source_run_id"] == source
    assert by_predicate["remediation_accepted"]["run_id"] == source
    # A remediação não pausou por baseline_failing: o Analista planejou sobre a base quebrada.
    llm.call.assert_awaited_once()
    assert "A base já falha" in llm.call.await_args.kwargs["messages"][0]["content"]


async def test_remediate_requires_a_pending_proposal(repo, mat_config):
    assert await cmd_remediate("nada") == 1
    source, _ = await failing_run(repo, mat_config)
    runner = AsyncMock()
    runner.run_pytest.return_value = RED_BASE
    runner.run_ruff.return_value = CommandResult(exit_code=0, stdout="", stderr="")
    assert await cmd_remediate(source, llm=scripted_llm(PLAN), runner=runner) == 0
    # A origem foi encerrada: a proposta não pode ser aceita de novo.
    assert await cmd_remediate(source, llm=scripted_llm(PLAN), runner=runner) == 1


async def test_remediation_run_keeps_accepting_the_failing_base_on_resume(repo, mat_config):
    """Retomar a análise de uma remediação (D66) continua aceitando a base quebrada."""
    source, _ = await failing_run(repo, mat_config)
    ambiguous = scripted_llm(
        _json_answer({"is_ambiguous": True, "ambiguity_reason": "qual teste?"}), PLAN
    )
    runner = AsyncMock()
    runner.run_pytest.return_value = RED_BASE
    runner.run_ruff.return_value = CommandResult(exit_code=0, stdout="", stderr="")
    assert await cmd_remediate(source, llm=ambiguous, runner=runner) == 0
    remediation = (await query(mat_config, f"SELECT id FROM runs WHERE id != '{source}';"))[0][
        "id"
    ]

    assert await cmd_resume(remediation, guidance="o test_x", llm=ambiguous, runner=runner) == 0

    status = (
        await query(mat_config, f"SELECT status FROM runs WHERE id = '{remediation}';")
    )[0]["status"]
    assert status == "awaiting_human"
    plans = await query(mat_config, f"SELECT id FROM plans WHERE run_id = '{remediation}';")
    assert len(plans) == 1


def _json_answer(body: dict):
    return answer(Block("text", json.dumps(body)))
