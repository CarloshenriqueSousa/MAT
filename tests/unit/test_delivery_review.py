"""Revisão final e entrega (passo 8, D65)."""

from __future__ import annotations

import json
from pathlib import Path

from mat.agents.stubs import (
    AnalystStub,
    GateStub,
    ProgrammerStub,
    ReviewerStub,
    TesterHappyStub,
    VerifierStub,
)
from mat.cli import cmd_report, cmd_resume, cmd_run
from mat.engine.orchestrator import PipelineOrchestrator, ResumeError
from mat.engine.state_machine import ExecutionMode, RunStatus
from mat.models.contracts import Workspace
from mat.project import list_branches
from mat.review import build_delivery_review, diff_stats
from mat.settings import load_config
from tests.conftest import git
from tests.unit.test_implementation import (
    CREATE_PING,
    DONE,
    PLAN,
    query,
    review,
    sandbox,
    scripted_llm,
    verdict,
)

DIFF = """diff --git a/app.py b/app.py
index 1..2 100644
--- a/app.py
+++ b/app.py
@@ -1,2 +1,3 @@
-print('v1')
+print('v2')
+print('extra')
 x = 1
diff --git a/ping.py b/ping.py
new file mode 100644
--- /dev/null
+++ b/ping.py
@@ -0,0 +1,2 @@
+def ping():
+    return 'pong'
diff --git a/old.txt b/old.txt
deleted file mode 100644
--- a/old.txt
+++ /dev/null
@@ -1 +0,0 @@
-velho
"""


def test_diff_stats_counts_lines_per_file():
    stats = [(s.path, s.status, s.added, s.removed) for s in diff_stats(DIFF)]
    assert stats == [
        ("app.py", "modified", 2, 1),
        ("ping.py", "added", 2, 0),
        ("old.txt", "deleted", 0, 1),
    ]


# ── Orquestrador ─────────────────────────────────────────────────────────────


def orchestrator(tmp_db) -> PipelineOrchestrator:
    return PipelineOrchestrator(
        config=load_config(),
        db=tmp_db,
        analyst=AnalystStub(),
        programmer=ProgrammerStub(),
        reviewer=ReviewerStub(),
        tester_happy=TesterHappyStub(),
        verifier=VerifierStub(),
        gate=GateStub(),
        execution_mode=ExecutionMode.IMPLEMENTATION,
    )


async def paused_for_review(tmp_db, tmp_path: Path) -> PipelineOrchestrator:
    orch = orchestrator(tmp_db)
    workspace = Workspace(
        repo_path=tmp_path, base_ref="main", base_sha="a" * 40, branch="mat/r", path=tmp_path
    )
    await orch.run("x", run_id="r", workspace=workspace)
    assert await orch.resume("r") == RunStatus.AWAITING_HUMAN
    return orch


async def test_accepting_the_delivery_completes_the_run(tmp_db, tmp_path):
    orch = await paused_for_review(tmp_db, tmp_path)

    assert await orch.resume("r", reason="ok") == RunStatus.PASSED

    run = await tmp_db.fetch_one("SELECT status, current_phase FROM runs;")
    assert (run["status"], run["current_phase"]) == ("passed", "completed")
    row = await tmp_db.fetch_one(
        "SELECT agent_type, content FROM evidence WHERE predicate = 'delivery_accepted';"
    )
    assert row["agent_type"] == "human"
    assert json.loads(row["content"])["reason"] == "ok"


async def test_rejecting_the_delivery_cancels_the_run(tmp_db, tmp_path):
    orch = await paused_for_review(tmp_db, tmp_path)

    assert await orch.resume("r", approve=False, reason="não era isso") == RunStatus.CANCELLED

    run = await tmp_db.fetch_one("SELECT status FROM runs;")
    assert run["status"] == "cancelled"
    rejected = await tmp_db.fetch_all(
        "SELECT 1 FROM evidence WHERE predicate = 'delivery_rejected';"
    )
    assert len(rejected) == 1


