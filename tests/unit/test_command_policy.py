"""Política de comandos: validação conhecida roda; o resto pede aprovação (D42/D64)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from mat.agents.loop import AgentLoop
from mat.agents.stubs import (
    AnalystStub,
    GateStub,
    ProgrammerStub,
    ReviewerStub,
    TesterHappyStub,
    VerifierStub,
)
from mat.cli import cmd_resume, cmd_run
from mat.engine.orchestrator import PipelineOrchestrator
from mat.engine.state_machine import ExecutionMode, RunStatus
from mat.factory import build_command_policy
from mat.models.contracts import Workspace
from mat.project import list_branches
from mat.sandbox.docker_runner import CommandResult
from mat.settings import load_config
from mat.tools.bash_executor import BashHandler
from mat.tools.command_policy import CommandApprovalRequired, CommandPolicy
from tests.unit.test_implementation import (
    DONE,
    PLAN,
    Block,
    answer,
    query,
    review,
    sandbox,
    scripted_llm,
    verdict,
)

# ── Classificação ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q",
        "ruff check .",
        "python -m pytest tests/ -x",
        "python3 -m ruff format --check .",
        "ls -la && cat app.py | grep ping",
        "pytest 2>&1 | tail -20",
        "pytest > /dev/null; echo ok",
        "find . -name '*.py'",
        "grep -rn 'def ' src;",
    ],
)
def test_known_validation_and_reading_run_without_approval(command):
    assert CommandPolicy().check(command).allowed


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("pip install requests", "`pip`"),
        ("rm -rf .", "`rm`"),
        ("python -c 'import os'", "python só"),
        ("python script.py", "python só"),
        ("echo $(id)", "substituição de comando"),
        ("echo `id`", "substituição de comando"),
        ("cat <<EOF\nx\nEOF", "here-doc"),
        ("cat a.py > b.py", "redireciona"),
        ("echo x >> notes.txt", "redireciona"),
        ("FOO=1 pytest", "variável de ambiente"),
        ("find . -name x -delete", "find com ação"),
        ("find . -exec rm {} ;", "find com ação"),
        ("ls; bash -c 'rm x'", "`bash`"),
        ("pytest && git status", "`git`"),
        ("echo 'sem fim", "interpretar"),
        ("", "vazio"),
    ],
)
def test_other_commands_need_approval(command, reason):
    check = CommandPolicy().check(command)
    assert not check.allowed
    assert reason in check.reason


def test_exact_approval_allows_only_that_command():
    policy = CommandPolicy(approved={"pip list"})
    assert policy.check("  pip list ").allowed
    assert not policy.check("pip list --outdated").allowed


def test_config_extends_the_policy(tmp_path):
    config = load_config()
    config.mat.commands.approved = ["make lint"]
    config.mat.commands.allowed_programs = ["pytest"]
    policy = build_command_policy(config)

    assert policy.check("make lint").allowed
    assert policy.check("pytest").allowed
    assert not policy.check("ruff check .").allowed


# ── Ferramenta bash e laço do agente ─────────────────────────────────────────


async def test_bash_handler_raises_for_unknown_commands_and_never_runs_them(tmp_path):
    runner = AsyncMock()
    handler = BashHandler(tmp_path, runner)

    with pytest.raises(CommandApprovalRequired) as excinfo:
        await handler.execute("pip install requests")

    assert excinfo.value.command == "pip install requests"
    runner.run_command.assert_not_awaited()


async def test_bash_handler_skips_denied_commands_without_pausing(tmp_path):
    runner = AsyncMock()
    handler = BashHandler(tmp_path, runner, CommandPolicy(denied={"pip install requests"}))

    out = await handler.execute("pip install requests")

    assert "NÃO EXECUTADO" in out
    runner.run_command.assert_not_awaited()


async def test_bash_handler_runs_allowed_commands(tmp_path):
    runner = AsyncMock()
    runner.run_command.return_value = CommandResult(exit_code=0, stdout="1 passed", stderr="")

    out = await BashHandler(tmp_path, runner).execute("pytest -q")

    assert "1 passed" in out
    runner.run_command.assert_awaited_once_with(tmp_path, ["bash", "-c", "pytest -q"])


async def test_agent_loop_lets_the_approval_request_escape():
    llm = scripted_llm(
        answer(
            Block("tool_use", id="t1", name="bash", input={"command": "pip install x"}),
            stop="tool_use",
        )
    )

    async def bash(command: str) -> str:
        raise CommandApprovalRequired(command, "não conhecido")

    loop = AgentLoop(llm, "m", "s", tools=[], tool_dispatchers={"bash": bash})
    with pytest.raises(CommandApprovalRequired):
        await loop.run("faça")


# ── Orquestrador ─────────────────────────────────────────────────────────────


class AskingProgrammer(ProgrammerStub):
    """Pede ``pip install x`` na primeira chamada; depois implementa normalmente."""

    def __init__(self, policy: CommandPolicy) -> None:
        super().__init__()
        self.policy = policy
        self.feedbacks: list[str] = []

    async def implement(self, plan, *, task_description="", feedback="", budget=None):
        self.feedbacks.append(feedback)
        if len(self.feedbacks) == 1:
            if budget is not None:
                budget.record_tokens(50)
            raise CommandApprovalRequired("pip install x", "`pip` não está entre os comandos")
        return await super().implement(
            plan, task_description=task_description, feedback=feedback, budget=budget
        )


def orchestrator(tmp_db, programmer, policy, mode=ExecutionMode.IMPLEMENTATION):
    return PipelineOrchestrator(
        config=load_config(),
        db=tmp_db,
        analyst=AnalystStub(),
        programmer=programmer,
        reviewer=ReviewerStub(),
        tester_happy=TesterHappyStub(),
        verifier=VerifierStub(),
        gate=GateStub(),
        execution_mode=mode,
        command_policy=policy,
    )


def workspace(tmp_path: Path) -> Workspace:
    return Workspace(
        repo_path=tmp_path, base_ref="main", base_sha="a" * 40, branch="mat/r", path=tmp_path
    )


async def predicates(tmp_db) -> list[str]:
    rows = await tmp_db.fetch_all("SELECT predicate FROM evidence ORDER BY created_at, rowid;")
    return [r["predicate"] for r in rows]


@pytest.mark.parametrize("approve", [True, False])
async def test_command_request_pauses_and_the_decision_continues_the_attempt(
    tmp_db, tmp_path, approve
):
    policy = CommandPolicy()
    programmer = AskingProgrammer(policy)
    orch = orchestrator(tmp_db, programmer, policy)
    await orch.run("x", run_id="r", workspace=workspace(tmp_path))

    assert await orch.resume("r") == RunStatus.AWAITING_HUMAN
    assert (await predicates(tmp_db))[-1] == "command_approval"
    attempt = await tmp_db.fetch_one("SELECT status, tokens_used FROM attempts;")
    assert (attempt["status"], attempt["tokens_used"]) == ("escalated", 50)

    # Uma orquestração nova (como a CLI faz) recebe a decisão pelo checkpoint.
    fresh_policy = CommandPolicy()
    orch = orchestrator(tmp_db, programmer, fresh_policy)
    assert await orch.resume("r", approve=approve, reason="ok") == RunStatus.AWAITING_HUMAN

    decided = fresh_policy.approved if approve else fresh_policy.denied
    assert "pip install x" in decided
    note = programmer.feedbacks[-1]
    assert "`pip install x`" in note
    assert ("aprovou" if approve else "recusou") in note
    evidence = await predicates(tmp_db)
    assert ("command_approved" if approve else "command_rejected") in evidence
    assert evidence[-1] == "final_review"
    # Pedir aprovação não gasta a rodada de correção.
    assert "validation_correction" not in evidence
    attempts = await tmp_db.fetch_all("SELECT status, tokens_used FROM attempts;")
    assert [a["status"] for a in attempts] == ["passed"]
    assert attempts[0]["tokens_used"] >= 50


async def test_simulation_does_not_pause_for_commands(tmp_db):
    policy = CommandPolicy()
    orch = orchestrator(tmp_db, AskingProgrammer(policy), policy, mode=ExecutionMode.SIMULATION)
    assert await orch.run("x", run_id="r") == RunStatus.AWAITING_HUMAN
    evidence = await predicates(tmp_db)
    assert "command_approval" not in evidence
    assert "agent_failure_escalated" in evidence


# ── CLI de ponta a ponta ─────────────────────────────────────────────────────

ASK_PIP = answer(
    Block("tool_use", id="t1", name="bash", input={"command": "pip install requests"}),
    stop="tool_use",
)


@pytest.mark.parametrize("reject", [False, True])
async def test_cli_shows_the_command_and_resumes(repo, mat_config, capsys, reject):
    sha = (await list_branches(repo))[0].sha
    llm = scripted_llm(PLAN, ASK_PIP, DONE, review(True), verdict(True))
    runner = sandbox()
    assert await cmd_run("x", repo=str(repo), base="main", base_sha=sha, llm=llm, runner=runner) == 0
    run_id = (await query(mat_config, "SELECT id FROM runs;"))[0]["id"]

    capsys.readouterr()
    assert await cmd_resume(run_id, llm=llm, runner=runner) == 0
    out = capsys.readouterr().out
    assert "pip install requests" in out
    assert "--reject" in out
    runner.run_command.assert_not_awaited()

    assert await cmd_resume(run_id, reject=reject, llm=llm, runner=runner) == 0
    evidence = [r["predicate"] for r in await query(mat_config, "SELECT predicate FROM evidence;")]
    assert ("command_rejected" if reject else "command_approved") in evidence
    assert evidence[-1] == "final_review"
