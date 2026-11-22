"""Alterações em testes existentes exigem aprovação humana do conteúdo exato (D42/D63)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from mat.agents.stubs import (
    AnalystStub,
    GateStub,
    ProgrammerStub,
    ReviewerStub,
    TesterHappyStub,
    VerifierStub,
)
from mat.cli import cmd_resume, cmd_run
from mat.engine.orchestrator import PipelineOrchestrator, PipelineState
from mat.engine.state_machine import ExecutionMode, Phase, RunStatus
from mat.gate.deterministic import (
    DeterministicGate,
    find_existing_test_changes,
    is_test_file,
    split_diff,
)
from mat.models.contracts import Baseline, Implementation, TestFileChange, Workspace
from mat.project import list_branches
from mat.sandbox.docker_runner import CommandResult, DockerRunner
from mat.settings import load_config
from tests.conftest import git
from tests.unit.test_implementation import (
    DONE,
    GREEN,
    PLAN,
    Block,
    answer,
    query,
    review,
    sandbox,
    scripted_llm,
    verdict,
)

MODIFIED_TEST = """diff --git a/tests/test_app.py b/tests/test_app.py
index 1111111..2222222 100644
--- a/tests/test_app.py
+++ b/tests/test_app.py
@@ -1,2 +1,2 @@
 def test_app():
