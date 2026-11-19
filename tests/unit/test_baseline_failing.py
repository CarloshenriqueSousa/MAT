"""Baseline já falhando pausa antes de editar e antes de gastar tokens (D43, D60)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from mat.agents.analyst import AnalystAgent
from mat.agents.errors import BaselineFailingError
from mat.cli import cmd_report, cmd_resume, cmd_run
from mat.db.connection import Database
from mat.gate.baseline import BaselineMeasurer
from mat.models.contracts import Baseline
from mat.project import list_branches
from mat.sandbox.docker_runner import CommandResult
from mat.settings import AgentProfile


def baseline(**fields) -> Baseline:
    return Baseline(id="b", run_id="r", **fields)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, []),  # sem medição no sandbox (simulação/legado)
        ({"pytest_exit_code": 0}, []),
        ({"pytest_exit_code": 5}, []),  # nenhum teste coletado ainda
        ({"pytest_exit_code": 1}, ["pytest saiu com 1"]),
        ({"pytest_exit_code": 4}, ["pytest saiu com 4"]),
        ({"pytest_exit_code": 0, "pytest_timed_out": True}, ["tempo limite"]),
        ({"pytest_exit_code": 0, "ruff_clean": False}, ["Ruff"]),
        ({"pytest_exit_code": 2, "ruff_clean": False}, ["pytest saiu com 2", "Ruff"]),
    ],
)
def test_failure_reasons(fields, expected):
    reasons = baseline(**fields).failure_reasons()

    assert len(reasons) == len(expected)
    for reason, fragment in zip(reasons, expected, strict=True):
        assert fragment in reason


def runner_with(pytest_exit: int, ruff_exit: int = 0, *, timed_out: bool = False) -> AsyncMock:
    runner = AsyncMock()
    runner.run_pytest.return_value = CommandResult(
        exit_code=pytest_exit,
        stdout="collected 3 items\n1 failed, 2 passed in 0.1s\nFAILED tests/test_x.py::t\n",
        stderr="",
        timed_out=timed_out,
    )
    runner.run_ruff.return_value = CommandResult(
        exit_code=ruff_exit, stdout="app.py:1:1: F401 unused" if ruff_exit else "", stderr=""
    )
    return runner


async def test_measurer_records_exit_codes(tmp_path):
    measured = await BaselineMeasurer(runner_with(1, 1)).measure("r", tmp_path)

    assert measured.pytest_exit_code == 1
    assert measured.ruff_clean is False
    assert "F401" in measured.ruff_output
    assert len(measured.failure_reasons()) == 2


async def test_analyst_stops_before_calling_the_llm(tmp_path):
    llm = AsyncMock()
    analyst = AnalystAgent(
        llm=llm,
        profile=AgentProfile(model="m", temperature=0.1, max_tokens=10),
        worktree_path=tmp_path,
        baseline_measurer=BaselineMeasurer(runner_with(1)),
    )

    with pytest.raises(BaselineFailingError) as exc:
        await analyst.analyze("r", 1, "x")

    assert exc.value.retry_recommended is False
    assert exc.value.baseline.pytest_exit_code == 1
    llm.call.assert_not_awaited()


async def test_cli_pauses_on_failing_baseline(repo, mat_config, fake_llm, capsys):
    sha = (await list_branches(repo))[0].sha

    code = await cmd_run(
        "Adicionar /ping",
        repo=str(repo),
        base="main",
        base_sha=sha,
        llm=fake_llm,
        runner=runner_with(1),
    )

    assert code == 0
    fake_llm.call.assert_not_awaited()
    out = capsys.readouterr().out
    assert "a base já falha" in out
    assert "mat resume" not in out

    db = Database(mat_config / "mat.db")
    run = await db.fetch_one("SELECT * FROM runs;")
    evidence = await db.fetch_one(
        "SELECT agent_type, content FROM evidence WHERE predicate = 'baseline_failing';"
    )
    saved = await db.fetch_one("SELECT pytest_passed, pytest_total FROM baselines;")
    plans = await db.fetch_all("SELECT id FROM plans;")
    await db.close()

    assert (run["status"], run["execution_mode"]) == ("awaiting_human", "implementation")
    assert evidence["agent_type"] == "analyst"
    content = json.loads(evidence["content"])
    assert content["pytest_exit_code"] == 1
    assert "FAILED tests/test_x.py::t" in content["pytest_output_tail"]
    assert tuple(saved) == (2, 3)
    assert plans == []
    # Nada foi editado no worktree.
    assert Path(run["worktree_path"], "app.py").read_text(encoding="utf-8") == "print('v1')\n"

    # Não é uma aprovação de plano: `mat resume` recusa.
    assert await cmd_resume(run["id"]) == 1

    capsys.readouterr()
    assert await cmd_report(run["id"]) == 0
    report = capsys.readouterr().out
    assert "baseline_failing" in report
    assert "FAILED tests/test_x.py::t" in report
