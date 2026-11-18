"""Branch/worktree por solicitação e pausa por checkout sujo (passo 3, D58)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mat.cli import cmd_report, cmd_run
from mat.db.connection import Database
from mat.project import (
    BaseMovedError,
    DirtyCheckoutError,
    ProjectError,
    dirty_files,
    list_branches,
    open_project,
    prepare_workspace,
    resolve_selection,
)
from mat.sandbox.worktree import WorktreeManager


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    path = tmp_path / "alvo"
    path.mkdir()
    git(path, "init", "--quiet", "-b", "main")
    git(path, "config", "user.name", "MAT tests")
    git(path, "config", "user.email", "mat-tests@localhost")
    (path / "app.py").write_text("print('v1')\n", encoding="utf-8", newline="\n")
    (path / "old.txt").write_text("velho\n", encoding="utf-8", newline="\n")
    git(path, "add", "--all")
    git(path, "commit", "--quiet", "-m", "inicial")
    return open_project(path)


@pytest.fixture
def mat_config(tmp_path, monkeypatch) -> Path:
    """Isola banco e worktrees da CLI no tmp_path."""
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    (config_dir / "mat.yaml").write_text(
        "paths:\n"
        f"  db: '{(tmp_path / 'mat.db').as_posix()}'\n"
        f"  worktrees: '{(tmp_path / 'worktrees').as_posix()}'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MAT_CONFIG_DIR", str(config_dir))
    return tmp_path


async def test_clean_checkout_has_no_dirty_files(repo):
    assert await dirty_files(repo) == []


async def test_dirty_files_cover_modified_staged_untracked_and_renamed(repo):
    (repo / "app.py").write_text("print('local')\n", encoding="utf-8", newline="\n")
    (repo / "novo.txt").write_text("não rastreado\n", encoding="utf-8", newline="\n")
    git(repo, "mv", "old.txt", "renomeado.txt")

    assert sorted(await dirty_files(repo)) == ["app.py", "novo.txt", "renomeado.txt"]


async def test_dirty_check_does_not_rewrite_the_index(repo):
    index = repo / ".git" / "index"
    before = index.stat().st_mtime_ns
    (repo / "app.py").touch()  # muda o stat; um `git status` comum regravaria o índice

    await dirty_files(repo)

    assert index.stat().st_mtime_ns == before


async def test_prepare_workspace_keeps_original_checkout_intact(repo, tmp_path):
    base_sha = git(repo, "rev-parse", "HEAD")
    selection = await resolve_selection(repo, "main", base_sha)

    workspace = await prepare_workspace(WorktreeManager(tmp_path / "wt"), selection, "run_abc")

    assert workspace.branch == "mat/run_abc"
    assert workspace.base_ref == "main"
    assert workspace.base_sha == base_sha
    assert workspace.path == tmp_path / "wt" / "run_abc"
    assert git(workspace.path, "rev-parse", "HEAD") == base_sha
    assert git(workspace.path, "branch", "--show-current") == "mat/run_abc"
    # checkout original: mesma branch, mesmo commit, sem alterações
    assert git(repo, "branch", "--show-current") == "main"
    assert git(repo, "rev-parse", "HEAD") == base_sha
    assert git(repo, "status", "--porcelain") == ""


async def test_dirty_checkout_creates_nothing_and_keeps_changes(repo, tmp_path):
    selection = await resolve_selection(repo, "main", git(repo, "rev-parse", "HEAD"))
    (repo / "app.py").write_text("print('trabalho local')\n", encoding="utf-8", newline="\n")

    with pytest.raises(DirtyCheckoutError) as info:
        await prepare_workspace(WorktreeManager(tmp_path / "wt"), selection, "run_sujo")

    assert info.value.files == ["app.py"]
    assert (repo / "app.py").read_text(encoding="utf-8") == "print('trabalho local')\n"
    assert not (tmp_path / "wt" / "run_sujo").exists()
    assert "mat/run_sujo" not in git(repo, "branch", "--list")


async def test_resolve_selection_checks_name_and_sha(repo):
    sha = git(repo, "rev-parse", "HEAD")
    with pytest.raises(ProjectError, match="não encontrada"):
        await resolve_selection(repo, "inexistente", sha)
    with pytest.raises(ProjectError, match="SHA inválido"):
        await resolve_selection(repo, "main", sha[:12])
    (repo / "app.py").write_text("print('v2')\n", encoding="utf-8", newline="\n")
    git(repo, "commit", "--quiet", "-am", "v2")
    with pytest.raises(BaseMovedError, match="escolha a base de novo"):
        await resolve_selection(repo, "main", sha)


# ── CLI ──────────────────────────────────────────────────────────────────────


async def test_cli_run_creates_and_records_workspace(repo, mat_config):
    sha = (await list_branches(repo))[0].sha

    code = await cmd_run("Adicionar /ping", repo=str(repo), base="main", base_sha=sha)

    assert code == 0
    db = Database(mat_config / "mat.db")
    run = await db.fetch_one("SELECT * FROM runs;")
    await db.close()
    assert run["status"] == "passed"
    assert run["base_sha"] == sha
    assert (run["repo_path"], run["base_ref"]) == (str(repo), "main")
    assert run["work_branch"] == f"mat/{run['id']}"
    assert Path(run["worktree_path"]) == mat_config / "worktrees" / run["id"]
    assert Path(run["worktree_path"], "app.py").exists()
    assert await cmd_report(run["id"]) == 0


async def test_cli_run_pauses_on_dirty_checkout(repo, mat_config):
    sha = git(repo, "rev-parse", "HEAD")
    (repo / "rascunho.txt").write_text("wip\n", encoding="utf-8", newline="\n")

    code = await cmd_run("Adicionar /ping", repo=str(repo), base="main", base_sha=sha)

    assert code == 0
    db = Database(mat_config / "mat.db")
    run = await db.fetch_one("SELECT * FROM runs;")
    evidence = await db.fetch_one(
        "SELECT content FROM evidence WHERE predicate = 'dirty_checkout';"
    )
    attempts = await db.fetch_all("SELECT id FROM attempts;")
    await db.close()
    assert run["status"] == "awaiting_human"
    assert run["worktree_path"] is None
    assert run["work_branch"] == f"mat/{run['id']}"
    content = json.loads(evidence["content"])
    assert content["files"] == ["rascunho.txt"]
    assert content["task_description"] == "Adicionar /ping"
    assert attempts == []
    assert not (mat_config / "worktrees" / run["id"]).exists()
    assert (repo / "rascunho.txt").read_text(encoding="utf-8") == "wip\n"


async def test_cli_run_requires_all_project_flags(mat_config):
    assert await cmd_run("x", repo=".", base="main") == 1


async def test_schema_v2_runs_gain_workspace_columns(tmp_path):
    db = Database(tmp_path / "v2.db")
    await db.init_schema()
    columns = {row["name"] for row in await db.fetch_all("PRAGMA table_info(runs);")}
    await db.close()
    assert {"repo_path", "base_ref", "work_branch", "worktree_path"} <= columns