-    assert 1 == 1
+    assert 2 == 2
"""
NEW_TEST = """diff --git a/tests/test_new.py b/tests/test_new.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/tests/test_new.py
@@ -0,0 +1,2 @@
+def test_new():
+    assert True
"""
DELETED_ROOT_TEST = """diff --git a/test_root.py b/test_root.py
deleted file mode 100644
index 4444444..0000000
--- a/test_root.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def test_root():
-    assert True
"""
RENAMED_TEST = """diff --git a/pkg/tests/test_a.py b/pkg/tests/test_b.py
similarity index 100%
rename from pkg/tests/test_a.py
rename to pkg/tests/test_b.py
"""
APP_CHANGE = """diff --git a/app.py b/app.py
index 5555555..6666666 100644
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-print('v1')
+print('v2')
"""

BASELINE = Baseline(id="b", run_id="r", pytest_passed=1, pytest_total=1, coverage_pct=0.0)


# ── Classificação do diff ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_app.py", True),
        ("pkg/tests/helpers.py", True),
        ("test_root.py", True),
        ("src/foo_test.py", True),
        ("app.py", False),
        ("src/testing.py", False),
        ("tests", False),
    ],
)
def test_is_test_file(path, expected):
    assert is_test_file(path) is expected


def test_split_diff_reads_paths_of_every_kind_of_section():
    sections = split_diff(MODIFIED_TEST + NEW_TEST + DELETED_ROOT_TEST + RENAMED_TEST)
    assert [(s.old_path, s.new_path) for s in sections] == [
        ("tests/test_app.py", "tests/test_app.py"),
        (None, "tests/test_new.py"),
        ("test_root.py", None),
        ("pkg/tests/test_a.py", "pkg/tests/test_b.py"),
    ]


def test_only_tests_that_existed_need_approval():
    changes = find_existing_test_changes(
        APP_CHANGE + MODIFIED_TEST + NEW_TEST + DELETED_ROOT_TEST + RENAMED_TEST
    )
    assert [c.path for c in changes] == ["tests/test_app.py", "test_root.py", "pkg/tests/test_a.py"]
    assert all(len(c.sha256) == 64 for c in changes)
    assert "+    assert 2 == 2" in changes[0].diff


def test_skip_in_an_existing_test_is_still_a_hard_cheat():
    gate = DeterministicGate(runner=AsyncMock())
    diff = MODIFIED_TEST + "+@pytest.mark.skip\n"
    assert "trapaça" in (gate.check_anti_cheat(diff) or "")


# ── Gate ─────────────────────────────────────────────────────────────────────


def green_runner() -> AsyncMock:
    runner = AsyncMock(spec=DockerRunner)
    runner.run_ruff.return_value = CommandResult(exit_code=0, stdout="", stderr="")
    runner.run_pytest.return_value = CommandResult(exit_code=0, stdout="1 passed", stderr="")
    return runner


async def test_gate_stops_before_running_anything_when_approval_is_pending(tmp_path):
    runner = green_runner()
    res = await DeterministicGate(runner).evaluate(tmp_path, BASELINE, MODIFIED_TEST)

    assert not res.passed
    assert [c.path for c in res.pending_test_changes] == ["tests/test_app.py"]
    assert "aprovação humana" in (res.error_message or "")
    runner.run_ruff.assert_not_awaited()
    runner.run_pytest.assert_not_awaited()


async def test_approval_covers_only_the_exact_content(tmp_path):
    [change] = find_existing_test_changes(MODIFIED_TEST)
    approved = {change.path: change.sha256}
    gate = DeterministicGate(green_runner())

    assert (await gate.evaluate(tmp_path, BASELINE, MODIFIED_TEST, approved_test_changes=approved)).passed

    changed_again = MODIFIED_TEST.replace("assert 2 == 2", "assert 3 == 3")
    res = await gate.evaluate(tmp_path, BASELINE, changed_again, approved_test_changes=approved)
    assert not res.passed
    assert [c.path for c in res.pending_test_changes] == ["tests/test_app.py"]


async def test_gate_prefers_the_fresh_worktree_diff(tmp_path):
    diff_source = AsyncMock(return_value=MODIFIED_TEST)
    gate = DeterministicGate(green_runner(), worktree_path=tmp_path, diff_source=diff_source)

    # O diff entregue pelo Programador está vazio, mas o worktree mudou depois.
    res = await gate.check(BASELINE, Implementation(diff=""))

    assert not res.passed
    assert res.pending_test_changes
    diff_source.assert_awaited_once()


# ── Orquestrador ─────────────────────────────────────────────────────────────

CHANGE = TestFileChange(path="tests/test_app.py", sha256="a" * 64, diff=MODIFIED_TEST)


class RecordingProgrammer(ProgrammerStub):
    def __init__(self) -> None:
        super().__init__()
        self.feedbacks: list[str] = []

    async def implement(self, plan, *, task_description="", feedback="", budget=None):
        self.feedbacks.append(feedback)
        return await super().implement(
            plan, task_description=task_description, feedback=feedback, budget=budget
        )


def orchestrator(tmp_db, *, gate, programmer=None, mode=ExecutionMode.IMPLEMENTATION):
    return PipelineOrchestrator(
        config=load_config(),
        db=tmp_db,
        analyst=AnalystStub(),
        programmer=programmer or ProgrammerStub(),
        reviewer=ReviewerStub(),
        tester_happy=TesterHappyStub(),
        verifier=VerifierStub(),
        gate=gate,
        execution_mode=mode,
    )


def workspace(tmp_path: Path) -> Workspace:
    return Workspace(
        repo_path=tmp_path, base_ref="main", base_sha="a" * 40, branch="mat/r", path=tmp_path
    )


async def predicates(tmp_db) -> list[str]:
    rows = await tmp_db.fetch_all("SELECT predicate FROM evidence ORDER BY created_at, rowid;")
    return [r["predicate"] for r in rows]


async def test_pending_change_pauses_and_approval_continues_the_same_attempt(tmp_db, tmp_path):
    gate = GateStub(test_changes=[CHANGE])
    orch = orchestrator(tmp_db, gate=gate)
    await orch.run("x", run_id="r", workspace=workspace(tmp_path))

    assert await orch.resume("r") == RunStatus.AWAITING_HUMAN
    attempt = await tmp_db.fetch_one("SELECT status FROM attempts;")
    assert attempt["status"] == "escalated"
    pause = await tmp_db.fetch_one(
        "SELECT content FROM evidence WHERE predicate = 'test_change_approval';"
    )
    assert json.loads(pause["content"])["test_changes"][0]["path"] == "tests/test_app.py"
    # A pausa de aprovação não gasta a rodada de correção.
    assert "validation_correction" not in await predicates(tmp_db)

    assert await orch.resume("r") == RunStatus.AWAITING_HUMAN

    assert gate.approvals[-1] == {"tests/test_app.py": "a" * 64}
    attempts = await tmp_db.fetch_all("SELECT status FROM attempts;")
    assert [a["status"] for a in attempts] == ["passed"]
    evidence = await predicates(tmp_db)
    assert "test_change_approved" in evidence
    assert evidence[-1] == "final_review"
    programmer_calls = await tmp_db.fetch_all(
        "SELECT 1 FROM dispatches WHERE agent_type = 'programmer';"
    )
    assert len(programmer_calls) == 1


async def test_rejection_sends_the_attempt_back_to_the_programmer(tmp_db, tmp_path):
    gate = GateStub(test_changes=[CHANGE])
    programmer = RecordingProgrammer()
    orch = orchestrator(tmp_db, gate=gate, programmer=programmer)
    await orch.run("x", run_id="r", workspace=workspace(tmp_path))
    assert await orch.resume("r") == RunStatus.AWAITING_HUMAN

    # Recusar não cancela: o Programador recebe a recusa e o gate volta a pedir aprovação.
    assert await orch.resume("r", approve=False, reason="não mexa no teste") == (
        RunStatus.AWAITING_HUMAN
    )
    assert len(programmer.feedbacks) == 2
    assert "recusou a alteração em testes existentes" in programmer.feedbacks[1]
    assert "não mexa no teste" in programmer.feedbacks[1]
    run = await tmp_db.fetch_one("SELECT status FROM runs;")
    assert run["status"] == "awaiting_human"
    evidence = await predicates(tmp_db)
    assert evidence.count("test_change_approval") == 2
    assert "test_change_rejected" in evidence
    assert evidence.count("validation_correction") == 1

    assert await orch.resume("r") == RunStatus.AWAITING_HUMAN
    assert (await predicates(tmp_db))[-1] == "final_review"


async def test_rejection_without_rounds_left_asks_for_guidance(tmp_db, tmp_path):
    gate = GateStub(test_changes=[CHANGE])
    orch = orchestrator(tmp_db, gate=gate)
    orch.config.mat.limits.max_review_rounds = 0
    await orch.run("x", run_id="r", workspace=workspace(tmp_path))
    await orch.resume("r")

    assert await orch.resume("r", approve=False) == RunStatus.AWAITING_HUMAN
    pause = await tmp_db.fetch_one(
        "SELECT content FROM evidence WHERE predicate = 'validation_failed';"
    )
    assert "recusou" in json.loads(pause["content"])["reason"]
    assert (await tmp_db.fetch_one("SELECT status FROM attempts;"))["status"] == "failed"


async def test_simulation_treats_pending_changes_as_a_failure(tmp_db):
    orch = orchestrator(tmp_db, gate=GateStub(test_changes=[CHANGE]), mode=ExecutionMode.SIMULATION)
    assert await orch.run("x", run_id="r") == RunStatus.FAILED
    assert "test_change_approval" not in await predicates(tmp_db)


def test_v1_snapshots_are_still_readable():
    state = PipelineState.from_snapshot(
        {
            "version": 1,
            "task_description": "x",
            "supervised": True,
            "phase": Phase.AWAITING_HUMAN.value,
            "plan_index": 1,
            "attempt_index": 1,
            "pause_reason": "plan_approval",
        }
    )
    assert state.attempt is None
    assert state.approved_test_changes == {}
    assert PipelineState.from_snapshot(state.to_snapshot()).pause_reason == "plan_approval"


# ── CLI de ponta a ponta com o gate real ─────────────────────────────────────

EDIT_EXISTING_TEST = answer(
    Block(
        "tool_use",
        id="t1",
        name="text_editor",
        input={
            "command": "str_replace",
            "path": "tests/test_app.py",
            "old_str": "assert 1 == 1",
            "new_str": "assert 2 == 2",
        },
    ),
    stop="tool_use",
)


async def test_cli_shows_the_test_diff_and_resumes_after_approval(repo, mat_config, capsys):
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text(
        "def test_app():\n    assert 1 == 1\n", encoding="utf-8", newline="\n"
    )
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "-m", "testes")
    sha = (await list_branches(repo))[0].sha

    llm = scripted_llm(PLAN, EDIT_EXISTING_TEST, DONE, review(True), verdict(True))
    runner = sandbox(GREEN, GREEN)
    assert await cmd_run(
        "Ajustar teste", repo=str(repo), base="main", base_sha=sha, llm=llm, runner=runner
    ) == 0
    run_id = (await query(mat_config, "SELECT id FROM runs;"))[0]["id"]

    capsys.readouterr()
    assert await cmd_resume(run_id, llm=llm, runner=runner) == 0
    out = capsys.readouterr().out
    assert "tests/test_app.py" in out
    assert "assert 2 == 2" in out
    assert "--reject" in out
    # O gate parou antes de rodar Ruff e pytest.
    assert runner.run_ruff.await_count == 1  # só o baseline

    assert await cmd_resume(run_id, llm=llm, runner=runner) == 0
    evidence = [r["predicate"] for r in await query(mat_config, "SELECT predicate FROM evidence;")]
    assert "test_change_approved" in evidence
    assert evidence[-1] == "final_review"