async def test_a_decided_delivery_cannot_be_decided_again(tmp_db, tmp_path):
    orch = await paused_for_review(tmp_db, tmp_path)
    await orch.resume("r")
    try:
        await orch.resume("r", approve=False)
    except ResumeError:
        pass
    else:  # pragma: no cover - falha explícita
        raise AssertionError("uma entrega decidida não pode ser decidida de novo")


# ── Revisão montada a partir do banco e do worktree ──────────────────────────


async def test_review_collects_tests_alerts_errors_and_metrics(tmp_db, tmp_path):
    await paused_for_review(tmp_db, tmp_path)
    await tmp_db.execute(
        "INSERT INTO evidence (evidence_id, run_id, agent_type, predicate, content) "
        "VALUES ('ev_x', 'r', 'human', 'command_approved', ?);",
        (json.dumps({"command": "make lint"}),),
    )

    async def provider(worktree: Path, base_sha: str) -> str:
        assert (worktree, base_sha) == (tmp_path, "a" * 40)
        return DIFF

    result = await build_delivery_review(tmp_db, "r", diff_provider=provider)

    assert result is not None
    assert [f.path for f in result.files] == ["app.py", "ping.py", "old.txt"]
    assert result.tests.gate_passed is True
    assert result.tests.tester_passed is True
    assert result.verdict is not None and result.verdict["approved"] is True
    assert "Comando aprovado na run: make lint" in result.alerts
    assert result.metrics.attempts == 1
    assert result.metrics.dispatches >= 5
    assert result.errors == []


async def test_review_reports_a_missing_worktree(tmp_db, tmp_path):
    await paused_for_review(tmp_db, tmp_path / "sumiu")

    result = await build_delivery_review(tmp_db, "r")

    assert result is not None
    assert any("não existe mais" in alert for alert in result.alerts)


async def test_unknown_run_has_no_review(tmp_db):
    assert await build_delivery_review(tmp_db, "nada") is None


# ── CLI de ponta a ponta ─────────────────────────────────────────────────────


async def test_cli_report_shows_the_review_and_accepting_keeps_the_worktree(
    repo, mat_config, capsys
):
    sha = (await list_branches(repo))[0].sha
    llm = scripted_llm(PLAN, CREATE_PING, DONE, review(True), verdict(True))
    runner = sandbox()
    assert await cmd_run("x", repo=str(repo), base="main", base_sha=sha, llm=llm, runner=runner) == 0
    run_id = (await query(mat_config, "SELECT id FROM runs;"))[0]["id"]
    assert await cmd_resume(run_id, llm=llm, runner=runner) == 0

    capsys.readouterr()
    assert await cmd_report(run_id, show_diff=True) == 0
    out = capsys.readouterr().out
    assert "ping.py" in out
    assert "return 'pong'" in out
    assert "Métricas" in out
    assert "Verificador: aprovou" in out

    # Aceitar não chama agentes: sem LLM nem sandbox.
    capsys.readouterr()
    assert await cmd_resume(run_id) == 0
    out = capsys.readouterr().out
    assert "Entrega aceita" in out
    assert "sem commit" in out

    run = (await query(mat_config, "SELECT status, worktree_path FROM runs;"))[0]
    assert run["status"] == "passed"
    worktree = Path(run["worktree_path"])
    assert git(worktree, "rev-parse", "HEAD") == sha
    assert "ping.py" in git(worktree, "status", "--porcelain")


async def test_cli_rejecting_the_delivery(repo, mat_config, capsys):
    sha = (await list_branches(repo))[0].sha
    llm = scripted_llm(PLAN, CREATE_PING, DONE, review(True), verdict(True))
    runner = sandbox()
    await cmd_run("x", repo=str(repo), base="main", base_sha=sha, llm=llm, runner=runner)
    run_id = (await query(mat_config, "SELECT id FROM runs;"))[0]["id"]
    await cmd_resume(run_id, llm=llm, runner=runner)

    capsys.readouterr()
    assert await cmd_resume(run_id, reject=True) == 0
    assert "Entrega recusada" in capsys.readouterr().out
    assert (await query(mat_config, "SELECT status FROM runs;"))[0]["status"] == "cancelled"
